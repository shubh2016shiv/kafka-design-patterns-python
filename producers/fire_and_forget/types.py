"""
Shared contracts and result models for the fire-and-forget producer package.

Why this module exists:
- make the producer contract explicit so tests can inject fakes without a live broker
- provide immutable, typed result objects so callers log and branch on fields, not strings
- keep core.py free of structural type definitions so each layer stays focused

ASCII contract map:

    Application code
          |
          v
    FireAndForgetProducer  ──uses──>  KafkaProducerLike (Protocol)
          |                                    |
          v                                    v
    SendResult / RetryResult          confluent_kafka.Producer
    ProducerHealthMetrics             or KafkaPythonProducerAdapter (test/fallback)
"""

from __future__ import annotations

# dataclass: lightweight immutable value objects — no Pydantic needed for
# internal results that never cross a serialization boundary.
from dataclasses import dataclass

# Protocol: structural typing lets tests inject a FakeProducer without
# inheriting from the real confluent_kafka.Producer C extension.
# Callable, Optional, Dict: explicit contracts for callbacks and config maps.
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple


# ---------------------------------------------------------------------------
# Protocol — minimal Kafka producer contract
# ---------------------------------------------------------------------------


class KafkaProducerLike(Protocol):
    """
    Structural interface required by FireAndForgetProducer.

    Why Protocol instead of ABC:
    - confluent_kafka.Producer is a C extension and cannot be subclassed easily.
    - Protocol lets FakeProducer in tests satisfy the contract purely through
      duck typing, with no real Kafka dependency at all.

    Why these three methods specifically:
    - produce() — the only way to enqueue a message; always non-blocking.
    - poll()    — gives librdkafka CPU time to fire queued delivery callbacks.
    - flush()   — blocks until all messages are delivered or timeout is reached;
                  needed for graceful shutdown and optional confirmation mode.
    """

    def produce(
        self,
        topic: str,
        value: bytes,
        key: Optional[bytes] = None,
        headers: Optional[List[Tuple[str, bytes]]] = None,
        callback: Optional[Callable[..., None]] = None,
        timestamp: Optional[int] = None,
    ) -> None:
        """
        Enqueue one message for asynchronous delivery.

        Input contract: topic, key, value are already validated and serialized
        by core logic before this call.
        Output contract: returns immediately — does NOT block for broker ack.
        """

    def poll(self, timeout: float) -> int:
        """
        Serve pending delivery callbacks from librdkafka's internal event queue.

        Why call this after produce():
        - librdkafka accumulates delivery events internally.
        - Without poll(), those callbacks never execute in the application thread.
        - poll(0) is non-blocking: fires only callbacks that are already ready.

        Returns the number of events processed (informational, not the queue depth).
        """

    def flush(self, timeout: float) -> int:
        """
        Block until all enqueued messages drain or timeout expires.

        Returns remaining queue depth — non-zero means potential message loss
        if the process exits immediately after this call.
        """


# ---------------------------------------------------------------------------
# Type aliases — named contracts shared across the package
# ---------------------------------------------------------------------------

# Delivery callback signature: (err_or_None, msg_or_None) → None.
# Both confluent_kafka and the kafka-python adapter use this shape.
DeliveryCallback = Callable[[Optional[Any], Any], None]

# Injectable factory used by core.py; tests supply a fake factory so no
# real broker connection is ever opened during unit test runs.
ProducerFactory = Callable[[Dict[str, Any]], KafkaProducerLike]

# Injectable config loader; tests supply a minimal dict, real code calls
# get_producer_config() from config.kafka_config.
ConfigLoader = Callable[..., Dict[str, Any]]


# ---------------------------------------------------------------------------
# Result dataclasses — immutable, typed operation outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SendResult:
    """
    Outcome of a single send() call.

    Why structured instead of bool/exception:
    - lets callers inspect topic and key alongside success/failure without
      parsing log strings
    - makes test assertions explicit and avoids try/except boilerplate

    Fields:
    - topic: destination Kafka topic.
    - message_key: routing key used for partition assignment.
      None means the broker assigns a partition using round-robin.
    - success: True when the message was enqueued locally without error.
      Important: this is NOT a broker acknowledgement unless
      require_delivery_confirmation=True was passed to send().
    - error_message: human-readable failure reason when success is False.
    """

    topic: str
    message_key: Optional[str]
    success: bool
    error_message: Optional[str] = None


@dataclass(frozen=True)
class RetryResult:
    """
    Outcome of a send_with_retry() call.

    Why separate from SendResult:
    - retry introduces the concept of 'how many attempts were made' which has
      no meaning in a single send.
    - distinguishing "succeeded on first try" from "succeeded on attempt 3"
      matters for SLA tracking and alerting.

    Fields:
    - topic: destination Kafka topic.
    - message_key: partitioning key (None = round-robin).
    - attempts_made: 1 = first attempt succeeded; N = retried N-1 times.
    - success: True when any attempt enqueued successfully.
    - final_error_message: last error seen; populated only when success is False.
    """

    topic: str
    message_key: Optional[str]
    attempts_made: int
    success: bool
    final_error_message: Optional[str] = None


@dataclass(frozen=True)
class ProducerHealthMetrics:
    """
    Snapshot of the producer's cumulative operational health.

    Why this exists:
    - exposes producer state for monitoring dashboards and interview demonstrations
    - is_healthy drives the guard in send() so a degraded producer cannot silently
      accept new messages; callers must reset the instance or investigate first

    Fields:
    - is_healthy: False when error_rate exceeds HEALTH_ERROR_RATE_THRESHOLD.
      Stays False until reset_instance() is called — intentionally conservative.
    - messages_sent: cumulative successfully-delivered message count.
    - error_count: cumulative delivery failure count.
    - error_rate: error_count / max(messages_sent + error_count, 1), i.e. the
      share of completed delivery callbacks that failed.
    - sanitized_config: producer config dict with credential keys redacted;
      safe to include in logs or structured health responses.
    """

    is_healthy: bool
    messages_sent: int
    error_count: int
    error_rate: float
    sanitized_config: Dict[str, Any]
