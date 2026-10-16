"""
The fire-and-forget Kafka producer.

═══════════════════════════════════════════════════════════════════════════
WHAT IS FIRE-AND-FORGET?
═══════════════════════════════════════════════════════════════════════════
Send the message, and never find out whether it arrived.

Think of dropping a postcard into a mailbox. You let go and walk away. It
almost certainly arrives. You get no tracking number, and if it is lost,
nobody tells you.

═══════════════════════════════════════════════════════════════════════════
WHAT MAKES IT FIRE-AND-FORGET? (the one thing to remember)
═══════════════════════════════════════════════════════════════════════════
There is no configuration setting for this. There is no `fire_and_forget=True`
flag anywhere in Kafka. The pattern is established by exactly one thing:

    producer.produce(topic, value=payload)              <-- fire-and-forget
    producer.produce(topic, value=payload,
                     on_delivery=callback)              <-- NOT fire-and-forget

It is the ABSENCE of the `on_delivery=` argument. Nothing else. Same class,
same broker, same network trip, same `acks` setting. Adding that one argument
turns this into the callback-confirmed pattern
(`producers.callback_confirmed`).

Two things people wrongly believe control it:

    acks=0          No. `acks` decides how careful the BROKER is before
                    answering. Whether YOUR CODE listens to that answer is a
                    separate decision. This class uses acks="all" and is
                    still fire-and-forget, because it never listens.

    an "async" flag No. Every produce() call in this library is asynchronous.
                    It returns without waiting for a delivery outcome.

═══════════════════════════════════════════════════════════════════════════
ASCII FLOW - what happens on a send
═══════════════════════════════════════════════════════════════════════════

  YOUR THREAD                        BACKGROUND THREAD (in the C library)      BROKER
  ───────────                        ────────────────────────────────────      ──────
  Stage 1.0  validate inputs
       │
       v
  Stage 2.0  serialize to bytes
       │
       v
  Stage 3.0  produce()  ──────────>  [ message waits in a local queue ]
       │     (returns instantly;              │
       │      touches no network)             │  started automatically; you
       v                                      │  never call it yourself
  Stage 4.0  return result                    v
       │                              send batch over network ───────────>  broker stores it
       v                                                           <───────  broker answers
  caller resumes immediately                  │
                                              v
  (never waits for the broker)      [ answer waits in a report queue ]
                                              │
                                              │  NOBODY IS LISTENING:
                                              │  no on_delivery callback was
                                              │  registered, so this answer
                                              x  is discarded, success or not

  Stage 5.0  flush()  -  before shutdown: give queued messages a bounded
                         chance to finish instead of abandoning them.

═══════════════════════════════════════════════════════════════════════════
WHEN TO USE THIS / WHEN NOT TO
═══════════════════════════════════════════════════════════════════════════
USE IT WHEN                              DO NOT USE IT WHEN
  high volume, each message cheap          each message is valuable
  losing a few is acceptable               loss must be detected or prevented
  throughput matters above all             you must react to a failure
  e.g. metrics, logs, clickstream,         e.g. payments, orders, sign-ups,
       telemetry, IoT sensor pings              inventory changes, audit logs

DECISION RULE
  "If one of these messages vanished and nobody noticed, is that acceptable?"
    yes -> fire-and-forget
    no  -> use `producers.callback_confirmed`, which adds the one argument
           above and turns silent loss into a reportable error.

PLAIN-LANGUAGE TERMS USED IN THIS FILE
  broker          one Kafka server
  topic           a named stream of messages
  partition       one ordered slice of a topic
  local queue     memory inside this process holding work not yet finished
  callback        a function saved now so the library can call it later
  serialize       convert a Python value into bytes for storage or transport
  back-pressure   the client saying "my local queue is full; slow down"
  flush           wait for queued work to finish or for a timeout to expire
  adapter         small code layer translating our interface to a library API
  context manager the object used by Python's ``with`` statement for cleanup
"""

# Postpone type-hint evaluation. This keeps annotations descriptive and avoids
# importing types only to evaluate hints at runtime.
from __future__ import annotations

# json: the payload wire format. Kafka itself stores only bytes and has no
# opinion on their structure, so we choose JSON and document it as the
# contract for the topics this producer writes to.
import json

# logging: operational breadcrumbs. Used instead of print() so applications
# control routing and verbosity, and so nothing is emitted in library use.
import logging

# time: monotonic clock for bounded waiting. monotonic() is used rather than
# time() because it cannot jump backwards when the system clock is adjusted.
import time

# TracebackType describes the traceback value received by a context manager's
# __exit__ method when code inside a ``with`` block raises an exception.
from types import TracebackType

# Self means "the same class as this method's instance" and gives __enter__ an
# accurate return type even if this class is later subclassed.
from typing import Self

# Import the default adapter that creates a real confluent-kafka producer.
from .clients import default_producer_factory
# Import the reviewed defaults used when callers provide no configuration.
from .config import build_producer_config
# Import every timing decision by name so no unexplained number appears below.
from .constants import (
    DEFAULT_FLUSH_TIMEOUT_SECONDS,
    DEFAULT_POLL_TIMEOUT_SECONDS,
    DEFAULT_QUEUE_CAPACITY_WAIT_SECONDS,
    QUEUE_CAPACITY_POLL_INTERVAL_SECONDS,
    SHUTDOWN_FLUSH_TIMEOUT_SECONDS,
)
# Import data contracts rather than depending on the concrete Kafka class.
from .types import (
    JsonObject,
    KafkaProducerLike,
    ProducerConfig,
    ProducerFactory,
    SendFailureReason,
    SendResult,
)

# Create a named logger. A logger records operational information and lets the
# host application decide whether it goes to a file, console, or monitoring tool.
logger = logging.getLogger("producers.fire_and_forget")


class FireAndForgetProducer:
    """A Kafka producer that sends without ever checking the outcome.

    PURPOSE
        Give calling code a `send()` that does not wait for broker delivery, for
        high-volume events where losing an occasional message is acceptable.

    WHAT IT DELIBERATELY DOES NOT DO
        It does not tell you whether a message arrived. No delivery callback
        is registered anywhere in this class - that omission *is* the pattern
        (see the module docstring). If you need delivery outcomes, use
        `producers.callback_confirmed` instead.

    LIFECYCLE - CREATE ONE, REUSE IT
        A producer holds TCP connections to the brokers and accumulates
        messages into batches. Creating one per message destroys both
        benefits and leaks connections. Create one per application and share
        it:

            producer = FireAndForgetProducer()
            try:
                producer.send("page-views", {"user": "u-1"})
            finally:
                producer.close()      # ALWAYS: flushes what is still queued

        Or let the context manager call `close()` for you:

            with FireAndForgetProducer() as producer:
                producer.send("page-views", {"user": "u-1"})

        If you want a single shared instance enforced process-wide, use
        `producers.singleton` instead, which adds that lifecycle management.

    THREAD SAFETY
        Safe to share across threads. The underlying client synchronises
        internally, so no application-level locking is needed around `send()`.
        Application shutdown must first stop worker threads from calling
        `send()`, then call `close()` once; closing concurrently with active
        sends creates an ambiguous lifecycle in any producer design.
    """

    # ============================================================
    # STEP 1: CONSTRUCTION
    # ============================================================

    def __init__(
        self,
        config: ProducerConfig | None = None,
        producer_factory: ProducerFactory | None = None,
        queue_capacity_wait_seconds: float = DEFAULT_QUEUE_CAPACITY_WAIT_SECONDS,
    ) -> None:
        """Create a producer and its connection pool.

        Args:
            config: Producer settings. When None, uses
                `config.build_producer_config()`, whose every key is explained
                in that module.
            producer_factory: How to build the underlying client. When None,
                uses `clients.default_producer_factory`. Tests inject a fake
                factory here so no broker is contacted.
            queue_capacity_wait_seconds: How long `send()` may wait for local
                queue capacity before dropping a message. Set 0.0 to never
                wait (shed load immediately). See `constants.py`.

        Failure behaviour:
            Raises `ValueError` for a negative queue wait. The default adapter
            raises `RuntimeError` when confluent-kafka is not installed and
            `KafkaException` when configuration is invalid. An unreachable
            broker does not normally raise here because connection is lazy;
            that failure later remains unobserved by this pattern.
        """
        # A negative duration has no useful meaning and could produce surprising
        # deadline calculations, so reject it before creating any resources.
        if queue_capacity_wait_seconds < 0:
            raise ValueError("queue_capacity_wait_seconds must be zero or greater.")

        # ``is not None`` matters: an explicitly supplied empty mapping should
        # reach the client and fail as configured, not silently gain defaults.
        self._config: ProducerConfig = dict(
            config if config is not None else build_producer_config()
        )
        # Keep the caller's overload budget for the queue-full retry loop.
        self._queue_capacity_wait_seconds = queue_capacity_wait_seconds
        # Use an injected factory in tests; otherwise use the real Kafka adapter.
        factory: ProducerFactory = producer_factory or default_producer_factory
        # Construct one long-lived client. None later means close() has run.
        self._producer: KafkaProducerLike | None = factory(self._config)

        # Log only connection labels, never arbitrary configuration, because a
        # full Kafka config may contain passwords or authentication tokens.
        logger.info(
            "FireAndForgetProducer ready (bootstrap.servers=%s, client.id=%s).",
            self._config.get("bootstrap.servers", "<unset>"),
            self._config.get("client.id", "<unset>"),
        )

    # ============================================================
    # STEP 2: SEND ONE MESSAGE
    # ============================================================

    def send(
        self,
        topic: str,
        data: JsonObject,
        key: str | None = None,
    ) -> SendResult:
        """Queue one message and return immediately.

        Walks Stages 1.0 to 4.0 of the module-level flow diagram.

        Args:
            topic: Destination topic name. Must be a non-empty string.
            data: The payload. Must be JSON-serializable.
            key: Optional partition key. Messages sharing a key always land
                in the same partition, which is how Kafka preserves ordering
                per entity (per customer, per device). Pass None when no
                ordering is required: the client then spreads messages across
                partitions, which batches better.

        Returns:
            A `SendResult`. Read `SendResult.queued` carefully: True means the
            local queue accepted the message, NOT that it was delivered. This
            pattern cannot report delivery - see the module docstring.

        Raises:
            ValueError: If `topic` is not a non-empty string, if `key` is
                supplied but blank, or if `data` is not JSON-serializable.
                These are programming errors, raised loudly rather than
                returned as a result, because retrying them cannot help.

        Failure behaviour:
            Returns `queued=False` with `error_message` set when the local
            queue is full even after waiting, or when the client rejects the
            message (for example, it exceeds the maximum message size).
            Broker-side failures are never reported, by design.
        """
        # --- Stage 1.0: validate inputs ------------------------------------
        # Raised rather than returned: a blank topic is a bug in the caller,
        # and no amount of retrying will fix it.
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("topic must be a non-empty string.")
        # A key selects a partition (one ordered slice of a Kafka topic). If a
        # key is supplied, blank text is almost certainly an application bug.
        if key is not None and (not isinstance(key, str) or not key.strip()):
            raise ValueError("key must be a non-empty string when provided.")
        # None usually means a forgotten payload. An empty dictionary remains
        # valid because it may be meaningful to the topic's data contract.
        if data is None:
            raise ValueError("data must not be None.")

        # close() replaces the client with None. Return a structured rejection
        # instead of attempting to call a resource that no longer exists.
        if self._producer is None:
            return SendResult(
                topic=topic,
                message_key=key,
                queued=False,
                failure_reason=SendFailureReason.PRODUCER_CLOSED,
                error_message="Producer is closed. Create a new instance to send again.",
            )

        # --- Stage 2.0: serialize to bytes ---------------------------------
        # Kafka stores bytes only, so the payload must be encoded here. A
        # non-serializable payload is a caller bug, hence ValueError.
        # Serialization means converting an in-memory Python value into bytes
        # that can be stored and moved between different programs.
        try:
            # json.dumps creates text; UTF-8 encode turns that text into bytes.
            value_bytes = json.dumps(data).encode("utf-8")
        # TypeError covers unsupported values such as object(); ValueError
        # covers invalid numeric data such as a disallowed circular structure.
        except (TypeError, ValueError) as error:
            # Chain the original error so a traceback retains the exact cause.
            raise ValueError(f"data is not JSON-serializable: {error}") from error

        # Kafka keys also travel as bytes. Keep None to request normal unkeyed
        # partition selection when the caller does not need entity ordering.
        key_bytes = key.encode("utf-8") if key is not None else None

        # --- Stages 3.0 and 4.0: queue it, then return local acceptance -----
        return self._queue_message(topic, key, value_bytes, key_bytes)

    def _queue_message(
        self,
        topic: str,
        key: str | None,
        value_bytes: bytes,
        key_bytes: bytes | None,
    ) -> SendResult:
        """Hand the message to the client, waiting briefly if the queue is full.

        Why this is a separate method: `send()` is about validating and
        shaping the caller's data; this is about the local queue's capacity.
        Keeping them apart means each has one job.

        Back-pressure handling:
            When the local queue is full the client raises `BufferError`.
            That is Kafka telling us "you are producing faster than I can
            deliver; slow down." Following the client guidance, we call
            `poll()` to serve pending events and give its background work time
            to advance, then retry. A deadline prevents a stuck broker from
            hanging the caller forever.

        Returns:
            A `SendResult` describing whether the queue accepted the message.
        """
        # send() checked the lifecycle immediately before calling this helper.
        # The assertion documents that invariant and helps static type checkers.
        assert self._producer is not None

        # A monotonic deadline cannot move backward if an administrator changes
        # the machine's wall clock while this retry loop is running.
        deadline = time.monotonic() + self._queue_capacity_wait_seconds

        # Repeat only for BufferError, which specifically means local queue
        # capacity is temporarily exhausted.
        while True:
            # Client calls can fail locally, so keep the success and failure
            # paths together around the single operation being attempted.
            try:
                # Stage 3.0 - THE PATTERN ITSELF.
                # Note what is NOT here: no `on_delivery=` argument. That
                # single omission is what makes this fire-and-forget. With it,
                # this would be the callback-confirmed pattern instead.
                self._producer.produce(topic, value=value_bytes, key=key_bytes)

                # Stage 4.0 - report local acceptance. No callback is
                # registered, so this says nothing about broker delivery.
                return SendResult(topic=topic, message_key=key, queued=True)

            # BufferError is back-pressure: the application is offering work
            # faster than the client can move it toward Kafka.
            except BufferError:
                # Stop retrying once the caller-approved waiting budget expires.
                if time.monotonic() >= deadline:
                    # Build one message used by both the log and returned result
                    # so operators and application code see the same diagnosis.
                    message = (
                        f"Local queue still full after "
                        f"{self._queue_capacity_wait_seconds:.1f}s; message dropped. "
                        f"The producer cannot deliver as fast as this application "
                        f"is producing."
                    )
                    # A warning is appropriate because data was dropped, but the
                    # producer object itself can still accept later messages.
                    logger.warning("send(topic=%s): %s", topic, message)
                    # QUEUE_FULL is retryable because capacity may recover later.
                    return SendResult(
                        topic=topic,
                        message_key=key,
                        queued=False,
                        failure_reason=SendFailureReason.QUEUE_FULL,
                        error_message=message,
                    )

                # Confluent recommends polling before retrying a BufferError.
                # Bound each poll by the remaining caller-approved wait.
                # Never pass a negative timeout if the deadline expires between
                # the preceding check and this calculation.
                remaining_seconds = max(0.0, deadline - time.monotonic())
                # Poll for at most one short slice and never beyond the total
                # remaining budget; then the while loop tries produce() again.
                self._producer.poll(
                    min(QUEUE_CAPACITY_POLL_INTERVAL_SECONDS, remaining_seconds)
                )

            # The Protocol cannot name every exception from every possible
            # adapter. This broad boundary catch converts any local client
            # rejection into the package's stable result contract.
            except Exception as error:  # noqa: BLE001 - boundary translation
                # Typically an oversized message or a malformed argument.
                # Returned rather than raised so one bad message cannot halt
                # a bulk send loop.
                # Do not include message data in the log; payloads may contain
                # personal or secret information.
                logger.error("send(topic=%s) rejected by client: %s", topic, error)
                # CLIENT_REJECTED is not automatically retryable because an
                # oversized or malformed message will fail the same way again.
                return SendResult(
                    topic=topic,
                    message_key=key,
                    queued=False,
                    failure_reason=SendFailureReason.CLIENT_REJECTED,
                    error_message=str(error),
                )

    # ============================================================
    # STEP 3: SERVICE RESULTS EXPLICITLY
    # ============================================================

    def poll(self, timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS) -> int:
        """Process delivery results that have already arrived.

        Fire-and-forget sends do not register delivery callbacks, so they do
        not need a poll after every successful enqueue. This method remains
        useful when the supplied client configuration registers other event,
        statistics, error, or logging callbacks. Back-pressure handling also
        polls briefly before retrying a full local queue, as recommended by
        the client API.

        Args:
            timeout_seconds: How long to wait if nothing is ready yet. 0.0
                means "handle what is ready now and return immediately".

        Returns:
            The number of events processed.

        Failure behaviour:
            Returns 0 when the producer is already closed.
        """
        # Validate before checking lifecycle so a caller bug behaves the same
        # whether the producer happens to be open or closed.
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be zero or greater.")
        # A closed producer has no client events left for this object to serve.
        if self._producer is None:
            return 0
        # Delegate callback/event processing to the concrete Kafka client.
        return self._producer.poll(timeout_seconds)

    # ============================================================
    # STEP 4: WAIT FOR QUEUED MESSAGES (Stage 5.0)
    # ============================================================

    def flush(self, timeout_seconds: float = DEFAULT_FLUSH_TIMEOUT_SECONDS) -> int:
        """Wait for queued messages to reach a final outcome.

        Why this matters:
            `produce()` only confirms local queueing. If the process exits
            with work still outstanding, that work is abandoned. A flush
            gives it time to reach either delivery or permanent failure; this
            pattern does not reveal which outcome occurred per message.

        Args:
            timeout_seconds: Maximum time to wait before giving up.

        Returns:
            How many messages were still outstanding when it returned. 0 means
            everything reached an outcome. A non-zero value means those
            messages may be lost, and is logged as a warning.

        Failure behaviour:
            Returns 0 when already closed. Never raises: a failure to flush
            is reported through the return value and the log, so shutdown
            paths cannot be derailed by it.
        """
        # A negative timeout is invalid rather than a request to wait forever.
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be zero or greater.")
        # close() already flushed and detached the client, so repeated calls are
        # harmless and report no outstanding work.
        if self._producer is None:
            return 0

        # Flush is a shutdown/coordination boundary. Convert client exceptions
        # into a sentinel result so cleanup code can continue running.
        try:
            # The client returns the number of messages still unresolved when
            # the timeout expires; zero means every message reached an outcome.
            outstanding = self._producer.flush(timeout_seconds)
        except Exception as error:  # noqa: BLE001 - shutdown must not raise
            # -1 cannot be confused with a real outstanding-message count.
            logger.error("flush() failed after %.1fs: %s", timeout_seconds, error)
            return -1

        # A positive count tells operators that shutdown may discard messages.
        if outstanding > 0:
            logger.warning(
                "flush(): %d message(s) still unresolved after %.1fs; they may be lost.",
                outstanding,
                timeout_seconds,
            )
        # Preserve the client's count so the application may enforce its own
        # shutdown policy or alert threshold.
        return outstanding

    # ============================================================
    # STEP 5: SHUTDOWN
    # ============================================================

    def close(self) -> None:
        """Flush pending messages and make this instance unusable.

        Always call this before the application exits - or use the context
        manager form, which calls it for you. It uses the dedicated,
        platform-aware `SHUTDOWN_FLUSH_TIMEOUT_SECONDS`; shutdown is the last
        chance for a queued message to finish before the process exits.

        Safe to call more than once; later calls do nothing.

        Failure behaviour:
            Never raises. Messages that remain unresolved after the timeout
            are logged as potentially lost rather than silently dropped.
        """
        # Idempotent means repeated calls have the same safe effect. Returning
        # here prevents a second close from flushing an already-detached client.
        if self._producer is None:
            return

        # Record the lifecycle transition without exposing configuration data.
        logger.info("FireAndForgetProducer closing; flushing queued messages...")
        # Use the shutdown-specific budget because the process may soon be killed
        # by its service manager or container platform.
        outstanding = self.flush(SHUTDOWN_FLUSH_TIMEOUT_SECONDS)
        # Zero means all queued work reached some final outcome before timeout.
        if outstanding == 0:
            logger.info("Close: all queued messages resolved.")
        # flush() reserves -1 for an exception where the remaining count is unknown.
        elif outstanding < 0:
            logger.error("Close: flush failed; delivery state is unknown.")
        # Any positive count represents messages that were still unresolved.
        else:
            logger.warning("Close: %d message(s) may have been lost.", outstanding)

        # Detach the client even after a failed flush. This prevents later code
        # from pretending the closed instance can safely accept new messages.
        self._producer = None

    # ============================================================
    # STEP 6: CONTEXT MANAGER SUPPORT
    # ============================================================

    def __enter__(self) -> Self:
        """Allow `with FireAndForgetProducer() as producer:` usage."""
        # A context manager returns the object bound after ``as`` in a with block.
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the producer on block exit, including when an error occurred.

        This is why the context manager form is the recommended one: messages
        are flushed even if the surrounding code raises.
        """
        # Always close, whether the block succeeded or raised. Returning None
        # deliberately does not suppress the surrounding exception.
        self.close()
