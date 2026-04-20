"""
Public API for the `producers.dead_letter_queue` package.

Pattern: Dead Letter Queue (DLQ) Producer
  Combines four fault-tolerance mechanisms into one cohesive send path:

    Circuit Breaker → Bulkhead → Retry/Exponential Backoff → Dead Letter Queue

Why use this over a plain producer:
  - Messages that fail all retries are PRESERVED in a DLQ topic for replay
    rather than silently dropped.
  - The circuit breaker fast-fails against a known-bad broker, preventing
    cascade failures across dependent services.
  - The bulkhead caps concurrent in-flight sends to protect application threads.
  - The sliding-window health monitor gives operators a live error-rate signal.

Typical usage:
    from producers.dead_letter_queue import (
        create_dlq_producer,
        create_ha_dlq_producer,
        SendAttemptResult,
    )

    producer = create_dlq_producer("payments")
    result: SendAttemptResult = producer.send_with_fault_tolerance(
        topic="payments-high",
        data={"transaction_id": "txn_001", "amount_cents": 9900},
    )
    if result.success:
        print(f"Delivered in {result.retry_count} retries")
    else:
        print(f"Routed to DLQ — circuit: {result.circuit_state.value}")
        print(f"Error: {result.error}")

For HA workloads (payments, auth, healthcare):
    producer = create_ha_dlq_producer("payments")
"""

# ── Core classes — for composition, sub-classing, or direct instantiation ─────
from .core import (
    Bulkhead,
    CircuitBreaker,
    DeadLetterQueueProducer,
    SlidingWindowHealthMonitor,
    create_dlq_producer,
    create_ha_dlq_producer,
)

# ── Configuration and result contracts ────────────────────────────────────────
from .types import (
    CircuitState,
    FaultToleranceConfig,
    RoutingProducerProtocol,
    SendAttemptResult,
    UnderlyingProducerProtocol,
)

# ── Constants — for downstream code that builds DLQ topic names ───────────────
from .constants import DLQ_TOPIC_SUFFIX

__all__ = [
    # Primary entry points
    "create_dlq_producer",
    "create_ha_dlq_producer",
    # Main producer class
    "DeadLetterQueueProducer",
    # Sub-components (useful for testing, composition, or custom wiring)
    "CircuitBreaker",
    "Bulkhead",
    "SlidingWindowHealthMonitor",
    # Type contracts
    "CircuitState",
    "FaultToleranceConfig",
    "SendAttemptResult",
    "UnderlyingProducerProtocol",
    "RoutingProducerProtocol",
    # Constants
    "DLQ_TOPIC_SUFFIX",
]
