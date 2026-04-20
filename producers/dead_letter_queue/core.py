"""
Core workflow for the Dead Letter Queue (DLQ) producer pattern.

Why this pattern exists:
  In distributed systems, transient broker failures are inevitable.  Without
  fault tolerance, a single broker restart can drop messages and cascade failures
  across dependent services.  This module combines four safety layers so that:

    1. Transient failures are retried automatically
       (Retry + Exponential Backoff).
    2. Sustained failures fail fast instead of queuing more work
       (Circuit Breaker).
    3. Resource starvation is isolated from the rest of the application
       (Bulkhead).
    4. Messages that exhaust all retries are preserved for later replay
       (Dead Letter Queue).

Pattern references:
  - Dead Letter Queue (DLQ)    : Apache Kafka docs, Confluent Platform docs,
                                 "Enterprise Integration Patterns" (Hohpe & Woolf)
  - Circuit Breaker            : Michael Nygard, "Release It!" (2nd ed., ch. 5)
  - Bulkhead                   : Nygard ibid.  Named after ship-compartment isolation.
  - Sliding-window health      : Netflix Hystrix design; Resilience4j CircuitBreaker

ASCII flow — send_with_fault_tolerance() full decision path:
─────────────────────────────────────────────────────────────

    caller.send_with_fault_tolerance(topic, data)
          │
          ▼
    ┌─────────────────────────────────────────┐
    │ Stage 1.0  Circuit Breaker gate         │
    │  OPEN? ──► fast-fail result (no network)│
    │  CLOSED / HALF_OPEN? ──► continue       │
    └─────────────────────────────────────────┘
          │
          ▼
    ┌─────────────────────────────────────────┐
    │ Stage 2.0  Bulkhead slot acquisition    │
    │  full / timeout? ──► resource-fail result│
    │  acquired? ──► continue                 │
    └─────────────────────────────────────────┘
          │
          ▼
    ┌─────────────────────────────────────────┐
    │ Stage 3.0  Retry loop                   │
    │  ┌─────────────────────────────────────┐│
    │  │ Stage 3.1  invoke routing producer  ││
    │  │ Stage 3.2  success? ──► exit loop   ││
    │  │ Stage 3.3  failure? ──► backoff,    ││
    │  │            retry or give up         ││
    │  └─────────────────────────────────────┘│
    └─────────────────────────────────────────┘
          │
          ▼
    ┌─────────────────────────────────────────┐
    │ Stage 4.0  Record outcome               │
    │  success? ──► pybreaker protected-call success│
    │  failure? ──► pybreaker protected-call failure│
    │              + Stage 4.1 DLQ send       │
    └─────────────────────────────────────────┘
          │
          ▼
    Stage 5.0  Release bulkhead slot  (always, via finally)
          │
          ▼
    Stage 6.0  Return SendAttemptResult to caller

─────────────────────────────────────────────────────────────
"""

from __future__ import annotations

# json — DLQ envelope serialisation for the failed-message payload.

# datetime — pybreaker stores opened timestamps as UTC datetimes; used for probe gating.
from datetime import datetime, timezone

# logging — structured operational log lines at each stage transition.
import logging

# threading — Lock and Semaphore for thread-safe circuit-breaker and bulkhead state.
import threading

# time — wall-clock timestamps for recovery timeouts and execution-time measurement.
import time

# collections.deque — fixed-size sliding window for health monitoring;
# automatically evicts oldest entries when maxlen is reached.
from collections import deque

# typing — explicit contracts on every public method and helper.
from typing import Any, Callable, Dict, Optional

# pybreaker — battle-tested circuit-breaker state machine and failure counting.
from pybreaker import (
    CircuitBreaker as PyBreakerCircuitBreaker,
    CircuitBreakerError,
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN,
)

# tenacity — battle-tested retry orchestration with configurable exponential backoff.
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from .clients import default_routing_producer_factory, default_underlying_producer_factory
from .constants import (
    DLQ_TOPIC_SUFFIX,
    HA_ERROR_RATE_UNHEALTHY_THRESHOLD,
    HA_FAILURE_THRESHOLD,
    HA_MAX_CONCURRENT_SENDS,
    HA_MAX_RETRIES,
    HA_RECOVERY_TIMEOUT_SECONDS,
)
from .types import (
    CircuitState,
    FaultToleranceConfig,
    RoutingProducerProtocol,
    SendAttemptResult,
    UnderlyingProducerProtocol,
)

# Environment import is used for safe factory wiring defaults.
try:
    from config.kafka_config import Environment
except ImportError:  # pragma: no cover - package install fallback.
    from kafka.config.kafka_config import Environment

logger = logging.getLogger(__name__)


_PYBREAKER_STATE_TO_CIRCUIT_STATE: dict[str, CircuitState] = {
    STATE_CLOSED: CircuitState.CLOSED,
    STATE_OPEN: CircuitState.OPEN,
    STATE_HALF_OPEN: CircuitState.HALF_OPEN,
    # Defensive normalization in case upstream emits hyphenated names.
    "half-open": CircuitState.HALF_OPEN,
}


# ── Circuit Breaker ────────────────────────────────────────────────────────────


class CircuitBreaker:
    """
    Compatibility wrapper around ``pybreaker.CircuitBreaker``.

    Why this wrapper exists:
    - Preserve this package's public API (`state`, `failure_count`, `can_execute`,
      `reset`) while using a battle-tested circuit implementation internally.
    - Map pybreaker runtime state names to the local `CircuitState` enum so
      `SendAttemptResult` and `health_status()` stay backward compatible.

    Main responsibility boundaries:
    - pybreaker tracks failures, successes, and OPEN/HALF_OPEN/CLOSED transitions
      during protected calls (`call(...)`).
    - this wrapper provides a pre-check gate (`can_execute`) used before bulkhead
      acquisition to fast-fail callers while the circuit is OPEN and still in the
      blackout window.
    """

    def __init__(self, config: FaultToleranceConfig) -> None:
        """
        Input:
          config.failure_threshold        -> pybreaker fail_max
          config.recovery_timeout_seconds -> pybreaker reset_timeout
          config.success_threshold        -> pybreaker success_threshold

        Output:
          A wrapper over a pybreaker instance initialized in CLOSED state.
        """
        self._config = config
        self._lock = threading.Lock()
        self._breaker = PyBreakerCircuitBreaker(
            fail_max=config.failure_threshold,
            reset_timeout=config.recovery_timeout_seconds,
            success_threshold=config.success_threshold,
        )
        logger.info(
            "CircuitBreaker initialised (pybreaker) - failure_threshold=%d, recovery_timeout=%ds",
            config.failure_threshold,
            config.recovery_timeout_seconds,
        )

    # ── Read-only properties ───────────────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        """
        Current circuit state mapped from pybreaker to local enum values.

        Failure behavior:
        - Unknown upstream state values are treated as OPEN (fail-safe).
        """
        pybreaker_state = self._breaker.current_state
        return _PYBREAKER_STATE_TO_CIRCUIT_STATE.get(pybreaker_state, CircuitState.OPEN)

    @property
    def failure_count(self) -> int:
        """Current pybreaker failure counter for visibility and health reporting."""
        return int(self._breaker.fail_counter)

    # ── State machine ──────────────────────────────────────────────────────────

    def can_execute(self) -> bool:
        """
        Return True if the caller is allowed to attempt a Kafka send.

        Output:
          False for OPEN circuits while blackout window is still active.
          True for CLOSED/HALF_OPEN states, and for OPEN when timeout elapsed
          (allowing the upcoming protected call to become the probe).

        Failure behavior:
          Returns False instead of raising so callers can return a structured
          SendAttemptResult fast-fail.
        """
        with self._lock:
            if self.state is not CircuitState.OPEN:
                return True

            elapsed_seconds = self._seconds_since_open()
            if elapsed_seconds is None:
                return False

            return elapsed_seconds >= float(self._config.recovery_timeout_seconds)

    def call(self, protected_call: Callable[[], Any]) -> Any:
        """
        Execute one protected operation through pybreaker.

        Input:
          protected_call: callback that performs exactly one broker-facing send.

        Output:
          Whatever the callback returns.

        Failure behavior:
          - Raises the callback exception for normal failures.
          - Raises CircuitBreakerError when circuit is OPEN or when threshold trip
            happens on this call.
        """
        return self._breaker.call(protected_call)

    def record_success(self) -> None:
        """
        Compatibility hook for external callers.

        The production send path now records outcomes via `call(...)` directly.
        This method is intentionally a no-op to preserve API shape without
        duplicating state transitions.
        """
        return None

    def record_failure(self) -> None:
        """
        Compatibility hook for external callers.

        The production send path now records outcomes via `call(...)` directly.
        This method is intentionally a no-op to preserve API shape without
        duplicating state transitions.
        """
        return None

    def reset(self) -> None:
        """
        Force-reset to CLOSED state — for administrative / operator use only.

        When to use: after manual broker repair when you want the circuit to
        recover immediately rather than waiting for recovery_timeout_seconds.
        """
        self._breaker.close()
        logger.info("CircuitBreaker manually reset to CLOSED by operator")

    def _seconds_since_open(self) -> Optional[float]:
        """
        Compute elapsed wall-clock seconds since circuit entered OPEN.

        Why private storage access is used:
        pybreaker does not expose a public opened-at timestamp accessor, but it
        does maintain timezone-aware datetime state internally. We read that
        value only for the pre-send gate check.
        """
        opened_at = self._breaker._state_storage.opened_at  # pyright: ignore[reportPrivateUsage]
        if opened_at is None:
            return None
        elapsed = datetime.now(timezone.utc) - opened_at
        return max(0.0, elapsed.total_seconds())


# ── Bulkhead ───────────────────────────────────────────────────────────────────


class Bulkhead:
    """
    Concurrency limiter that isolates this producer's resource usage from the
    rest of the application (the bulkhead pattern).

    Why bulkhead:
    - Without a concurrency cap, a slow broker causes unbounded thread accumulation
      as callers pile up waiting for send to return.  The semaphore ensures at most
      max_concurrent_sends threads are in the send path simultaneously.
    - Named after ship bulkheads that contain flooding within one compartment —
      a Kafka outage should not sink the entire application.

    Implementation: threading.BoundedSemaphore with a timed acquire.
    Failure behavior: acquire() returns False (not raises) when full or timed out,
    so callers receive a structured SendAttemptResult rather than an exception.
    """

    def __init__(self, config: FaultToleranceConfig) -> None:
        self._config = config
        self._semaphore = threading.BoundedSemaphore(config.max_concurrent_sends)
        self._active_count: int = 0
        self._count_lock = threading.Lock()
        logger.info(
            "Bulkhead initialised — max_concurrent_sends=%d",
            config.max_concurrent_sends,
        )

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        Attempt to acquire one bulkhead slot before entering the send path.

        Input:  timeout — maximum wait in seconds; defaults to send_timeout_seconds.
        Output: True if a slot was acquired (caller MUST call release() after send).
                False if the bulkhead is full or the timeout elapsed.
        """
        wait = timeout if timeout is not None else self._config.send_timeout_seconds
        acquired = self._semaphore.acquire(blocking=True, timeout=wait)
        if acquired:
            with self._count_lock:
                self._active_count += 1
        return acquired

    def release(self) -> None:
        """Release a previously acquired bulkhead slot."""
        with self._count_lock:
            self._active_count = max(0, self._active_count - 1)
        self._semaphore.release()

    @property
    def active_count(self) -> int:
        """Number of currently in-flight send operations."""
        with self._count_lock:
            return self._active_count


# ── Sliding-Window Health Monitor ──────────────────────────────────────────────


class SlidingWindowHealthMonitor:
    """
    Tracks error rate and latency of recent send operations using a fixed-size
    sliding window (deque with maxlen).

    Why sliding window instead of cumulative counters:
    - Cumulative error rate dilutes recent spikes — 10,000 old successes can
      hide a current 50 % failure rate.
    - A sliding window (deque with maxlen=N) automatically forgets data older
      than N operations, giving a live picture of current broker health.

    Design reference:
    - Netflix Hystrix health tracking (deprecated, but well-documented).
    - Resilience4j CircuitBreaker COUNT_BASED sliding window.
    """

    def __init__(self, config: FaultToleranceConfig) -> None:
        self._config = config
        # deque with maxlen is the sliding window: oldest entry drops automatically.
        self._window: deque[dict[str, Any]] = deque(maxlen=config.health_window_size)
        self._total_operations: int = 0
        self._total_errors: int = 0
        self._lock = threading.Lock()

    def record_operation(self, success: bool, elapsed_seconds: float) -> None:
        """
        Add one completed operation to the sliding window.

        Input:  success         — whether the send reached the broker.
                elapsed_seconds — wall-clock time for the full send attempt
                                  (including retry delays).
        """
        with self._lock:
            self._window.append(
                {
                    "ts": time.monotonic(),
                    "success": success,
                    "elapsed": elapsed_seconds,
                }
            )
            self._total_operations += 1
            if not success:
                self._total_errors += 1

    @property
    def error_rate(self) -> float:
        """Current error rate over the sliding window (0.0 – 1.0)."""
        with self._lock:
            if not self._window:
                return 0.0
            errors = sum(1 for op in self._window if not op["success"])
            return errors / len(self._window)

    @property
    def avg_latency_seconds(self) -> float:
        """Mean elapsed time per operation over the sliding window."""
        with self._lock:
            if not self._window:
                return 0.0
            return sum(op["elapsed"] for op in self._window) / len(self._window)

    @property
    def is_healthy(self) -> bool:
        """True when the sliding-window error rate is below the configured threshold."""
        return self.error_rate <= self._config.error_rate_unhealthy_threshold

    def get_metrics_snapshot(self) -> Dict[str, Any]:
        """
        Return a point-in-time metrics dict suitable for logging or a dashboard.

        Output keys:
          total_operations, total_errors — cumulative since process start.
          window_error_rate              — error rate over last N operations.
          avg_latency_seconds            — mean latency over last N operations.
          is_healthy                     — bool: error rate below threshold.
          window_sample_count            — how many samples are in the window.
        """
        with self._lock:
            window_sample_count = len(self._window)
            if window_sample_count == 0:
                window_error_rate = 0.0
                avg_latency_seconds = 0.0
            else:
                # Deadlock fix:
                # compute metrics inside one lock scope. Calling properties
                # (`error_rate`, `avg_latency_seconds`) here would re-acquire
                # the same non-reentrant lock and can deadlock monitoring paths.
                error_count = sum(1 for operation in self._window if not operation["success"])
                latency_sum = sum(operation["elapsed"] for operation in self._window)
                window_error_rate = error_count / window_sample_count
                avg_latency_seconds = latency_sum / window_sample_count

            return {
                "total_operations": self._total_operations,
                "total_errors": self._total_errors,
                "window_error_rate": window_error_rate,
                "avg_latency_seconds": avg_latency_seconds,
                "is_healthy": window_error_rate <= self._config.error_rate_unhealthy_threshold,
                "window_sample_count": window_sample_count,
            }


# ── Dead Letter Queue Producer ────────────────────────────────────────────────


class DeadLetterQueueProducer:
    """
    Fault-tolerant Kafka producer that combines Circuit Breaker, Bulkhead,
    Retry with Exponential Backoff, and Dead Letter Queue preservation.

    The four layers and what each protects against:
    ┌──────────────────────────┬────────────────────────────────────────────────┐
    │ Layer                    │ Failure it prevents                            │
    ├──────────────────────────┼────────────────────────────────────────────────┤
    │ Circuit Breaker          │ Cascade: hammering a failing broker            │
    │ Bulkhead                 │ Resource exhaustion under sustained load       │
    │ Retry + Exponential Back │ Silent drop of transient network failures      │
    │ Dead Letter Queue (DLQ)  │ Silent message loss after permanent failure    │
    └──────────────────────────┴────────────────────────────────────────────────┘

    Dependency injection:
    - underlying_producer and routing_producer are injected (not created here)
      so tests can supply fakes without starting a real Kafka broker.
    - Factory functions create_dlq_producer() and create_ha_dlq_producer()
      wire production dependencies when no injection is provided.

    DLQ topic convention:
    - DLQ topic = f"{service_name}{DLQ_TOPIC_SUFFIX}"
      e.g. "payments" → "payments-dead-letter-queue"
    - DLQ sends go through underlying_producer directly (bypassing routing) to
      avoid an infinite retry loop when the routing producer itself is failing.

    Example usage:
        producer = create_dlq_producer("payments")
        result = producer.send_with_fault_tolerance(
            topic="payments-high",
            data={"transaction_id": "txn_001", "amount_cents": 5000},
        )
        if result.success:
            print(f"Delivered after {result.retry_count} retries")
        else:
            print(f"Routed to DLQ — last error: {result.error}")
            print(f"Circuit state: {result.circuit_state.value}")
    """

    def __init__(
        self,
        service_name: str,
        config: Optional[FaultToleranceConfig] = None,
        underlying_producer: Optional[UnderlyingProducerProtocol] = None,
        routing_producer: Optional[RoutingProducerProtocol] = None,
        enable_dlq: bool = True,
        producer_config: Optional[Dict[str, Any]] = None,
        environment: Environment = Environment.DEVELOPMENT,
        bootstrap_servers: Optional[str] = None,
    ) -> None:
        """
        Input:
          service_name        — logical service identifier; drives DLQ topic naming
                                and routing-topic generation.
          config              — fault-tolerance tuning; defaults to conservative
                                production settings (FaultToleranceConfig()).
          underlying_producer — injected for DLQ writes and unit tests.
                                Created automatically when not provided.
          routing_producer    — injected for normal sends and unit tests.
                                Created automatically when not provided.
          enable_dlq          — set False to suppress DLQ writes in read-only
                                environments or test scenarios that inspect errors.
        """
        self.service_name = service_name
        self.config = config or FaultToleranceConfig()
        self.enable_dlq = enable_dlq

        # Resolve production dependencies when not injected.
        # The base (underlying) producer is built first so the routing producer
        # can share the same singleton connection rather than creating a second one.
        # Safety-critical:
        # propagate caller bootstrap/environment context into producer creation.
        # Otherwise default wiring can accidentally target a production cluster.
        _base_producer = underlying_producer or default_underlying_producer_factory(
            config=producer_config,
            environment=environment,
            bootstrap_servers=bootstrap_servers,
        )
        self._underlying_producer: UnderlyingProducerProtocol = _base_producer
        self._routing_producer: RoutingProducerProtocol = (
            routing_producer or default_routing_producer_factory(service_name, _base_producer)  # type: ignore[arg-type]
        )

        # Fault-tolerance components — each handles one failure scenario.
        self._circuit_breaker = CircuitBreaker(self.config)
        self._bulkhead = Bulkhead(self.config)
        self._health_monitor = SlidingWindowHealthMonitor(self.config)

        self._dlq_topic = f"{service_name}{DLQ_TOPIC_SUFFIX}"

        logger.info(
            "DeadLetterQueueProducer ready — service=%s, dlq_topic=%s, dlq_enabled=%s",
            service_name,
            self._dlq_topic,
            enable_dlq,
        )

    # ── Primary public API ─────────────────────────────────────────────────────

    def send_with_fault_tolerance(
        self,
        topic: str,
        data: Dict[str, Any],
        routing_metadata: Optional[Any] = None,
        **kwargs: Any,
    ) -> SendAttemptResult:
        """
        Send one message with all four fault-tolerance layers active.

        Input:
          topic             — target Kafka topic for direct sends.
          data              — message payload; must be JSON-serialisable.
          routing_metadata  — optional MessageMetadata for priority routing.
                              When provided, the routing producer selects the
                              destination topic; the topic argument is ignored.
          **kwargs          — forwarded to the underlying produce call.

        Output: SendAttemptResult — see types.py for field documentation.

        Failure behavior:
          OPEN circuit     → immediate fast-fail; no network call made.
          Bulkhead full    → resource-timeout result returned.
          Retries exhausted → DLQ send attempted + failure result returned.
        """
        # Stage 1.0 — Circuit Breaker gate.
        # OPEN means the broker is known bad; fail immediately without touching
        # the network.  This protects the broker AND saves time for the caller.
        if not self._circuit_breaker.can_execute():
            logger.warning(
                "CircuitBreaker OPEN for service=%s — fast-failing send to topic=%s",
                self.service_name,
                topic,
            )
            return SendAttemptResult(
                success=False,
                error=RuntimeError(
                    f"Circuit breaker OPEN for service '{self.service_name}' — "
                    f"broker flagged unavailable; will probe after "
                    f"{self.config.recovery_timeout_seconds}s"
                ),
                circuit_state=self._circuit_breaker.state,
            )

        # Stage 2.0 — Bulkhead slot acquisition.
        # Block up to send_timeout_seconds; return immediately if already at cap.
        if not self._bulkhead.acquire(timeout=self.config.send_timeout_seconds):
            logger.warning(
                "Bulkhead at capacity (%d/%d active) for service=%s — rejecting send to topic=%s",
                self._bulkhead.active_count,
                self.config.max_concurrent_sends,
                self.service_name,
                topic,
            )
            return SendAttemptResult(
                success=False,
                error=RuntimeError(
                    f"Bulkhead capacity exhausted "
                    f"(max_concurrent_sends={self.config.max_concurrent_sends}) "
                    f"for service '{self.service_name}'"
                ),
                circuit_state=self._circuit_breaker.state,
            )

        try:
            # Stage 3.0 — Retry loop with exponential backoff.
            result = self._execute_with_retry(topic, data, routing_metadata, **kwargs)

            # Stage 4.0 — Record outcome in health monitor.
            # Circuit transitions are now recorded during protected send calls
            # inside _execute_with_retry() via pybreaker.
            if not result.success and self.enable_dlq:
                # Stage 4.1 — Preserve the failed message in the DLQ topic.
                self._send_to_dlq(
                    original_topic=topic,
                    original_data=data,
                    error=result.error,
                    routing_metadata=routing_metadata,
                )

            self._health_monitor.record_operation(result.success, result.execution_time_seconds)
            return result

        finally:
            # Stage 5.0 — Release bulkhead slot unconditionally.
            # The finally block guarantees release even if an unexpected exception
            # escapes _execute_with_retry(), preventing a permanent slot leak.
            self._bulkhead.release()

    def send_with_priority(
        self,
        data: Dict[str, Any],
        priority: Any,
        **kwargs: Any,
    ) -> SendAttemptResult:
        """
        Convenience wrapper: let the routing layer select the destination topic
        based on the given TopicPriority value.

        Input:
          data     — message payload.
          priority — TopicPriority enum value (HIGH, MEDIUM, LOW, BACKGROUND).
        Output: SendAttemptResult (same contract as send_with_fault_tolerance).
        """
        # Import deferred to avoid pulling confluent_kafka into types.py scope.
        from ..content_based_router.types import MessageRoutingContext

        metadata = MessageRoutingContext(priority=priority, source_service=self.service_name)
        return self.send_with_fault_tolerance("", data, routing_metadata=metadata, **kwargs)

    # ── Operational / monitoring API ───────────────────────────────────────────

    def health_status(self) -> Dict[str, Any]:
        """
        Return a structured health snapshot for monitoring dashboards or logs.

        Output keys:
          service_name    — the logical service this producer is scoped to.
          circuit_breaker — dict with state (str) and failure_count (int).
          bulkhead        — dict with active_count and max_concurrent_sends.
          health_monitor  — dict from SlidingWindowHealthMonitor.get_metrics_snapshot().
        """
        return {
            "service_name": self.service_name,
            "circuit_breaker": {
                "state": self._circuit_breaker.state.value,
                "failure_count": self._circuit_breaker.failure_count,
            },
            "bulkhead": {
                "active_count": self._bulkhead.active_count,
                "max_concurrent_sends": self.config.max_concurrent_sends,
            },
            "health_monitor": self._health_monitor.get_metrics_snapshot(),
        }

    def reset_circuit_breaker(self) -> None:
        """Administrative reset — forces circuit to CLOSED for operator use."""
        self._circuit_breaker.reset()

    def set_dlq_enabled(self, enabled: bool) -> None:
        """Enable or disable DLQ routing at runtime (e.g. for maintenance windows)."""
        self.enable_dlq = enabled
        logger.info(
            "DLQ routing %s for service=%s",
            "enabled" if enabled else "disabled",
            self.service_name,
        )

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _execute_with_retry(
        self,
        topic: str,
        data: Dict[str, Any],
        routing_metadata: Optional[Any],
        **kwargs: Any,
    ) -> SendAttemptResult:
        """
        Execute one protected send operation through tenacity + pybreaker.

        Why tenacity:
        - battle-tested retry orchestration (`stop_after_attempt`, `wait_exponential`)
          replaces hand-rolled loops and sleep math.
        - gives explicit control to skip retries for specific exceptions.

        Why pybreaker call() is wrapped inside each tenacity attempt:
        - each broker-facing send contributes to the circuit breaker's counters.
        - when circuit is OPEN, pybreaker raises CircuitBreakerError immediately
          and tenacity is configured to stop retrying (fast-fail behavior).

        Input:
          topic, data, routing_metadata from send_with_fault_tolerance.
        Output:
          SendAttemptResult on first success or final non-retriable/exhausted failure.
        """
        start = time.monotonic()
        last_error: Optional[Exception] = None
        attempt_number = 0

        def _should_retry_exception(exception: BaseException) -> bool:
            """
            Decide whether tenacity should schedule another attempt.

            CircuitBreakerError is intentionally non-retriable here because
            pybreaker already says the broker is unavailable.
            """
            return not isinstance(exception, CircuitBreakerError)

        def _before_sleep(retry_state: Any) -> None:
            """Log per-attempt retry details before tenacity sleeps."""
            next_sleep_seconds = (
                retry_state.next_action.sleep if retry_state.next_action is not None else 0.0
            )
            failure = retry_state.outcome.exception() if retry_state.outcome is not None else None
            logger.warning(
                "Send attempt %d/%d failed for service=%s, topic=%s - retrying in %.2fs. Error: %s",
                retry_state.attempt_number,
                self.config.max_retries + 1,
                self.service_name,
                topic,
                next_sleep_seconds,
                failure,
            )

        retry_controller = Retrying(
            stop=stop_after_attempt(self.config.max_retries + 1),
            wait=wait_exponential(
                multiplier=self.config.initial_retry_delay_seconds,
                exp_base=self.config.retry_backoff_multiplier,
                min=self.config.initial_retry_delay_seconds,
                max=self.config.max_retry_delay_seconds,
            ),
            retry=retry_if_exception(_should_retry_exception),
            before_sleep=_before_sleep,
            reraise=True,
        )

        try:
            for attempt in retry_controller:
                attempt_number = attempt.retry_state.attempt_number
                with attempt:
                    # Stage 3.1.1:
                    # Force broker confirmation for each send attempt so success means
                    # broker-acknowledged delivery, not just local queueing.
                    send_kwargs = dict(kwargs)
                    send_kwargs["require_delivery_confirmation"] = True
                    send_kwargs["delivery_timeout_seconds"] = (
                        self.config.delivery_confirmation_timeout_seconds
                    )

                    def _protected_send() -> None:
                        """Single broker-facing operation protected by pybreaker."""
                        if routing_metadata is not None:
                            self._routing_producer.send_with_metadata(
                                data, routing_metadata, **send_kwargs
                            )
                        else:
                            self._routing_producer.send_to_topic(topic, data, **send_kwargs)

                    self._circuit_breaker.call(_protected_send)

        except Exception as exc:
            last_error = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
            logger.error(
                "Send failed after %d/%d attempts for service=%s, topic=%s. Final error: %s",
                attempt_number if attempt_number > 0 else 1,
                self.config.max_retries + 1,
                self.service_name,
                topic,
                last_error,
            )
            return SendAttemptResult(
                success=False,
                error=last_error,
                execution_time_seconds=time.monotonic() - start,
                retry_count=max(0, (attempt_number if attempt_number > 0 else 1) - 1),
                circuit_state=self._circuit_breaker.state,
            )

        return SendAttemptResult(
            success=True,
            execution_time_seconds=time.monotonic() - start,
            retry_count=max(0, attempt_number - 1),
            circuit_state=self._circuit_breaker.state,
        )

    def _send_to_dlq(
        self,
        original_topic: str,
        original_data: Dict[str, Any],
        error: Optional[Exception],
        routing_metadata: Optional[Any],
    ) -> None:
        """
        Preserve a failed message in the Dead Letter Queue topic.

        Why a separate DLQ send path (not through the routing producer):
        - The routing producer is the component that exhausted retries — routing
          DLQ through the same path risks an infinite failure loop.
        - underlying_producer writes directly to the named DLQ topic, bypassing
          all routing rules and ensuring DLQ delivery is independent of routing health.

        DLQ envelope fields:
          original_topic    — the topic the message was intended for.
          original_data     — the message payload exactly as submitted.
          error_type        — Python exception class name for categorisation.
          error_message     — string representation of the final error.
          routing_metadata  — serialised routing metadata if present.
          service_name      — the producing service for replay/triage attribution.
          failed_at_unix    — Unix timestamp (float) for time-ordering in the DLQ.

        Failure behavior:
        - DLQ send failure is logged at CRITICAL level but NOT re-raised.
          Raising would replace the original error context and mask the fact
          that a message was lost.  Operators should monitor the CRITICAL log.
        """
        routing_meta_serialised: Optional[Dict[str, Any]] = None
        if routing_metadata is not None and hasattr(routing_metadata, "__dict__"):
            try:
                routing_meta_serialised = vars(routing_metadata)
            except Exception:
                routing_meta_serialised = {"_repr": repr(routing_metadata)}

        dlq_envelope: Dict[str, Any] = {
            "original_topic": original_topic,
            "original_data": original_data,
            "error_type": type(error).__name__ if error else "Unknown",
            "error_message": str(error) if error else "",
            "routing_metadata": routing_meta_serialised,
            "service_name": self.service_name,
            "failed_at_unix": time.time(),
        }

        try:
            self._underlying_producer.send(
                self._dlq_topic,
                dlq_envelope,
                require_delivery_confirmation=True,
                delivery_timeout_seconds=self.config.delivery_confirmation_timeout_seconds,
            )
            logger.info(
                "DLQ preserved — service=%s, dlq_topic=%s, original_topic=%s",
                self.service_name,
                self._dlq_topic,
                original_topic,
            )
        except Exception as dlq_error:
            logger.critical(
                "CRITICAL: DLQ send also failed — message may be permanently lost.  "
                "service=%s, dlq_topic=%s, dlq_error=%s, original_error=%s",
                self.service_name,
                self._dlq_topic,
                dlq_error,
                error,
            )


# ── Factory functions ──────────────────────────────────────────────────────────


def create_dlq_producer(
    service_name: str,
    config: Optional[FaultToleranceConfig] = None,
    *,
    producer_config: Optional[Dict[str, Any]] = None,
    environment: Environment = Environment.DEVELOPMENT,
    bootstrap_servers: Optional[str] = None,
) -> DeadLetterQueueProducer:
    """
    Create a DLQ producer with default conservative production settings.

    Input:  service_name — logical service name for topic and DLQ naming.
            config       — optional tuning; defaults to FaultToleranceConfig().
    Output: DeadLetterQueueProducer wired to the shared singleton connection.
    """
    return DeadLetterQueueProducer(
        service_name=service_name,
        config=config,
        producer_config=producer_config,
        environment=environment,
        bootstrap_servers=bootstrap_servers,
    )


def create_ha_dlq_producer(
    service_name: str,
    *,
    producer_config: Optional[Dict[str, Any]] = None,
    environment: Environment = Environment.DEVELOPMENT,
    bootstrap_servers: Optional[str] = None,
) -> DeadLetterQueueProducer:
    """
    Create a DLQ producer pre-configured for High Availability requirements.

    HA config vs defaults (see constants.py for values):
    ┌──────────────────────────────┬───────────┬────────────┐
    │ Parameter                    │ Default   │ HA preset  │
    ├──────────────────────────────┼───────────┼────────────┤
    │ failure_threshold            │ 5         │ 2          │
    │ recovery_timeout_seconds     │ 60        │ 30         │
    │ max_retries                  │ 3         │ 5          │
    │ max_concurrent_sends         │ 100       │ 200        │
    │ error_rate_unhealthy_thres.  │ 0.10      │ 0.05       │
    └──────────────────────────────┴───────────┴────────────┘

    Use for: payments, authentication, healthcare data — any pipeline where
    message loss has regulatory or financial consequences.
    """
    ha_config = FaultToleranceConfig(
        failure_threshold=HA_FAILURE_THRESHOLD,
        recovery_timeout_seconds=HA_RECOVERY_TIMEOUT_SECONDS,
        max_retries=HA_MAX_RETRIES,
        max_concurrent_sends=HA_MAX_CONCURRENT_SENDS,
        error_rate_unhealthy_threshold=HA_ERROR_RATE_UNHEALTHY_THRESHOLD,
    )
    return DeadLetterQueueProducer(
        service_name=service_name,
        config=ha_config,
        producer_config=producer_config,
        environment=environment,
        bootstrap_servers=bootstrap_servers,
    )
