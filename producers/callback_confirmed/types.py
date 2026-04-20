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

from __future__ import annotations

# Dataclasses provide immutable, typed operation outcomes for logging and tests.
from dataclasses import dataclass

# Protocol keeps runtime dependencies swappable while preserving static contracts.
from typing import Any, Callable, Dict, Optional, Protocol


class KafkaProducerLike(Protocol):
    """
    Minimal producer contract used by workflow code.

    This interface matches only the capabilities needed by the simple producer
    flow so both Confluent and kafka-python backends can be used interchangeably.
    """

    def produce(
        self, topic: str, key: Optional[str], value: bytes, callback: Callable[..., None]
    ) -> None:
        """Queue one message for asynchronous delivery."""

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


DeliveryCallback = Callable[[Optional[Exception], Any], None]
ProducerFactory = Callable[[Dict[str, Any]], KafkaProducerLike]
AdminClientFactory = Callable[[Dict[str, Any]], KafkaAdminClientLike]
ConfigLoader = Callable[..., Dict[str, Any]]


@dataclass(frozen=True)
class KafkaReadinessReport:
    """
    Readiness decision returned before any send attempt.

    Fields:
    - ready: True only when brokers are reachable and the topic metadata is valid.
    - bootstrap_servers: bootstrap endpoint string used for metadata lookups.
    - broker_count: number of brokers visible in metadata.
    - topic_name/topic_exists/topic_partition_count: topic-level verification details.
    - error_message: actionable failure explanation when ready is False.
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
