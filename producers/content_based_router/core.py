"""
Content-Based Router — the KafkaEventRouter class.

What this file is for:
    One small, reusable class that ties the pieces together:
        validate -> route -> serialize -> hand to ONE shared producer -> count
    the delivery results that come back later through callbacks.

The six ideas this class is built around:
    1. It is GIVEN a producer; it never creates one. The application owns the
       producer's whole life (create once at startup, flush once at shutdown).
    2. It validates and routes BEFORE touching Kafka.
    3. One producer serves many topics — never one producer per topic/request.
    4. It counts accepted / delivered / failed separately, so "accepted" is
       never mistaken for "delivered".
    5. A temporarily full local queue is retried, but only until a deadline.
    6. publish() returns LOCAL acceptance, never a promise of delivery.

ASCII flow of publish(event):

    event
      |
      v
   STEP 1  route_event(event)        -> topic, key, reason   (bad event raises)
      |
      v
   STEP 2  serialize_event(event)    -> bytes
      |
      v
   STEP 3  producer.produce(...)     -> accepted locally? if queue full, retry
      |                                  with poll() until a deadline
      v
   STEP 4  producer.poll(0)          -> run any ready delivery callbacks
      |
      v
   PublishReceipt (local acceptance)   ... delivery result counted later
"""

from __future__ import annotations

# logging: record failures and milestones without using print() inside a library.
import logging

# threading: a web server may call publish() from several threads at once, all
# sharing this one router. A lock guards our counters so two threads cannot
# corrupt them while incrementing at the same time.
import threading

# time.monotonic: a clock that only moves forward and is immune to the system
# clock being changed. We use it to measure the retry deadline reliably.
import time

from .constants import (
    DEFAULT_QUEUE_WAIT_SECONDS,
    HEADER_KEY_EVENT_ID,
    HEADER_KEY_EVENT_TYPE,
    QUEUE_FULL_POLL_INTERVAL_SECONDS,
)
from .events import serialize_event
from .routing import route_event
from .types import KafkaProducerLike, LocalQueueTimeoutError, PublishReceipt


# ============================================================================
# STEP 0: module logger
# ============================================================================

# One stable logger name so an application can route or filter these records.
logger = logging.getLogger("producers.content_based_router")


class KafkaEventRouter:
    """Validate, route, and publish business events through ONE shared producer.

    Create it once with a producer you built at application startup, call
    publish() for each event, read snapshot() whenever you want the counts, and
    let the application flush the producer at shutdown.
    """

    # ------------------------------------------------------------------
    # STEP 1: construction — store the injected producer and set up counters
    # ------------------------------------------------------------------

    def __init__(
        self,
        producer: KafkaProducerLike,
        *,
        topic_prefix: str | None = None,
        queue_wait_seconds: float = DEFAULT_QUEUE_WAIT_SECONDS,
        log: logging.Logger | None = None,
    ) -> None:
        """Set up the router around an already-created producer.

        Args:
            producer:            the shared producer this router will use. The
                                 application creates and owns it; we only borrow.
            topic_prefix:        override the default topic prefix if you want
                                 this router to write to a different topic group.
                                 None means "use the package default".
            queue_wait_seconds:  how long publish() may retry a full local queue
                                 before giving up with LocalQueueTimeoutError.
            log:                 inject a custom logger (handy in tests); None
                                 uses this module's logger.

        Raises:
            ValueError: queue_wait_seconds must be positive — a zero or negative
            budget would make the retry loop meaningless.
        """
        if queue_wait_seconds <= 0:
            raise ValueError("queue_wait_seconds must be greater than zero.")

        self._producer = producer
        self._topic_prefix = topic_prefix
        self._queue_wait_seconds = queue_wait_seconds
        self._logger = log or logger

        # The lock protects the counters below. A delivery callback may run on
        # whichever thread happens to call poll()/flush(), so increments to
        # these numbers must happen one thread at a time.
        self._lock = threading.Lock()

        # accepted  = events the producer took into its local queue
        # delivered = events Kafka later confirmed via a success callback
        # failed    = events whose delivery callback reported an error
        self._accepted_count = 0
        self._delivered_count = 0
        self._failed_count = 0

    # ------------------------------------------------------------------
    # STEP 2: publish — the one method business code calls
    # ------------------------------------------------------------------

    def publish(self, event: dict) -> PublishReceipt:
        """Route one event and queue it for asynchronous delivery.

        Returns a PublishReceipt only after the producer accepts the event into
        its local queue. The real delivery outcome arrives later and is recorded
        in snapshot() by the delivery callback built below.

        Raises:
            EventValidationError / UnknownEventTypeError: invalid event (nothing
                is sent).
            LocalQueueTimeoutError: the local queue stayed full past the deadline.
        """
        # ---- 2.1: decide the destination (validates the event too) ----
        decision = (
            route_event(event, prefix=self._topic_prefix)
            if self._topic_prefix is not None
            else route_event(event)
        )

        # ---- 2.2: turn the event into the bytes Kafka will carry ----
        payload = serialize_event(event)
        event_id = event["event_id"]

        # ---- 2.3: build this event's delivery callback ----
        on_delivery = self._build_delivery_callback(event_id, decision.topic)

        # ---- 2.4: hand the message to the producer, retrying a full queue ----
        self._produce_with_backpressure(event, decision, payload, on_delivery)

        # ---- 2.5: record local acceptance ----
        with self._lock:
            self._accepted_count += 1

        # ---- 2.6: let the producer run any callbacks that are ready now ----
        # poll(0) does not block; it just services work that is already done.
        self._producer.poll(0)

        return PublishReceipt(
            event_id=event_id,
            topic=decision.topic,
            key=decision.key,
            route_reason=decision.reason,
        )

    # ------------------------------------------------------------------
    # STEP 3: the delivery callback — count the eventual Kafka result
    # ------------------------------------------------------------------

    def _build_delivery_callback(self, event_id: str, topic: str):
        """Return a callback the producer will run when delivery finishes.

        The returned function "remembers" this event's id and topic (that is
        what a closure does) so, when it runs later, it can log the right event.
        We keep the callback tiny — just counting and logging — because it runs
        on the producer's thread and any slow work here would delay every other
        ready callback.
        """

        def on_delivery(error, message) -> None:
            # error is None on success, or an exception describing the failure.
            with self._lock:
                if error is None:
                    self._delivered_count += 1
                else:
                    self._failed_count += 1

            if error is not None:
                self._logger.error(
                    "Kafka delivery failed event_id=%s topic=%s error=%s",
                    event_id,
                    topic,
                    error,
                )

        return on_delivery

    # ------------------------------------------------------------------
    # STEP 4: produce with bounded back-pressure handling
    # ------------------------------------------------------------------

    def _produce_with_backpressure(
        self,
        event: dict,
        decision,
        payload: bytes,
        on_delivery,
    ) -> None:
        """Hand one message to the producer, retrying only while the queue is full.

        A full local queue makes produce() raise BufferError, meaning the message
        was NOT accepted on that attempt. We briefly let the producer make
        progress, then retry the SAME message — but only until a deadline, so a
        permanently overloaded producer can never hang the caller forever.
        """
        event_id = event["event_id"]

        # Encode the key and headers once, outside the retry loop.
        key_bytes = decision.key.encode("utf-8")
        headers = [
            (HEADER_KEY_EVENT_ID, event_id.encode("utf-8")),
            (HEADER_KEY_EVENT_TYPE, event["event_type"].encode("utf-8")),
        ]

        # The absolute time by which we must have been accepted, or we give up.
        deadline = time.monotonic() + self._queue_wait_seconds

        while True:
            try:
                self._producer.produce(
                    topic=decision.topic,
                    key=key_bytes,
                    value=payload,
                    on_delivery=on_delivery,
                    headers=headers,
                )
                # Accepted into the local queue — stop retrying.
                return
            except BufferError as error:
                # How much of our time budget is left?
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LocalQueueTimeoutError(
                        f"Local Kafka queue stayed full for event_id={event_id}"
                    ) from error
                # Give the producer a short slice to drain, then retry. We never
                # poll longer than the time we have left.
                self._producer.poll(min(QUEUE_FULL_POLL_INTERVAL_SECONDS, remaining))

    # ------------------------------------------------------------------
    # STEP 5: snapshot — a point-in-time copy of the counters
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return current counts, including how many are still awaiting a callback.

        awaiting_callbacks = accepted - delivered - failed. A non-zero value just
        means some results have not come back yet; call poll()/flush() to let
        them arrive. Read this instead of assuming "flush returned 0" means every
        message succeeded.
        """
        with self._lock:
            accepted = self._accepted_count
            delivered = self._delivered_count
            failed = self._failed_count

        return {
            "accepted": accepted,
            "delivered": delivered,
            "failed": failed,
            "awaiting_callbacks": accepted - delivered - failed,
        }
