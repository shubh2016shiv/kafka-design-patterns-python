"""Callback-confirmed publishing (Pattern 2): send async, learn each final outcome.

Flow:
    publish_event() -> produce(on_delivery=callback)   # accepted in the LOCAL waiting list
    poll()          -> runs callbacks that are ready   # call it in your loop
    finish()        -> flush, then DeliverySummary     # call once at shutdown

The same flow in everyday words (a courier story):
    publish_event()   you put a parcel on the courier's desk and walk away
    the callback      the phone number you wrote on the parcel
    poll()            you check your phone: did the courier call with news?
    finish()          end of day: wait for the last calls, then write a report

Use from ONE thread. The counters below are plain integers with no locking.

HOW TO REUSE THIS FOR A NEW PROJECT: see README.md ("Reuse in a new project").
"""

# logging lets us record what happened, so your app's log tools can collect it.
import logging

# time gives us a stopwatch (time.monotonic) for "give up after N seconds" logic.
import time

# deque is a list with a maximum size: old entries fall off the far end by themselves.
from collections import deque

# Producer is the real Kafka client. confluent_kafka is the library that talks to Kafka.
from confluent_kafka import Producer

# DeliverySummary is the final report returned by finish().
from .delivery_summary import DeliverySummary

# EventSchema says which fields an event needs; ORDER_EVENT_SCHEMA is the default;
# encode_event_as_json turns an event into the bytes Kafka stores.
from .event_schema import ORDER_EVENT_SCHEMA, EventSchema, encode_event_as_json

# The wait times and the Kafka settings builder (each explained in producer_config.py).
from .producer_config import (
    LOCAL_QUEUE_FULL_MAX_WAIT_SECONDS,
    LOCAL_QUEUE_FULL_POLL_SLICE_SECONDS,
    SHUTDOWN_FLUSH_TIMEOUT_SECONDS,
    build_producer_config,
)

# One shared logger name for the package, so you can filter its logs in one place.
logger = logging.getLogger("producers.callback_confirmed")

# How many failures we remember for the final report (event id + reason, never contents).
# 20 is enough to diagnose a problem, and small enough that memory can never grow forever.
RECENT_FAILURE_SAMPLE_SIZE = 20


class CallbackConfirmedProducer:
    """Publish many events asynchronously and count their final outcomes.

    Errors:
        publish_event() raises ValueError for a bad event, TimeoutError if the local
        waiting list stays full, RuntimeError if used after finish().
        confluent_kafka.KafkaException is raised (and the event is not counted as
        queued) when the client refuses a message outright, e.g. it is too large.
        Delivery failures do NOT raise; they appear in the DeliverySummary.
    """

    def __init__(
        self,
        topic_name: str,
        *,
        event_schema: EventSchema = ORDER_EVENT_SCHEMA,
        bootstrap_servers: str | None = None,
        extra_kafka_settings: dict | None = None,
        local_queue_max_wait_seconds: float = LOCAL_QUEUE_FULL_MAX_WAIT_SECONDS,
        kafka_producer=None,
    ) -> None:
        """Create the producer.

        Args:
            topic_name: Topic to publish to. It must already exist (many brokers have
                auto-creation turned off).
            event_schema: Which fields your events must contain, and which one is the
                ordering key. Change this (not the code) for a new kind of event.
            bootstrap_servers: Broker addresses. None reads KAFKA_BOOTSTRAP_SERVERS.
            extra_kafka_settings: Added to the defaults; put TLS/SASL settings here.
            local_queue_max_wait_seconds: How long publish may wait for room.
            kafka_producer: Only for tests (a pretend Kafka). Leave None in real use.
        """
        # STEP 0 - set up: hire the courier and start with empty tally sheets.

        # Step 0.1: Check what we cannot work without. Failing now, with a clear
        # message, is better than failing in a confusing way later.
        if not isinstance(topic_name, str) or not topic_name.strip():
            raise ValueError("topic_name must be a non-empty string")
        if local_queue_max_wait_seconds < 0:
            raise ValueError("local_queue_max_wait_seconds must not be negative")

        # Step 0.2: Remember where messages go, what events look like, and how long
        # we are willing to wait when the waiting list is full.
        self.topic_name = topic_name
        self.event_schema = event_schema
        self.local_queue_max_wait_seconds = local_queue_max_wait_seconds

        # Step 0.3: Get the courier. In real use we create the real Kafka client with
        # safe settings plus anything extra you passed (like a login). Tests hand in a
        # pretend courier instead, so they need no real Kafka.
        if kafka_producer is None:
            kafka_producer = Producer(
                build_producer_config(bootstrap_servers, extra_kafka_settings)
            )
        self.kafka_producer = kafka_producer

        # Step 0.4: Start the tally sheets at zero. "queued" counts parcels put on the
        # desk; "delivered" and "failed" count the courier's phone calls, later.
        self.is_finished = False
        self.queued_count = 0
        self.delivered_count = 0
        self.failed_count = 0

        # Step 0.5: A short notebook of the latest failures (which event, what went
        # wrong). maxlen means it holds at most 20 entries: it can never grow forever.
        self.recent_delivery_failures: deque = deque(maxlen=RECENT_FAILURE_SAMPLE_SIZE)

    # ------------------------------------------------------------------
    # STEP 1 - publish_event(): validate, encode, queue
    # ------------------------------------------------------------------
    def publish_event(self, event: dict) -> str:
        """Queue one event. Returns its event id: "accepted locally", NOT "delivered"."""
        # Step 1.1: Refuse if we already closed for the day. A parcel handed over after
        # closing time would never get a phone call.
        if self.is_finished:
            raise RuntimeError("Cannot publish after finish()")

        # Step 1.2: Check the parcel is properly filled in (required fields present,
        # id and key not empty). A bad parcel is rejected here, before anything is sent.
        self.event_schema.validate(event)

        # Step 1.3: Remember the parcel's name (the event id). We need it later to say
        # WHICH parcel the courier is phoning about.
        event_id = self.event_schema.read_event_id(event)

        # Step 1.4: Pack the parcel. The "key" decides its shelf: same key, same shelf,
        # so those events stay in order. The "value" is the whole event written as text.
        # Step 1.5: Put it on the courier's desk. This returns straight away. It does
        # NOT wait for the message to arrive anywhere.
        self._queue_message(
            event_id,
            message_key=self.event_schema.build_message_key(event),
            message_value=encode_event_as_json(event),
        )

        # Step 1.6: Hand the name back to the caller. This only means "it is on the
        # desk", NOT "it arrived".
        return event_id

    def _queue_message(self, event_id: str, *, message_key: bytes, message_value: bytes) -> None:
        """Call produce(); if the local queue is full, wait briefly and retry.

        Retrying is safe because BufferError means produce() did NOT accept the
        message. We stop at the deadline so a stuck broker cannot hang the caller.
        """
        # Step 1.5.1: Start a stopwatch. If the desk stays full longer than we are
        # willing to wait, we give up instead of waiting forever.
        deadline = time.monotonic() + self.local_queue_max_wait_seconds

        # Step 1.5.2: Write this parcel's "phone number" (the callback). It remembers
        # the event id, so the later phone call can name the parcel.
        delivery_callback = self._build_delivery_callback(event_id)

        # Keep trying until the parcel is accepted, or we run out of time (below).
        while True:
            try:
                # Step 1.5.3: Try to put the parcel on the desk with the phone number
                # attached. on_delivery is the ONE thing that makes this
                # "callback-confirmed". Without it we would never hear back.
                self.kafka_producer.produce(
                    topic=self.topic_name,
                    key=message_key,
                    value=message_value,
                    on_delivery=delivery_callback,
                )
                # Step 1.5.4: It worked. Count one parcel on the desk, and stop. We count
                # only AFTER success, so a refused parcel is never counted.
                self.queued_count += 1
                return
            except BufferError:
                # Step 1.5.5: BufferError means the desk is full and the courier did NOT
                # take the parcel. Nothing was sent, so trying again cannot cause a
                # duplicate. First check the stopwatch.
                seconds_left = deadline - time.monotonic()
                if seconds_left <= 0:
                    # Step 1.5.6: Out of time. Tell the caller clearly. This parcel was
                    # NOT queued, so the caller must decide what to do with it.
                    # "from None" hides the confusing internal BufferError in the traceback.
                    raise TimeoutError(
                        f"Local queue stayed full; event_id={event_id} was NOT queued"
                    ) from None
                # Step 1.5.7: Time remains. Give the courier a moment to clear the desk
                # (poll), then loop back and try this same parcel again.
                self.kafka_producer.poll(min(LOCAL_QUEUE_FULL_POLL_SLICE_SECONDS, seconds_left))

    # ------------------------------------------------------------------
    # STEP 2 - delivery callback: runs later, inside poll() or flush()
    # ------------------------------------------------------------------
    def _build_delivery_callback(self, event_id: str):
        """Build the callback for ONE event; it remembers the event id (a closure).

        The client calls callback(delivery_error, delivered_message). We only count
        and log here, because slow work inside a callback delays every other callback.
        """

        # Step 2.1: This inner function is the "phone call". It does not run now. Kafka
        # runs it later, once it knows what happened to this one parcel. It can still
        # see event_id from the outer function: that is how it knows which parcel.
        def on_delivery(delivery_error, delivered_message) -> None:
            # Step 2.2: No error means good news: Kafka stored the message.
            if delivery_error is None:
                # Step 2.2.1: Add one to the "delivered" tally.
                self.delivered_count += 1
                # Step 2.2.2: Write a quiet note of where it landed (partition = which
                # lane of the topic, offset = its position there). Only the id and
                # location are logged, never the message contents.
                logger.debug(
                    "acknowledged event_id=%s partition=%s offset=%s",
                    event_id,
                    delivered_message.partition(),
                    delivered_message.offset(),
                )
            # Step 2.3: An error means bad news: Kafka gave up on this parcel.
            else:
                # Step 2.3.1: Add one to the "failed" tally.
                self.failed_count += 1
                # Step 2.3.2: Write it in the failures notebook (parcel name + reason),
                # so the final report can list what went wrong.
                self.recent_delivery_failures.append((event_id, str(delivery_error)))
                # Step 2.3.3: Also shout in the logs, so a person or an alert system
                # notices straight away.
                logger.error("delivery failed event_id=%s error=%s", event_id, delivery_error)
            # Step 2.4: Keep this short and quick. No slow work in here, because every
            # other waiting phone call has to queue up behind this one.

        # Step 2.5: Hand the "phone number" back so it can be attached to the parcel.
        return on_delivery

    # ------------------------------------------------------------------
    # STEP 3 - poll(): serve ready callbacks while you keep publishing
    # ------------------------------------------------------------------
    def poll(self, timeout_seconds: float = 0.0) -> int:
        """Run callbacks that are ready. 0 means do not wait. Returns how many ran."""
        # Step 3.1: Refuse after closing. The final report must not change once written.
        if self.is_finished:
            raise RuntimeError("Cannot poll after finish()")

        # Step 3.2: Check the phone. Every call that has come in (news about a parcel)
        # is answered now, which runs that parcel's callback from Step 2. 0 means
        # "answer what is already ringing, don't wait for more". Returns how many
        # calls were answered.
        return self.kafka_producer.poll(timeout_seconds)

    # ------------------------------------------------------------------
    # STEP 3b - snapshot_delivery_counts(): look at the tallies at any time
    # ------------------------------------------------------------------
    def snapshot_delivery_counts(self) -> dict:
        """Return the current tallies. Safe to call any time, even long before finish().

        For a service that runs for days: call this now and then and send the numbers
        to your monitoring. They are a moving picture, not a final verdict. Only the
        DeliverySummary from finish() says whether everything was confirmed.
        """
        # A fresh dict each time, so the caller cannot change our real counters.
        return {
            "queued": self.queued_count,
            "delivered": self.delivered_count,
            "failed": self.failed_count,
            # Still waiting for news. Normal while running; a problem if it stays high.
            "awaiting_callback": self.queued_count - self.delivered_count - self.failed_count,
        }

    # ------------------------------------------------------------------
    # STEP 4 - finish(): drain once and report honestly
    # ------------------------------------------------------------------
    def finish(self, timeout_seconds: float = SHUTDOWN_FLUSH_TIMEOUT_SECONDS) -> DeliverySummary:
        """Wait (up to the timeout) for outstanding messages; return the summary."""
        # Step 4.1: This can only be done once. A second report could disagree with the
        # first.
        if self.is_finished:
            raise RuntimeError("finish() has already been called")

        # Step 4.2: Close for the day: from now on no new parcels, no more polling.
        self.is_finished = True

        # Step 4.3: Wait for the courier to finish (up to the time limit). While we
        # wait, the remaining phone calls arrive and their callbacks run. It returns
        # how many parcels were STILL unfinished when time ran out.
        unfinished_message_count = self.kafka_producer.flush(timeout_seconds)

        # Step 4.4: Write the final report from the tally sheets. "awaiting" = parcels
        # put on the desk minus all the calls we got. Those have no answer at all,
        # which is NOT the same as good news.
        return DeliverySummary(
            queued_count=self.queued_count,
            delivered_count=self.delivered_count,
            failed_count=self.failed_count,
            awaiting_callback_count=(self.queued_count - self.delivered_count - self.failed_count),
            unfinished_message_count=unfinished_message_count,
            recent_delivery_failures=tuple(self.recent_delivery_failures),
        )
