"""Reusable callback-confirmed publishing with honest delivery accounting.

The notebook's full lifecycle becomes one small service-facing abstraction:

    create one producer
            |
            v
    Stage 1.0 publish(event) -- validate and JSON encode
            |
            v
    Stage 2.0 produce(..., on_delivery=callback) -- local queue acceptance
            |
            +---- queue full ----> poll briefly and retry until deadline
            |
            v
    Stage 3.0 poll() -- run ready success/failure callbacks
            |
            v
    Stage 4.0 finish() -- bounded flush and truthful DeliverySummary

Plain-language terms:

    local queue     memory inside this process that holds unfinished messages
    callback        a function saved now so the Kafka client can call it later
    correlation     linking a later callback to the original business event
    back-pressure   the client saying "my local queue is full; slow down"
    flush           wait for queued work to finish or for a timeout to expire
    delivery        the producer's final Kafka outcome, not consumer processing

This class is intentionally designed for one application thread to publish and
service callbacks. The underlying Kafka client supports multiple threads, but
these Python counters would need synchronization before sharing this wrapper.
"""

# Lets us write type hints like `ProducerConfig | None` below without Python
# evaluating them immediately when this file loads.
from __future__ import annotations

# json converts application dictionaries into UTF-8 bytes understood by Kafka.
import json
# logging records failures and delivery metadata without using print in a library.
import logging
# time.monotonic provides a deadline that wall-clock changes cannot move backward.
import time
# deque stores only a bounded number of recent failures, preventing memory growth.
from collections import deque

# The adapter factory is the only default path that creates a concrete Kafka client.
from .clients import default_producer_factory
# The configuration builder gives direct users the same reviewed defaults as tests.
from .config import build_callback_producer_config
# Named constants document waiting decisions instead of scattering magic numbers.
from .constants import (
    DEFAULT_FLUSH_TIMEOUT_SECONDS,
    DEFAULT_POLL_TIMEOUT_SECONDS,
    DEFAULT_QUEUE_CAPACITY_WAIT_SECONDS,
    QUEUE_CAPACITY_POLL_INTERVAL_SECONDS,
)
# Shared types keep the behavior testable without a broker or Kafka installation.
from .types import (
    DeliveryFailure,
    DeliverySummary,
    JsonObject,
    KafkaProducerLike,
    ProducerConfig,
    ProducerFactory,
)

# Use one stable logger name so applications can route or filter these records.
logger = logging.getLogger("producers.callback_confirmed")

# Business fields required by default for the notebook's order-event example.
DEFAULT_REQUIRED_EVENT_FIELDS: tuple[str, ...] = ("event_id", "event_type", "order_id")
# Keep a small diagnostic sample rather than retaining every failed payload forever.
DEFAULT_FAILURE_SAMPLE_SIZE: int = 20


class CallbackConfirmedProducer:
    """Publish many business events asynchronously and track final callbacks.

    Purpose:
        Reuse one Kafka client, correlate callbacks by ``event_id``, handle
        local queue pressure for a bounded time, and report delivery honestly.

    Input contract:
        By default, every event contains ``event_id``, ``event_type``, and
        ``order_id``. The order id becomes the Kafka key, keeping one order's
        events in the same partition while topic partitioning remains stable.
        Constructor field-name arguments adapt that contract for another domain.

    Output contract:
        ``publish`` returns the event id after local acceptance only.
        ``finish`` returns a :class:`DeliverySummary`; its ``all_confirmed``
        property is the final producer-level success decision.

    Failure behavior:
        Invalid events raise ``ValueError``. A local queue that stays full
        raises ``TimeoutError``. Other immediate client errors propagate.
        Final delivery errors are recorded by callbacks in the summary.
    """

    def __init__(
        self,
        topic: str,
        *,
        config: ProducerConfig | None = None,
        producer_factory: ProducerFactory = default_producer_factory,
        queue_capacity_wait_seconds: float = DEFAULT_QUEUE_CAPACITY_WAIT_SECONDS,
        failure_sample_size: int = DEFAULT_FAILURE_SAMPLE_SIZE,
        event_id_field: str = "event_id",
        key_field: str = "order_id",
        required_event_fields: tuple[str, ...] = DEFAULT_REQUIRED_EVENT_FIELDS,
    ) -> None:
        """Create one long-lived publisher and initialize bounded counters.

        Args:
            topic: Destination Kafka topic, meaning a named message stream.
            config: Kafka settings. ``None`` uses reviewed package defaults.
            producer_factory: Injectable client constructor used by unit tests.
            queue_capacity_wait_seconds: Total time allowed for queue-full retries.
            failure_sample_size: Maximum recent failures retained for diagnosis.
            event_id_field: Payload field used to correlate a later callback.
            key_field: Payload field encoded as the Kafka partition key.
            required_event_fields: Schema fields every published event must contain.

        Failure behavior:
            Raises ``ValueError`` for invalid local arguments. Client creation
            errors propagate because the publisher cannot operate without it.
        """
        # A blank topic is a permanent caller bug, not a condition worth retrying.
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("topic must be a non-empty string.")
        # Negative waiting would make deadline behavior surprising and invalid.
        if queue_capacity_wait_seconds < 0:
            raise ValueError("queue_capacity_wait_seconds must be zero or greater.")
        # At least one failure should be retained when diagnostic sampling is enabled.
        if failure_sample_size < 1:
            raise ValueError("failure_sample_size must be at least 1.")
        # Configurable field names make this producer reusable beyond order events.
        if not event_id_field.strip() or not key_field.strip():
            raise ValueError("event_id_field and key_field must not be blank.")
        # A completely empty schema would provide no contract for application events.
        if not required_event_fields:
            raise ValueError("required_event_fields must contain at least one field.")

        # Store the destination once so callers cannot accidentally vary it per event.
        self._topic = topic
        # Store the bounded back-pressure budget used by publish().
        self._queue_capacity_wait_seconds = queue_capacity_wait_seconds
        # Store the field that gives each callback a stable business identity.
        self._event_id_field = event_id_field
        # Store the field used to keep related records on the same partition.
        self._key_field = key_field
        # Always require the configured identity and key, even if a caller omits
        # them from its additional schema tuple. dict.fromkeys removes duplicates.
        self._required_event_fields = tuple(
            dict.fromkeys((*required_event_fields, event_id_field, key_field))
        )
        # Copy caller configuration so later external mutation cannot change our view.
        self._config = dict(config if config is not None else build_callback_producer_config())
        # Construct one client and reuse its connections, background work, and batches.
        self._client: KafkaProducerLike = producer_factory(self._config)
        # False means publish/poll are allowed; finish() changes it exactly once.
        self._finished = False

        # Count messages accepted into the local queue, not delivery successes.
        self._queued_count = 0
        # Count callbacks where Kafka reported acknowledgement under the acks policy.
        self._delivered_count = 0
        # Count callbacks where the producer reported a final delivery failure.
        self._failed_count = 0
        # Store only compact event-id/error pairs, never complete business payloads.
        self._recent_failures: deque[DeliveryFailure] = deque(maxlen=failure_sample_size)

    def _build_delivery_callback(self, event_id: str):
        """Return a fast callback that remembers its original business event id.

        The nested function is a closure: it remembers ``event_id`` after this
        outer method returns. Callbacks update counters and logs only; blocking
        database calls or retries would delay every other ready callback.
        """

        # The Kafka client calls this function later from poll() or flush().
        def on_delivery(delivery_error, delivered_message) -> None:
            # None is the Kafka client convention for successful delivery.
            if delivery_error is None:
                # Increment only inside the success branch so outcomes remain distinct.
                self._delivered_count += 1
                # Log correlation and Kafka position without logging sensitive payloads.
                logger.debug(
                    "Kafka acknowledged event_id=%s topic=%s partition=%s offset=%s",
                    event_id,
                    delivered_message.topic(),
                    delivered_message.partition(),
                    delivered_message.offset(),
                )
                # Stop here so the failure branch cannot also count this message.
                return

            # A non-None error is a final producer delivery failure, not BufferError.
            self._failed_count += 1
            # Retain a small immutable diagnostic record for the shutdown summary.
            self._recent_failures.append(
                DeliveryFailure(event_id=event_id, error_message=str(delivery_error))
            )
            # Operators need the event id for tracing, but the payload stays out of logs.
            logger.error(
                "Kafka delivery failed for event_id=%s: %s",
                event_id,
                delivery_error,
            )

        # Return the closure so produce() can register it for this one event.
        return on_delivery

    def publish(self, event: JsonObject) -> str:
        """Queue one event and return its id after local acceptance.

        Returning an event id is not a delivery receipt. The final outcome is
        counted later when poll() or finish() serves the delivery callback.
        """
        # Publishing after finish would target a lifecycle that is already closed.
        if self._finished:
            raise RuntimeError("Cannot publish after finish().")
        # Require an object because field validation and correlation need named keys.
        if not isinstance(event, dict):
            raise ValueError("event must be a JSON object.")

        # Collect all missing fields so the caller can repair the event in one pass.
        missing_fields = [
            field for field in self._required_event_fields if field not in event
        ]
        # An empty list means all required fields are present.
        if missing_fields:
            raise ValueError(f"event is missing required fields: {missing_fields}")

        # Convert ids to text so logs, callbacks, and Kafka keys use stable forms.
        event_id = str(event[self._event_id_field]).strip()
        message_key_text = str(event[self._key_field]).strip()
        # Present-but-empty identifiers are as unusable as missing identifiers.
        if not event_id or not message_key_text:
            raise ValueError(
                f"{self._event_id_field} and {self._key_field} must not be blank."
            )

        # JSON serialization is the boundary from application objects to wire bytes.
        try:
            message_value = json.dumps(
                event,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        # Unsupported objects are caller/schema errors; retrying Kafka cannot fix them.
        except (TypeError, ValueError) as error:
            raise ValueError(f"event is not JSON-serializable: {error}") from error

        # The business order id becomes bytes used for Kafka partition selection.
        message_key = message_key_text.encode("utf-8")
        # This callback closure remembers which event the later result belongs to.
        delivery_callback = self._build_delivery_callback(event_id)
        # The deadline bounds all local queue-full retries for this event.
        deadline = time.monotonic() + self._queue_capacity_wait_seconds

        # Repeat only when BufferError says this event was not accepted locally.
        while True:
            try:
                # Stage 2.0: on_delivery is the single argument that makes this
                # callback-confirmed rather than fire-and-forget.
                self._client.produce(
                    topic=self._topic,
                    key=message_key,
                    value=message_value,
                    on_delivery=delivery_callback,
                )
                # Increment only after produce() accepts the message locally.
                self._queued_count += 1
                # Return correlation identity, not a claim of Kafka delivery.
                return event_id
            # BufferError means produce() rejected this attempt before acceptance.
            except BufferError:
                # Calculate how much of the total queue-wait budget remains.
                remaining_seconds = deadline - time.monotonic()
                # Stop rather than retry forever when the caller's deadline expires.
                if remaining_seconds <= 0:
                    raise TimeoutError(
                        f"Kafka local queue remained full for event_id={event_id}."
                    ) from None
                # Service events for one short slice, then retry this same unaccepted event.
                self._client.poll(
                    min(QUEUE_CAPACITY_POLL_INTERVAL_SECONDS, remaining_seconds)
                )

    def poll(self, timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS) -> int:
        """Run ready callbacks, waiting at most ``timeout_seconds``.

        A timeout of zero is non-blocking: process what is ready now and return.
        Networking already happens in the client's background work; poll serves
        application callbacks and other queued client events.
        """
        # A negative duration has no useful, portable meaning for this API.
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be zero or greater.")
        # After finish, summary counters must remain a stable final snapshot.
        if self._finished:
            raise RuntimeError("Cannot poll after finish().")
        # Return the underlying count of client events that were served.
        return self._client.poll(timeout_seconds)

    def finish(self, timeout_seconds: float = DEFAULT_FLUSH_TIMEOUT_SECONDS) -> DeliverySummary:
        """Drain queued work once and return an honest producer-delivery summary.

        ``producer_remaining == 0`` means no work remains in the client, but it
        does not prove success: failed callbacks also finish work. Therefore
        callers should use ``summary.all_confirmed`` rather than checking flush
        output alone.
        """
        # Validate before changing lifecycle state so caller mistakes are repairable.
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be zero or greater.")
        # A second finish would create two competing definitions of final state.
        if self._finished:
            raise RuntimeError("finish() has already been called.")
        # Block all later publish/poll calls before entering the shutdown wait.
        self._finished = True

        # flush serves callbacks while waiting and returns unresolved client work.
        producer_remaining = self._client.flush(timeout_seconds)
        # Accepted messages without success/failure callbacks remain unconfirmed.
        awaiting_callback = (
            self._queued_count - self._delivered_count - self._failed_count
        )

        # Freeze current counters and failure samples into an immutable result.
        return DeliverySummary(
            queued=self._queued_count,
            delivered=self._delivered_count,
            failed=self._failed_count,
            awaiting_callback=awaiting_callback,
            producer_remaining=producer_remaining,
            recent_failures=tuple(self._recent_failures),
        )
