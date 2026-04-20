"""
Core callback-confirmed producer workflow with explicit preflight decision points.

Why this module exists:
- provide a clear, testable producer path for learning and production-minded use
- separate input validation, readiness checks, and send execution responsibilities
- return structured outcomes that support debugging and interview-style explanation

ASCII flow:
    Stage 1.0 Validate input
          |
          v
    Stage 2.0 Load and explain config
          |
          v
    Stage 3.0 Verify Kafka readiness (optional but recommended)
          |
          v
    Stage 4.0 Produce + poll + flush
          |
          v
    Stage 5.0 Return ProduceMessageResult with actionable status
"""

from __future__ import annotations

# json serialization is the wire-format boundary for event payloads.
import json

# stable logger used by tests and operational dashboards.
import logging

# typing keeps workflow contracts explicit and tooling-friendly.
from typing import Any, Dict, Optional

from .clients import default_admin_client_factory, default_producer_factory
from .constants import (
    DEFAULT_FLUSH_TIMEOUT_SECONDS,
    DEFAULT_METADATA_TIMEOUT_SECONDS,
    DEFAULT_POLL_TIMEOUT_SECONDS,
)
from .types import (
    AdminClientFactory,
    ConfigLoader,
    DeliveryCallback,
    KafkaReadinessReport,
    ProduceMessageResult,
    ProducerFactory,
)

# Primary import path for repository execution.
try:
    from config.kafka_config import Environment, get_producer_config
except ImportError:  # pragma: no cover - package install fallback.
    # Fallback import path when package is installed under `kafka.*`.
    from kafka.config.kafka_config import Environment, get_producer_config

logger = logging.getLogger("producers.callback_confirmed_producer")

# These markers intentionally cover common credential naming patterns seen in
# Kafka, OAuth, cloud-secret, and certificate-based configurations.
SENSITIVE_CONFIG_KEY_MARKERS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "privatekey",
    "keyfile",
    "key_file",
    "credential",
    "oauth",
    "jwt",
    "sasl.jaas.config",
)


def _is_sensitive_config_key(config_key: str) -> bool:
    """
    Determine whether a config key is likely to contain secret material.

    This intentionally uses conservative substring matching to reduce accidental
    leakage in logs. False positives are acceptable; secret leaks are not.
    """

    normalized_key = config_key.lower().replace("-", "_")
    return any(marker in normalized_key for marker in SENSITIVE_CONFIG_KEY_MARKERS)


def sanitize_config_for_logging(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Redact secrets before writing configuration to logs.

    Input:
    - config: producer configuration dictionary that may contain credentials.
    Output:
    - a shallow copy with password/secret keys masked.
    """

    redacted_config: Dict[str, Any] = {}
    for config_key, config_value in config.items():
        if _is_sensitive_config_key(config_key):
            redacted_config[config_key] = "***REDACTED***"
        else:
            redacted_config[config_key] = config_value
    return redacted_config


def explain_producer_config(config: Dict[str, Any]) -> Dict[str, str]:
    """
    Return human-readable explanations for known producer configuration keys.

    Why:
    Interview and onboarding discussions are stronger when each config key has a
    clear purpose/tradeoff description.
    """

    config_explanations = {
        "bootstrap.servers": "Initial broker endpoints used for metadata bootstrap.",
        "client.id": "Logical producer identity for logs and monitoring.",
        "acks": "Durability threshold for successful publish acknowledgement.",
        "enable.idempotence": "Reduces duplicate writes caused by retries.",
        "retries": "How patient the producer is with transient failures.",
        "retry.backoff.ms": "Delay between retry attempts.",
        "linger.ms": "Small wait to improve batching efficiency.",
        "batch.size": "Upper bound for per-partition batch size.",
        "compression.type": "CPU vs network/storage trade-off.",
        "message.max.bytes": "Maximum publish payload size guardrail.",
        "max.in.flight.requests.per.connection": "Outstanding request concurrency per connection.",
    }
    return {
        config_key: config_explanations[config_key]
        for config_key in config
        if config_key in config_explanations
    }


def validate_produce_request(
    topic_name: str, message_key: Optional[str], message_value: Any
) -> None:
    """
    Validate business-level input before touching Kafka clients.

    Failure behavior:
    - raises ValueError for invalid topic/key/value inputs.
    """

    if not isinstance(topic_name, str) or not topic_name.strip():
        raise ValueError("topic_name must be a non-empty string")
    if message_key is not None and (not isinstance(message_key, str) or not message_key.strip()):
        raise ValueError("message_key must be a meaningful non-empty string when provided")
    if message_value is None:
        raise ValueError("message_value cannot be None")


def serialize_message_value(message_value: Any) -> bytes:
    """
    Serialize payload to deterministic UTF-8 JSON bytes.

    Using sorted keys makes test expectations stable and helps diff-based debugging.
    """

    return json.dumps(message_value, sort_keys=True).encode("utf-8")


def create_callback_confirmed_producer(
    *,
    environment: Environment = Environment.PRODUCTION,
    config_loader: ConfigLoader = get_producer_config,
    producer_factory: ProducerFactory = default_producer_factory,
):
    """
    Build a producer instance for the requested environment.

    This helper is useful when callers want direct producer access while reusing
    the same configuration and logging standards as the callback-confirmed flow.
    """

    producer_config = config_loader(environment=environment)
    logger.debug(
        "Creating producer with config: %s",
        sanitize_config_for_logging(producer_config),
    )
    return producer_factory(producer_config)


def verify_kafka_readiness(
    topic_name: str,
    producer_config: Dict[str, Any],
    *,
    admin_client_factory: AdminClientFactory = default_admin_client_factory,
    metadata_timeout: float = DEFAULT_METADATA_TIMEOUT_SECONDS,
) -> KafkaReadinessReport:
    """
    Run metadata preflight checks before publishing.

    Decision contract:
    - returns `ready=False` with actionable error details when brokers/topic are unavailable
    - returns `ready=True` with broker/topic stats when preflight checks pass
    """

    # Stage 1.0: Validate bootstrap endpoint presence in producer configuration.
    bootstrap_servers = str(producer_config.get("bootstrap.servers", "")).strip()
    if not bootstrap_servers:
        return KafkaReadinessReport(
            ready=False,
            bootstrap_servers="",
            broker_count=0,
            topic_name=topic_name,
            topic_exists=False,
            topic_partition_count=0,
            error_message="Producer config is missing `bootstrap.servers`.",
        )

    # Stage 1.1: Fetch metadata through the selected admin client abstraction.
    try:
        admin_client = admin_client_factory(producer_config)
        cluster_metadata = admin_client.list_topics(timeout=metadata_timeout)
        broker_metadata = getattr(cluster_metadata, "brokers", {}) or {}
        topic_metadata_map = getattr(cluster_metadata, "topics", {}) or {}
    except Exception as metadata_error:
        return KafkaReadinessReport(
            ready=False,
            bootstrap_servers=bootstrap_servers,
            broker_count=0,
            topic_name=topic_name,
            topic_exists=False,
            topic_partition_count=0,
            error_message=(
                "Kafka metadata could not be fetched. Check broker reachability, "
                "listener configuration, and network access. Root cause: "
                f"{metadata_error}"
            ),
        )

    # Stage 1.2: Require at least one visible broker for readiness success.
    if not broker_metadata:
        return KafkaReadinessReport(
            ready=False,
            bootstrap_servers=bootstrap_servers,
            broker_count=0,
            topic_name=topic_name,
            topic_exists=False,
            topic_partition_count=0,
            error_message="Kafka metadata returned zero brokers.",
        )

    # Stage 1.3: Ensure target topic exists and does not report metadata error.
    topic_metadata = topic_metadata_map.get(topic_name)
    if topic_metadata is None:
        return KafkaReadinessReport(
            ready=False,
            bootstrap_servers=bootstrap_servers,
            broker_count=len(broker_metadata),
            topic_name=topic_name,
            topic_exists=False,
            topic_partition_count=0,
            error_message=(
                f"Topic '{topic_name}' was not found in broker metadata. "
                "Create the topic before producing."
            ),
        )

    topic_error = getattr(topic_metadata, "error", None)
    if topic_error:
        return KafkaReadinessReport(
            ready=False,
            bootstrap_servers=bootstrap_servers,
            broker_count=len(broker_metadata),
            topic_name=topic_name,
            topic_exists=False,
            topic_partition_count=0,
            error_message=f"Topic '{topic_name}' metadata returned an error: {topic_error}",
        )

    topic_partitions = getattr(topic_metadata, "partitions", {}) or {}
    return KafkaReadinessReport(
        ready=True,
        bootstrap_servers=bootstrap_servers,
        broker_count=len(broker_metadata),
        topic_name=topic_name,
        topic_exists=True,
        topic_partition_count=len(topic_partitions),
    )


def delivery_report(delivery_error: Optional[Exception], delivered_message: Any) -> None:
    """
    Standard delivery callback for produce operations.

    Input:
    - delivery_error: exception from client callback on send failure.
    - delivered_message: metadata object exposing topic/partition/offset accessors.
    """

    if delivery_error is not None:
        logger.error("Message delivery failed: %s", delivery_error)
        return
    logger.info(
        "Message delivered to %s [%s] @ offset %s",
        delivered_message.topic(),
        delivered_message.partition(),
        delivered_message.offset(),
    )


def produce_message(
    topic_name: str,
    message_key: Optional[str],
    message_value: Any,
    *,
    environment: Environment = Environment.PRODUCTION,
    config_loader: ConfigLoader = get_producer_config,
    producer_factory: ProducerFactory = default_producer_factory,
    admin_client_factory: AdminClientFactory = default_admin_client_factory,
    delivery_callback: DeliveryCallback = delivery_report,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    flush_timeout: float = DEFAULT_FLUSH_TIMEOUT_SECONDS,
    metadata_timeout: float = DEFAULT_METADATA_TIMEOUT_SECONDS,
    verify_readiness: bool = True,
    raise_on_error: bool = False,
) -> ProduceMessageResult:
    """
    Produce one message with optional readiness preflight and structured outcomes.

    Critical decision points:
    - readiness check can fail fast before producing
    - flush result decides success/failure for short-lived scripts
    - `raise_on_error` lets callers choose strict exception semantics
    """

    # Stage 1.0: Validate caller inputs before networking or serialization.
    validate_produce_request(topic_name, message_key, message_value)

    # Stage 2.0: Load environment-specific producer configuration.
    producer_config = config_loader(environment=environment)

    # Default readiness object for flows that skip metadata preflight.
    readiness_report = KafkaReadinessReport(
        ready=True,
        bootstrap_servers=str(producer_config.get("bootstrap.servers", "")),
        broker_count=0,
        topic_name=topic_name,
        topic_exists=True,
        topic_partition_count=0,
    )

    # Stage 3.0: Metadata preflight (recommended for deterministic failures).
    if verify_readiness:
        readiness_report = verify_kafka_readiness(
            topic_name,
            producer_config,
            admin_client_factory=admin_client_factory,
            metadata_timeout=metadata_timeout,
        )
        if not readiness_report.ready:
            logger.error("Kafka preflight failed: %s", readiness_report.error_message)
            if raise_on_error:
                raise ConnectionError(readiness_report.error_message)
            return ProduceMessageResult(
                topic_name=topic_name,
                message_key=message_key,
                success=False,
                serialized_size_bytes=0,
                flush_remaining_messages=0,
                readiness=readiness_report,
                error_message=readiness_report.error_message,
            )

    # Stage 4.0: Build producer dependency and prepare callback-tracked send.
    delivery_successful = False
    error_message: Optional[str] = None
    remaining_messages_after_flush = 0
    serialized_size_bytes = 0
    callback_confirmed = False
    callback_delivery_error: Optional[Exception] = None

    try:
        # Stage 4.1: Create producer client and serialize payload.
        producer_client = producer_factory(producer_config)
        payload_bytes = serialize_message_value(message_value)
        serialized_size_bytes = len(payload_bytes)

        # Stage 4.2: Wrap caller callback so success reflects callback-confirmed
        # delivery, not only queue drain semantics.
        def callback_tracking_wrapper(
            delivery_error: Optional[Exception],
            delivered_message: Any,
        ) -> None:
            nonlocal callback_confirmed, callback_delivery_error

            callback_confirmed = True
            if delivery_error is not None:
                callback_delivery_error = delivery_error

            delivery_callback(delivery_error, delivered_message)

        # Stage 4.3: Queue one message for asynchronous delivery.
        producer_client.produce(
            topic=topic_name,
            key=message_key,
            value=payload_bytes,
            callback=callback_tracking_wrapper,
        )

        # Stage 4.4: Allow callbacks to execute, then drain producer queue.
        producer_client.poll(poll_timeout)
        remaining_messages_after_flush = producer_client.flush(timeout=flush_timeout)

        # Stage 4.5: Evaluate callback-confirmed success criteria.
        # For this pattern, success requires:
        # - flush drained queue (`remaining == 0`)
        # - delivery callback actually fired
        # - callback delivered no error from broker/client path
        if remaining_messages_after_flush != 0:
            delivery_successful = False
            error_message = (
                f"{remaining_messages_after_flush} message(s) remained queued after "
                f"waiting {flush_timeout} second(s)"
            )
            logger.warning(error_message)
            if raise_on_error:
                raise TimeoutError(error_message)
        elif not callback_confirmed:
            delivery_successful = False
            error_message = (
                "Delivery callback was not observed after poll/flush; send outcome "
                "cannot be confirmed."
            )
            logger.warning(error_message)
            if raise_on_error:
                raise RuntimeError(error_message)
        elif callback_delivery_error is not None:
            delivery_successful = False
            error_message = f"Delivery callback reported failure: {callback_delivery_error}"
            logger.error(error_message)
            if raise_on_error:
                raise RuntimeError(error_message) from callback_delivery_error
        else:
            delivery_successful = True

    except Exception as produce_error:
        error_message = str(produce_error)
        logger.exception("Failed to produce message to topic '%s'", topic_name)
        if raise_on_error:
            raise

    # Stage 5.0: Return complete structured outcome for caller/operator use.
    return ProduceMessageResult(
        topic_name=topic_name,
        message_key=message_key,
        success=delivery_successful,
        serialized_size_bytes=serialized_size_bytes,
        flush_remaining_messages=remaining_messages_after_flush,
        readiness=readiness_report,
        error_message=error_message,
    )
