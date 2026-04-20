"""
Type contracts for the Dead Letter Queue (DLQ) producer pattern.

Why this module exists:
- centralise all dataclasses, enums, and Protocol stubs so core.py stays
  focused on workflow logic
- expose the contract surface as the first thing a reader opens — understanding
  the types IS understanding the pattern
- support static analysis and test-stub injection via Protocol

Naming reference:
  Enums and dataclasses here use Kafka/resilience literature names so a reader
  can cross-reference against:
    - Apache Kafka: The Definitive Guide (Gwen Shapira et al.)
    - "Release It!" by Michael Nygard  (circuit breaker / bulkhead patterns)
    - Confluent documentation on fault-tolerant producers and DLQ topic patterns
"""

from __future__ import annotations

# dataclass — zero-boilerplate mutable value objects; used for config and results.
from dataclasses import dataclass

# Enum — finite named states replace magic strings and prevent invalid state values.
from enum import Enum

# Protocol + runtime_checkable — structural duck-typing for injected dependencies;
# lets tests pass fakes without importing confluent_kafka at all.
from typing import Any, Dict, Optional, Protocol, runtime_checkable


# ── Circuit-breaker state machine ─────────────────────────────────────────────


class CircuitState(Enum):
    """
    The three canonical circuit-breaker states from Nygard's "Release It!".

    State-machine transitions:
        CLOSED    ──[failure_threshold failures]──► OPEN
        OPEN      ──[recovery_timeout elapses]────► HALF_OPEN
        HALF_OPEN ──[success_threshold successes]─► CLOSED
        HALF_OPEN ──[any failure]────────────────► OPEN

    CLOSED
        Normal operation.  Every send attempt passes through to the broker.
        Failure count increments on error; resets gradually on success.

    OPEN
        Fast-fail mode.  Calls are rejected before touching the network.
        This prevents a struggling broker from being flooded with retries
        while it is trying to recover.

    HALF_OPEN
        Recovery probe.  A limited number of calls are allowed through to
        test whether the broker has stabilised.  The circuit closes on
        enough consecutive successes, or re-opens on any failure.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ── Configuration contract ────────────────────────────────────────────────────


@dataclass
class FaultToleranceConfig:
    """
    Tuneable parameters for every fault-tolerance mechanism in this pattern.

    Why one config object instead of scattered keyword arguments:
    - constructor signature stays stable across refactors
    - tuning for a specific environment (e.g. HIGH_AVAILABILITY) is one swap
    - documents the production-tuning rationale for every parameter in one place

    Defaults are conservative: they fail safe and recover slowly rather than
    flooding a struggling broker with aggressive retries.

    Production tuning guide:
    - Payment / healthcare pipelines : lower failure_threshold (2–3),
      raise max_retries (5+), keep DLQ always enabled.
    - High-throughput analytics       : raise max_concurrent_sends (500+),
      lower success_threshold (1) for faster circuit recovery.
    - Batch ETL                       : increase retry delays to avoid hammering
      a broker that is mid-restart.
    """

    # ── Circuit Breaker ───────────────────────────────────────────────────────

    failure_threshold: int = 5
    """Consecutive failures that trip the circuit from CLOSED to OPEN.
    Lower = faster failure detection but noisier on flaky networks."""

    recovery_timeout_seconds: int = 60
    """Seconds the circuit stays OPEN before probing recovery (HALF_OPEN).
    Trade-off: too low = premature retries; too high = extended outage window."""

    success_threshold: int = 3
    """Consecutive successes in HALF_OPEN needed to restore CLOSED state.
    Prevents a single fluke success from prematurely declaring the broker healthy."""

    # ── Retry / Exponential Backoff ───────────────────────────────────────────

    max_retries: int = 3
    """Attempts after the first failure before declaring the send failed
    and routing the message to the Dead Letter Queue."""

    initial_retry_delay_seconds: float = 1.0
    """Starting wait between retry attempts.  Multiplied by retry_backoff_multiplier
    each round.  Set low enough to catch transient blips without stalling."""

    max_retry_delay_seconds: float = 30.0
    """Cap on inter-retry wait — prevents multi-minute stalls under sustained load."""

    retry_backoff_multiplier: float = 2.0
    """Exponential growth factor.  delay_n = min(initial * multiplier^n, max_delay)."""

    # ── Bulkhead ──────────────────────────────────────────────────────────────

    max_concurrent_sends: int = 100
    """Maximum in-flight send operations allowed simultaneously.
    Prevents one slow broker from exhausting all application threads."""

    send_timeout_seconds: float = 30.0
    """Maximum wait to acquire a bulkhead slot.  Should be less than the
    caller's own SLA timeout so the caller can react before its deadline."""

    # ── Sliding-Window Health Monitor ─────────────────────────────────────────

    health_window_size: int = 100
    """Number of recent operations tracked for error-rate computation.
    Larger window = smoother signal; smaller window = faster reaction to spikes."""

    error_rate_unhealthy_threshold: float = 0.10
    """Error rate (0.0–1.0) above which the producer is marked UNHEALTHY.
    0.10 = 10 %; a conservative default matching typical Kafka SLA expectations."""


# ── Result contract ───────────────────────────────────────────────────────────


@dataclass
class SendAttemptResult:
    """
    Structured outcome of one send attempt through the DLQ producer.

    Why a result object instead of raising exceptions on failure:
    - callers can branch on success/circuit_state/retry_count without catching
      multiple unrelated exception types
    - retry_count and execution_time_seconds provide observability without
      requiring an external metrics system
    - mirrors ProduceMessageResult from callback_confirmed for consistency

    Failure contract:
    - success=False means the message was exhausted through all retries and
      routed to the Dead Letter Queue topic (if DLQ is enabled on the producer)
    - error holds the last exception seen across all retry attempts
    - circuit_state reflects the breaker state AFTER this attempt was recorded
    """

    success: bool
    error: Optional[Exception] = None
    execution_time_seconds: float = 0.0
    retry_count: int = 0
    circuit_state: CircuitState = CircuitState.CLOSED


# ── Injectable dependency protocols (for unit-test isolation) ─────────────────


@runtime_checkable
class UnderlyingProducerProtocol(Protocol):
    """
    Minimal interface expected from the shared Kafka producer dependency.

    Why a Protocol instead of importing SingletonProducer directly:
    - unit tests inject a fake without pulling in confluent_kafka
    - decouples this package from the singleton/ implementation detail
    - any object that has send() and flush() satisfies this contract

    Used by DeadLetterQueueProducer for direct DLQ topic writes that bypass
    the routing layer to avoid infinite retry loops.
    """

    def send(self, topic: str, data: Dict[str, Any], **kwargs: Any) -> None: ...

    def flush(self, timeout: float) -> int: ...


@runtime_checkable
class RoutingProducerProtocol(Protocol):
    """
    Minimal interface expected from the topic-routing producer dependency.

    Allows tests and callers to substitute any routing strategy without
    changing DeadLetterQueueProducer internals.

    Used for all normal (non-DLQ) sends so callers don't hard-code topic names.
    """

    def send_to_topic(self, topic: str, data: Dict[str, Any], **kwargs: Any) -> None: ...

    def send_with_metadata(self, data: Dict[str, Any], metadata: Any, **kwargs: Any) -> None: ...
