"""
Fire-and-forget Kafka producer: shared, long-lived, thread-safe application instance.

─────────────────────────────────────────────────────────────────────────────
WHAT IS FIRE-AND-FORGET?
─────────────────────────────────────────────────────────────────────────────
Fire-and-forget is the simplest Kafka producer delivery mode.
The producer calls produce(), which enqueues the message in librdkafka's
internal buffer and returns immediately — without waiting for a broker
acknowledgement. The application moves on while the Kafka client handles
network I/O on a background thread.

Delivery outcome:  "best effort" — messages may be lost if the broker is
                   down, the local buffer fills, or the process exits before
                   flush() is called.

Use when:
  - message loss is acceptable (metrics, analytics, non-critical events)
  - throughput and low caller latency matter more than durability guarantees
  - you want the simplest possible producer code path

Avoid when:
  - every message must be accounted for (payments, orders, audit logs)
  - you need at-least-once or exactly-once semantics
  → use callback_confirmed or transactional_producer instead

─────────────────────────────────────────────────────────────────────────────
WHY A SHARED (SINGLETON) INSTANCE?
─────────────────────────────────────────────────────────────────────────────
confluent_kafka.Producer is thread-safe and designed to be created once and
reused for the lifetime of the application. Creating a new Producer per
request wastes TCP connections to brokers and prevents internal batching from
working efficiently. The singleton pattern here is an *implementation detail*
of producer lifecycle management, not a Kafka delivery guarantee.

─────────────────────────────────────────────────────────────────────────────
ASCII FLOW — fire-and-forget send path
─────────────────────────────────────────────────────────────────────────────

  Application Thread (caller)         librdkafka Background Thread
  ────────────────────────────         ──────────────────────────────
  Stage 1.0  validate + serialize
        |
        v
  Stage 2.0  wire delivery callback
        |
        v
  Stage 3.0  producer.produce()  ──→  [librdkafka internal queue]
        |                                       |
        v                                       |  async network I/O
  Stage 4.0a  producer.poll(0)           [Kafka Broker]
        |      (non-blocking:                   |
        |       fires only ready callbacks)     |  broker ack or error
        |                                       |
        v                               delivery callback fires
  caller resumes immediately             → updates health metrics
  (does NOT wait for broker ack)         → logs topic/partition/offset

─────────────────────────────────────────────────────────────────────────────
OPTIONAL CONFIRMATION MODE (Stage 4.0b)
─────────────────────────────────────────────────────────────────────────────
When require_delivery_confirmation=True is passed to send(), the caller
DOES wait for the delivery callback before returning. This turns the call into
a synchronous send and sacrifices throughput for delivery certainty.

  Stage 4.0b  _await_delivery_confirmation()
        |      polls in 100 ms intervals until callback fires or timeout
        v
  raises TimeoutError  if broker takes too long
  raises RuntimeError  if broker rejected the message
"""

from __future__ import annotations

# atexit: registers _cleanup() so in-flight messages are flushed on exit.
import atexit

# json: standard wire format for event payloads on this topic schema.
import json

# logging: structured operational breadcrumbs without leaking secrets.
import logging

# threading: Lock for double-checked locking; Event for delivery confirmation.
import threading

# time: monotonic clock for delivery confirmation deadline; exponential backoff.
import time

# typing: explicit contracts for every public and internal boundary.
from typing import Any, Dict, List, Optional, Tuple

from .clients import default_producer_factory
from .constants import (
    DEFAULT_DELIVERY_TIMEOUT_SECONDS,
    DEFAULT_FLUSH_TIMEOUT_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_POLL_TIMEOUT_SECONDS,
    DEFAULT_RETRY_DELAY_SECONDS,
    HEALTH_ERROR_RATE_THRESHOLD,
    HEALTH_MIN_MESSAGE_COUNT,
    SENSITIVE_CONFIG_KEY_MARKERS,
    SHUTDOWN_FLUSH_TIMEOUT_SECONDS,
)
from .types import (
    DeliveryCallback,
    KafkaProducerLike,
    ProducerFactory,
    ProducerHealthMetrics,
    RetryResult,
    SendResult,
)

# Primary import path for repository execution.
try:
    from config.kafka_config import Environment, get_producer_config
except ImportError:  # pragma: no cover — package install fallback.
    from kafka.config.kafka_config import Environment, get_producer_config  # type: ignore[no-redef]

logger = logging.getLogger("producers.fire_and_forget")


# ---------------------------------------------------------------------------
# FireAndForgetProducer — thread-safe shared producer instance
# ---------------------------------------------------------------------------


class FireAndForgetProducer:
    """
    Thread-safe, application-scoped Kafka producer using fire-and-forget delivery.

    Design:
    - One instance is created on first access and reused for the entire application
      lifecycle. This eliminates per-request TCP handshakes to brokers and lets
      librdkafka's internal batching work as intended.
    - Thread-safety is guaranteed by double-checked locking on _class_lock.
      Once initialized, send() requires no locking because confluent_kafka.Producer
      is itself thread-safe at the C-extension level.
    - Health monitoring tracks cumulative delivery errors so operators can detect
      broker issues without grep-ing logs.

    Lifecycle:
        # Any thread, any call site — always returns the same instance.
        producer = FireAndForgetProducer.get_instance()
        producer.send("page-views", {"user_id": 42, "page": "/home"})

    Reset (testing only):
        FireAndForgetProducer.reset_instance()

    Graceful shutdown:
        producer.flush()   # or let atexit handle it automatically
    """

    # Class-level singleton state — shared across all threads.
    _instance: Optional["FireAndForgetProducer"] = None
    _class_lock: threading.Lock = threading.Lock()
    _initialized: bool = False

    def __new__(
        cls,
        config: Optional[Dict[str, Any]] = None,
        producer_factory: Optional[ProducerFactory] = None,
    ) -> "FireAndForgetProducer":
        """
        Thread-safe singleton instantiation using double-checked locking.

        Why double-checked locking:
        - The outer `if` skips the lock on every call after initialization,
          keeping the hot path lock-free for maximum throughput.
        - The inner `if` inside the lock handles the race where two threads
          both see _instance is None simultaneously.

        Production note:
        Python's GIL means assignment to _instance is atomic, but we still
        use a lock to guarantee the full __init__ completes before any other
        thread sees the instance as ready.
        """
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        producer_factory: Optional[ProducerFactory] = None,
    ) -> None:
        """
        Initialize the shared producer instance (runs once despite multiple __new__ calls).

        Args:
            config: Optional producer config dict. If omitted, loads PRODUCTION
                    defaults from config.kafka_config.get_producer_config().
                    Only honoured on the *first* call — subsequent calls are no-ops.
            producer_factory: Optional factory injected by tests so no real broker
                              connection is opened. Defaults to default_producer_factory.
        """
        if self._initialized:
            return

        with self._class_lock:
            if self._initialized:
                return

            resolved_config = config or get_producer_config(environment=Environment.PRODUCTION)
            factory = producer_factory or default_producer_factory

            self._config: Dict[str, Any] = resolved_config
            self._producer: KafkaProducerLike = factory(resolved_config)
            self._health_lock: threading.Lock = threading.Lock()

            # Mutable health counters — updated only inside delivery callbacks,
            # which librdkafka fires on the calling thread via poll().
            self._is_healthy: bool = True
            self._messages_sent: int = 0
            self._error_count: int = 0

            # Register cleanup so pending messages are flushed on process exit.
            atexit.register(self._shutdown)

            FireAndForgetProducer._initialized = True
            logger.info(
                "FireAndForgetProducer initialized (bootstrap: %s).",
                resolved_config.get("bootstrap.servers", "unknown"),
            )

    # ------------------------------------------------------------------
    # Class-level factory and lifecycle methods
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(
        cls,
        config: Optional[Dict[str, Any]] = None,
        producer_factory: Optional[ProducerFactory] = None,
    ) -> "FireAndForgetProducer":
        """
        Return the shared producer instance, creating it on first call.

        Args:
            config: Only honoured on the first call; ignored on subsequent calls.
            producer_factory: Injected by tests; ignored after first initialization.

        Returns:
            The singleton FireAndForgetProducer instance.
        """
        return cls(config=config, producer_factory=producer_factory)

    @classmethod
    def reset_instance(cls) -> None:
        """
        Tear down and discard the current singleton instance.

        When to use:
        - test isolation: call this in setUp/tearDown to start each test fresh
        - never call this in production code; it breaks the shared-instance contract

        Effect: next get_instance() call creates a brand-new producer with a fresh
        broker connection and zeroed health counters.
        """
        with cls._class_lock:
            if cls._instance is not None:
                cls._instance._shutdown()
            cls._instance = None
            cls._initialized = False

    # ------------------------------------------------------------------
    # Delivery callback
    # ------------------------------------------------------------------

    def _default_delivery_callback(self, err: Optional[Any], msg: Any) -> None:
        """
        Default broker delivery callback — updates health metrics.

        When librdkafka fires this:
        - err is None → message reached a broker partition and was written.
        - err is set  → delivery failed (broker timeout, topic not found, etc.).

        Health degradation logic:
        - We require HEALTH_MIN_MESSAGE_COUNT completions before evaluating rate
          so a single early error does not permanently mark the producer unhealthy.
        - Once unhealthy, the producer stays unhealthy until reset_instance().
          This is intentional: force an operator action rather than silently
          continuing to drop messages.
        """
        with self._health_lock:
            if err is not None:
                self._error_count += 1
            else:
                self._messages_sent += 1

            total_callbacks = self._messages_sent + self._error_count
            error_rate = self._error_count / max(total_callbacks, 1)
            should_mark_unhealthy = (
                total_callbacks >= HEALTH_MIN_MESSAGE_COUNT
                and error_rate > HEALTH_ERROR_RATE_THRESHOLD
            )
            if should_mark_unhealthy:
                self._is_healthy = False

        if err is not None:
            logger.error("Delivery failed: %s", err)
            if should_mark_unhealthy:
                logger.warning(
                    "Producer marked unhealthy: error rate %.1f%% (%d/%d) exceeds %.1f%% threshold.",
                    100 * error_rate,
                    self._error_count,
                    total_callbacks,
                    100 * HEALTH_ERROR_RATE_THRESHOLD,
                )
            return

        logger.debug(
            "Delivered → topic=%s partition=%d offset=%d",
            msg.topic(),
            msg.partition(),
            msg.offset(),
        )

    # ------------------------------------------------------------------
    # Delivery confirmation helper
    # ------------------------------------------------------------------

    def _await_delivery_confirmation(
        self,
        topic: str,
        delivery_event: threading.Event,
        delivery_state: Dict[str, Any],
        timeout_seconds: float,
    ) -> None:
        """
        Block until the delivery callback fires or the deadline expires.

        Why this helper exists:
        produce() is always asynchronous — the callback fires when librdkafka
        receives a broker ack. To turn fire-and-forget into a synchronous send,
        we poll in short intervals until our Event is set by the callback.

        Stage 1.0  Poll in 100 ms intervals, checking the event after each poll.
                   'remaining' prevents over-sleeping past the deadline.

        Stage 2.0  If the event never fired, raise TimeoutError with the topic name
                   so the caller can log and decide whether to retry.

        Stage 3.0  If the callback itself raised an exception, re-raise it wrapped
                   in RuntimeError so it propagates out of send().

        Stage 4.0  If the broker returned a delivery error, raise RuntimeError
                   with the error string so callers can inspect it.

        Args:
            topic: Destination topic — included in error messages for traceability.
            delivery_event: threading.Event set by the delivery callback when done.
            delivery_state: Dict capturing callback_error and delivery_error.
            timeout_seconds: Maximum wall-clock seconds to wait for confirmation.
        """
        # Stage 1.0 — poll until delivery callback fires or deadline passes.
        deadline = time.monotonic() + timeout_seconds
        while not delivery_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._producer.poll(min(DEFAULT_POLL_INTERVAL_SECONDS, remaining))

        # Stage 2.0 — timeout path.
        if not delivery_event.is_set():
            raise TimeoutError(
                f"No delivery confirmation received for topic '{topic}' "
                f"within {timeout_seconds:.1f}s. "
                "Check broker connectivity and increase delivery_timeout_seconds if needed."
            )

        # Stage 3.0 — callback raised an exception.
        callback_error = delivery_state.get("callback_error")
        if callback_error is not None:
            raise RuntimeError(
                f"Delivery callback raised an exception for topic '{topic}': {callback_error}"
            ) from callback_error

        # Stage 4.0 — broker rejected the message.
        delivery_error = delivery_state.get("delivery_error")
        if delivery_error is not None:
            raise RuntimeError(f"Broker rejected message for topic '{topic}': {delivery_error}")

    # ------------------------------------------------------------------
    # Public send API
    # ------------------------------------------------------------------

    def send(
        self,
        topic: str,
        data: Dict[str, Any],
        key: Optional[str] = None,
        callback: Optional[DeliveryCallback] = None,
        headers: Optional[Dict[str, str]] = None,
        timestamp: Optional[int] = None,
        require_delivery_confirmation: bool = False,
        delivery_timeout_seconds: float = DEFAULT_DELIVERY_TIMEOUT_SECONDS,
    ) -> SendResult:
        """
        Enqueue a message for fire-and-forget delivery.

        Default mode (require_delivery_confirmation=False):
        - produce() enqueues locally and returns immediately.
        - poll(0) fires any delivery callbacks that are already ready.
        - Caller does NOT wait for broker acknowledgement.
        - Highest throughput; messages may be lost if the broker is unreachable.

        Confirmation mode (require_delivery_confirmation=True):
        - Same produce() call, but then polls until the delivery callback fires.
        - Raises TimeoutError or RuntimeError on failure.
        - Lower throughput; appropriate for critical messages that must succeed.

        Stage 1.0  Health guard — refuse to send if error rate is too high.
        Stage 2.0  Serialize payload and key to bytes.
        Stage 3.0  Wire delivery callback chain (default or caller-supplied).
        Stage 4.0  Call producer.produce() — always non-blocking.
        Stage 4.0a Fire-and-forget: poll(0) and return immediately.
        Stage 4.0b Confirmation: poll until ack or timeout.

        Args:
            topic: Target Kafka topic name.
            data: Event payload — must be JSON-serializable.
            key: Optional partition routing key. None = round-robin assignment.
            callback: Custom delivery callback. If omitted, the default callback
                      updates health metrics and logs the result.
            headers: Optional Kafka message headers (string key → string value).
            timestamp: Optional producer-side epoch-milliseconds timestamp.
                       None = broker assigns the timestamp.
            require_delivery_confirmation: When True, block until broker acks.
            delivery_timeout_seconds: Max wait for confirmation (ignored when False).

        Returns:
            SendResult with success=True on enqueue success.

        Raises:
            ValueError: If topic/key/data inputs are invalid or data cannot be
                        JSON-serialized.
            TimeoutError: If confirmation mode times out waiting for broker ack.
            RuntimeError: If confirmation mode receives a broker delivery error.
        """
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("Topic must be a non-empty string.")
        if key is not None and (not isinstance(key, str) or not key.strip()):
            raise ValueError("Message key must be a non-empty string when provided.")

        # Stage 1.0 — health guard.
        with self._health_lock:
            is_healthy = self._is_healthy

        if not is_healthy:
            return SendResult(
                topic=topic,
                message_key=key,
                success=False,
                error_message=(
                    "Producer is in an unhealthy state (error rate too high). "
                    "Call reset_instance() after investigating broker connectivity."
                ),
            )

        # Stage 2.0 — serialize payload and key.
        if data is None:
            raise ValueError("Message data cannot be None.")
        try:
            serialized_value: bytes = json.dumps(data).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Message data is not JSON-serializable: {exc}") from exc

        serialized_key: Optional[bytes] = key.encode("utf-8") if key else None

        kafka_headers: List[Tuple[str, bytes]] = []
        if headers:
            kafka_headers = [(k, v.encode("utf-8")) for k, v in headers.items()]

        # Stage 3.0 — wire callback chain.
        # Always track delivery state so confirmation mode can inspect the outcome.
        # The default callback always runs so health counters remain accurate even
        # when callers provide a custom callback.
        callback_delegate: Optional[DeliveryCallback] = callback
        delivery_event = threading.Event()
        delivery_state: Dict[str, Any] = {"delivery_error": None, "callback_error": None}

        def _wired_callback(err: Optional[Any], msg: Any) -> None:
            """Capture broker result, invoke the configured callback, then signal the event."""
            delivery_state["delivery_error"] = err
            try:
                try:
                    self._default_delivery_callback(err, msg)
                except Exception as exc:  # noqa: BLE001
                    delivery_state["callback_error"] = exc
                    logger.exception("Default delivery callback raised for topic '%s'.", topic)
                if (
                    callback_delegate is not None
                    and callback_delegate != self._default_delivery_callback
                ):
                    try:
                        callback_delegate(err, msg)
                    except Exception as exc:  # noqa: BLE001
                        if delivery_state["callback_error"] is None:
                            delivery_state["callback_error"] = exc
                        logger.exception("Custom delivery callback raised for topic '%s'.", topic)
            finally:
                delivery_event.set()

        # Stage 4.0 — enqueue message (always non-blocking).
        produce_kwargs: Dict[str, Any] = {
            "topic": topic,
            "value": serialized_value,
            "key": serialized_key,
            "headers": kafka_headers,
            "callback": _wired_callback,
        }
        if timestamp is not None:
            produce_kwargs["timestamp"] = timestamp

        try:
            self._producer.produce(**produce_kwargs)
        except Exception as exc:
            logger.error("produce() failed for topic '%s': %s", topic, exc)
            return SendResult(topic=topic, message_key=key, success=False, error_message=str(exc))

        # Stage 4.0a — fire-and-forget: non-blocking poll then return.
        if not require_delivery_confirmation:
            self._producer.poll(DEFAULT_POLL_TIMEOUT_SECONDS)
            return SendResult(topic=topic, message_key=key, success=True)

        # Stage 4.0b — confirmation mode: wait for broker ack.
        self._await_delivery_confirmation(
            topic=topic,
            delivery_event=delivery_event,
            delivery_state=delivery_state,
            timeout_seconds=delivery_timeout_seconds,
        )
        return SendResult(topic=topic, message_key=key, success=True)

    def send_with_retry(
        self,
        topic: str,
        data: Dict[str, Any],
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
        key: Optional[str] = None,
        **send_kwargs: Any,
    ) -> RetryResult:
        """
        Enqueue a message with application-level exponential-backoff retry.

        What is being retried here:
        This retries LOCAL enqueue failures (e.g., producer buffer full, serialization
        error). It does NOT retry broker-level delivery failures — those are controlled
        by the 'retries' and 'retry.backoff.ms' producer config keys.
        Validation errors and unhealthy-producer states are treated as non-retriable
        and fail fast because retrying cannot change those outcomes.

        Backoff schedule example with retry_delay_seconds=1.0:
          attempt 1: immediate
          attempt 2: wait 1 s
          attempt 3: wait 2 s
          attempt 4: wait 4 s

        Args:
            topic: Target Kafka topic.
            data: Event payload (JSON-serializable dict).
            max_retries: Maximum additional attempts after the first failure.
                         0 means try once and give up immediately on failure.
            retry_delay_seconds: Base delay; doubled on each subsequent attempt.
            key: Optional partition routing key.
            **send_kwargs: Additional keyword arguments forwarded to send().

        Returns:
            RetryResult with success=True if any attempt enqueued successfully.
        """
        last_error: str = "unknown error"

        for attempt in range(max_retries + 1):
            try:
                result = self.send(topic=topic, data=data, key=key, **send_kwargs)
                if result.success:
                    return RetryResult(
                        topic=topic,
                        message_key=key,
                        attempts_made=attempt + 1,
                        success=True,
                    )
                last_error = result.error_message or "send() returned success=False"
                with self._health_lock:
                    producer_healthy = self._is_healthy
                if not producer_healthy:
                    return RetryResult(
                        topic=topic,
                        message_key=key,
                        attempts_made=attempt + 1,
                        success=False,
                        final_error_message=last_error,
                    )
            except ValueError as exc:
                last_error = str(exc)
                return RetryResult(
                    topic=topic,
                    message_key=key,
                    attempts_made=attempt + 1,
                    success=False,
                    final_error_message=last_error,
                )
            except Exception as exc:
                last_error = str(exc)

            if attempt < max_retries:
                delay = retry_delay_seconds * (2**attempt)
                logger.warning(
                    "send_with_retry: attempt %d/%d failed for topic '%s' — "
                    "retrying in %.1fs. Error: %s",
                    attempt + 1,
                    max_retries + 1,
                    topic,
                    delay,
                    last_error,
                )
                time.sleep(delay)

        logger.error(
            "send_with_retry: all %d attempts exhausted for topic '%s'. Last error: %s",
            max_retries + 1,
            topic,
            last_error,
        )
        return RetryResult(
            topic=topic,
            message_key=key,
            attempts_made=max_retries + 1,
            success=False,
            final_error_message=last_error,
        )

    # ------------------------------------------------------------------
    # Operational helpers
    # ------------------------------------------------------------------

    def flush(self, timeout_seconds: float = DEFAULT_FLUSH_TIMEOUT_SECONDS) -> int:
        """
        Block until all queued messages are delivered or timeout expires.

        When to call:
        - before application shutdown (if not using atexit)
        - after a batch of critical messages when you need delivery assurance
        - in integration tests to ensure all messages land before consuming

        Args:
            timeout_seconds: Maximum wait time in seconds.

        Returns:
            Remaining queue depth after timeout — 0 means all messages delivered.
            Negative value (-1) means the flush call itself raised an exception.
        """
        try:
            remaining = self._producer.flush(timeout_seconds)
            if remaining > 0:
                logger.warning(
                    "flush(): %d message(s) still queued after %.1fs timeout.",
                    remaining,
                    timeout_seconds,
                )
            else:
                logger.debug("flush(): all messages delivered.")
            return remaining
        except Exception as exc:
            logger.error("flush() raised an unexpected error: %s", exc)
            return -1

    def get_health_metrics(self) -> ProducerHealthMetrics:
        """
        Return a snapshot of current producer health and delivery statistics.

        Use this for:
        - health check endpoints (Kubernetes liveness probes, etc.)
        - operational dashboards
        - demo and interview demonstrations of producer observability

        Returns:
            ProducerHealthMetrics with is_healthy, message counts, and
            a secrets-redacted copy of the producer config.
        """
        with self._health_lock:
            messages_sent = self._messages_sent
            error_count = self._error_count
            total_callbacks = messages_sent + error_count
            error_rate = error_count / max(total_callbacks, 1)
            is_healthy = self._is_healthy

        return ProducerHealthMetrics(
            is_healthy=is_healthy,
            messages_sent=messages_sent,
            error_count=error_count,
            error_rate=error_rate,
            sanitized_config=_sanitize_config(self._config),
        )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        """
        Flush pending messages and release resources.

        Registered with atexit on initialization so this runs automatically
        when the Python process exits normally. Also called by reset_instance().

        Why 60 s flush timeout:
        A generous timeout gives in-flight messages the best chance of delivery
        before the process exits. Messages still queued after this window are
        logged as potentially lost — not silently dropped.
        """
        if not hasattr(self, "_producer") or self._producer is None:
            return

        logger.info("FireAndForgetProducer shutting down — flushing pending messages...")
        try:
            remaining = self._producer.flush(SHUTDOWN_FLUSH_TIMEOUT_SECONDS)
            if remaining > 0:
                logger.warning(
                    "Shutdown: %d message(s) may be lost (not delivered within %.0fs).",
                    remaining,
                    SHUTDOWN_FLUSH_TIMEOUT_SECONDS,
                )
            else:
                logger.info("Shutdown: all pending messages delivered successfully.")
        except Exception as exc:
            logger.error("Shutdown flush raised an error: %s", exc)
        finally:
            self._producer = None  # type: ignore[assignment]
            self._is_healthy = False


# ---------------------------------------------------------------------------
# Config sanitization helper
# ---------------------------------------------------------------------------


def _sanitize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a copy of config with credential-like keys replaced by '***REDACTED***'.

    Why this is needed:
    producer configs can contain SASL passwords, OAuth tokens, or TLS key paths.
    Logging them in plaintext creates a secret-leak risk in log aggregation systems.
    """
    return {key: "***REDACTED***" if _is_sensitive(key) else value for key, value in config.items()}


def _is_sensitive(config_key: str) -> bool:
    """Return True when the key name matches a known credential naming pattern."""
    lowered = config_key.lower()
    return any(marker in lowered for marker in SENSITIVE_CONFIG_KEY_MARKERS)


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------


def get_shared_producer(
    config: Optional[Dict[str, Any]] = None,
    producer_factory: Optional[ProducerFactory] = None,
) -> FireAndForgetProducer:
    """
    Return the shared FireAndForgetProducer instance.

    Convenience wrapper so call sites do not need to import the class directly.
    """
    return FireAndForgetProducer.get_instance(config=config, producer_factory=producer_factory)


def produce_event(
    topic: str,
    data: Dict[str, Any],
    **send_kwargs: Any,
) -> SendResult:
    """
    One-line fire-and-forget send using the shared producer instance.

    Suitable for non-critical event emission in application code where the
    caller does not need to hold a reference to the producer itself.
    """
    return get_shared_producer().send(topic=topic, data=data, **send_kwargs)


def produce_event_with_retry(
    topic: str,
    data: Dict[str, Any],
    **send_kwargs: Any,
) -> RetryResult:
    """
    One-line send-with-retry using the shared producer instance.

    Use when local enqueue reliability matters but broker-level confirmation
    is not required (fire-and-forget with best-effort retry on queue-full errors).
    """
    return get_shared_producer().send_with_retry(topic=topic, data=data, **send_kwargs)
