"""
Shared contracts and result models for the callback-confirmed producer package.

Why this module exists:
- keep interfaces explicit between core workflow logic and Kafka client adapters
- make tests independent from live infrastructure through protocol-based fakes
- provide stable, structured result objects for operations and troubleshooting

ASCII flow:
    produce_message(...)
         |
         v
    KafkaReadinessReport (preflight state)
         |
         v
    ProduceMessageResult (send outcome)
         |
         v
    DemoRoundTripResult (optional produce+consume teaching flow)
"""

# Lets us write type hints like `-> int` without Python evaluating them the
# moment this file loads. Without this, some hints below would fail to import.
from __future__ import annotations

# Dataclasses provide immutable, typed operation outcomes for logging and tests.
from dataclasses import dataclass

# Any/Callable/Dict/Optional: plain type-hint building blocks.
# Protocol: lets us describe "any object with these methods" as a type,
# without that object needing to inherit from a shared base class. See the
# class below for what that means in practice.
# TypeAlias: lets us give a short, readable name (like `JsonValue`) to a type
# hint instead of spelling out the full definition everywhere it is used.
from typing import Any, Callable, Dict, Optional, Protocol, TypeAlias


class KafkaProducerLike(Protocol):
    """
    Minimal producer contract used by workflow code.

    This interface matches only the capabilities needed by the simple producer
    flow so both Confluent and kafka-python backends can be used interchangeably.

    Why `Protocol` instead of a normal base class: `confluent_kafka.Producer`
    and our `KafkaPythonProducerAdapter` do not share a parent class. `Protocol`
    says "any object with a matching produce/poll/flush method counts as this
    type," so both can be used here without either one inheriting from the other.
    """

    def produce(self, topic: str, **kwargs: Any) -> None:
        """Queue one message with either callback/on_delivery keyword style."""

    def poll(self, timeout: float) -> int:
        """
        Serve producer callbacks and network events.

        One-shot scripts call this so delivery callbacks can run before process exit.
        """

    def flush(self, timeout: float) -> int:
        """Wait for queued messages to drain and return remaining queue size."""


class KafkaAdminClientLike(Protocol):
    """
    Minimal metadata contract used by preflight readiness checks.

    A narrow contract keeps tests simple and allows multiple admin client backends.
    """

    def list_topics(self, timeout: float) -> Any:
        """Fetch cluster metadata used to verify broker and topic availability."""


# JSON values are recursive: lists and objects may contain more JSON values.
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

# Kafka configuration is the one intentional Any boundary because the external
# library owns many settings whose values have different types.
ProducerConfig: TypeAlias = dict[str, Any]
# A delivery callback receives an optional error and client-owned message metadata.
DeliveryCallback = Callable[[Optional[Exception], Any], None]
# Factory means a function whose job is to construct an object.
ProducerFactory = Callable[[ProducerConfig], KafkaProducerLike]
AdminClientFactory = Callable[[ProducerConfig], KafkaAdminClientLike]
ConfigLoader = Callable[..., ProducerConfig]


# frozen=True: once created, this object's fields cannot be changed. This
# stops any code from accidentally editing a result after it was returned.
# slots=True: this class will only ever have the exact fields listed below.
# It skips Python's normal per-object storage dictionary, making instances
# smaller and a bit faster to create. The tradeoff is that no new attribute
# can be added to an instance later, which is fine here since nothing does.
@dataclass(frozen=True, slots=True)
class DeliveryFailure:
    """Small diagnostic record for one final delivery failure.

    Payload data is intentionally absent so failure sampling cannot retain
    secrets or large business events in memory.
    """

    # Business identifier used to trace the original event safely.
    event_id: str
    # Human-readable client error; application decisions should use summary counts.
    error_message: str


# Same frozen/slots reasoning as `DeliveryFailure` above: immutable, and no
# extra attributes beyond the ones listed.
@dataclass(frozen=True, slots=True)
class DeliverySummary:
    """Immutable final accounting for one publisher instance.

    Queued, delivered, and failed are deliberately separate states. A message
    may also remain awaiting a callback when shutdown times out. This prevents
    callers from incorrectly treating ``flush() == 0`` as proof of success.
    """

    # Number of events accepted into the local producer queue.
    queued: int
    # Number of callbacks reporting Kafka acknowledgement.
    delivered: int
    # Number of callbacks reporting final producer delivery failure.
    failed: int
    # Accepted events for which this application saw no final callback.
    awaiting_callback: int
    # Work the Kafka client still reported after the bounded flush.
    producer_remaining: int
    # Bounded diagnostic examples; successful payloads are never retained.
    recent_failures: tuple[DeliveryFailure, ...] = ()

    @property
    def all_confirmed(self) -> bool:
        """Return true only when every accepted event was acknowledged."""
        # Check all counters instead of assuming an empty client queue means success.
        return (
            self.delivered == self.queued
            and self.failed == 0
            and self.awaiting_callback == 0
            and self.producer_remaining == 0
        )


@dataclass(frozen=True)
class KafkaReadinessReport:
    """
    Readiness decision returned before any send attempt.

    Fields:
    - ready: True only when brokers are reachable and the topic metadata is valid.
    - bootstrap_servers: bootstrap endpoint string used for metadata lookups.
    - broker_count: number of brokers visible in metadata.
    - topic_name/topic_exists/topic_partition_count: topic-level verification details.
    - error_message: actionable failure explanation when ready is False, or an
      informational note when `ready=True` only because the check was skipped.
    """

    ready: bool
    bootstrap_servers: str
    broker_count: int
    topic_name: str
    topic_exists: bool
    topic_partition_count: int
    error_message: Optional[str] = None


@dataclass(frozen=True)
class ProduceMessageResult:
    """
    End-to-end outcome for one produce request.

    Fields capture both business-relevant status (`success`) and operational
    troubleshooting context (`flush_remaining_messages`, nested readiness details).
    """

    topic_name: str
    message_key: Optional[str]
    success: bool
    serialized_size_bytes: int
    flush_remaining_messages: int
    readiness: KafkaReadinessReport
    error_message: Optional[str] = None


@dataclass(frozen=True)
class ConsumedEventVerification:
    """
    Consume-back verification result used by the teaching demo.

    This object distinguishes "produce succeeded but consume check timed out"
    from "end-to-end round trip verified".
    """

    consumed: bool
    topic_name: str
    expected_demo_run_id: str
    received_event: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None


@dataclass(frozen=True)
class DemoRoundTripResult:
    """
    Combined result for the demo's produce and consume verification phases.

    Keeping both sub-results preserves clear operator context for partial failures.
    """

    produce_result: ProduceMessageResult
    consume_verification: ConsumedEventVerification
