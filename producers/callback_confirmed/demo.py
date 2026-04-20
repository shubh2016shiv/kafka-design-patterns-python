"""
Teaching-focused demo helpers for the callback-confirmed producer.

Why this module exists:
- provide interview-friendly, realistic event examples and decision logs
- show critical produce workflow choices before and after sending
- offer optional consume-back verification for end-to-end confidence

ASCII flow:
    Stage 1.0 Build demo event and topic inputs
         |
         v
    Stage 2.0 Explain config + pre-produce checklist
         |
         v
    Stage 3.0 Ensure topic exists for demo environment
         |
         v
    Stage 4.0 Produce event using core flow
         |
         v
    Stage 5.0 Optionally consume back and verify run id
"""

from __future__ import annotations

# json: pretty event logging and consume payload deserialization.
import json

# logging: instructional breadcrumbs and operational diagnostics.
import logging

# time: unique run identifiers and event timestamps.
import time

# pathlib: deterministic location lookup for local infrastructure env file.
from pathlib import Path

# typing: explicit contracts for event payload and config dictionaries.
from typing import Any, Dict

from .clients import (
    AdminClient,
    ConfluentNewTopic,
    KafkaPythonAdminClient,
    KafkaPythonConsumer,
    KafkaPythonNewTopic,
    TopicAlreadyExistsError,
)
from .core import (
    ConfigLoader,
    Environment,
    explain_producer_config,
    get_producer_config,
    produce_message,
    sanitize_config_for_logging,
)
from .types import (
    ConsumedEventVerification,
    DemoRoundTripResult,
    KafkaReadinessReport,
    ProduceMessageResult,
)

logger = logging.getLogger(__name__)


def _is_topic_already_exists_error(create_error: Exception) -> bool:
    """
    Decide whether a topic-create error means "topic already exists".

    Decision order:
    1. Typed exception checks (most reliable).
    2. Structured error attributes (`code`, `name`) when available.
    3. Message text fallback as a final compatibility guard.
    """

    # Stage 1.0: kafka-python explicit typed exception.
    if TopicAlreadyExistsError is not None and isinstance(create_error, TopicAlreadyExistsError):
        return True

    # Stage 1.1: Inspect direct error object and nested Kafka error object.
    candidate_errors = [create_error]
    if create_error.args:
        candidate_errors.append(create_error.args[0])

    for candidate_error in candidate_errors:
        error_name_attr = getattr(candidate_error, "name", None)
        if callable(error_name_attr):
            try:
                if error_name_attr() == "TOPIC_ALREADY_EXISTS":
                    return True
            except Exception:  # pragma: no cover - defensive inspection path.
                pass
        elif error_name_attr == "TOPIC_ALREADY_EXISTS":
            return True

        error_code_attr = getattr(candidate_error, "code", None)
        if callable(error_code_attr):
            try:
                error_code_value = error_code_attr()
                if error_code_value in ("TOPIC_ALREADY_EXISTS", 36):
                    return True
            except Exception:  # pragma: no cover - defensive inspection path.
                pass
        elif error_code_attr in ("TOPIC_ALREADY_EXISTS", 36):
            return True

        if "TopicAlreadyExists" in type(candidate_error).__name__:
            return True

    # Stage 1.2: Last resort message pattern for older/opaque client errors.
    error_text = str(create_error)
    return "TOPIC_ALREADY_EXISTS" in error_text or "TopicAlreadyExists" in error_text


def build_order_created_demo_event() -> Dict[str, Any]:
    """
    Create a realistic business event for producer demonstrations.

    This payload intentionally includes versioning, identity, and timestamp fields
    so learners can discuss schema evolution and partitioning decisions.
    """

    current_epoch_ms = int(time.time() * 1000)
    return {
        "event_name": "order_created",
        "event_version": "v1",
        "event_timestamp_epoch_ms": current_epoch_ms,
        "demo_run_id": f"demo-run-{current_epoch_ms}",
        "order_id": "ord-demo-20260420-0001",
        "customer_id": "customer-1024",
        "currency": "INR",
        "order_total": 1499.00,
        "source_service": "checkout-service",
        "notes": "Demo event used to teach simple Kafka producer flow.",
    }


def log_producer_teaching_summary(producer_config: Dict[str, Any]) -> None:
    """
    Log sanitized config and explain important producer knobs.

    Why:
    Teams learn faster when each setting is tied to a reliability or performance
    decision instead of being treated as magic boilerplate.
    """

    logger.info("Producer config (sanitized): %s", sanitize_config_for_logging(producer_config))
    for config_key, explanation in explain_producer_config(producer_config).items():
        logger.info("Config %s -> %s", config_key, explanation)


def log_pre_produce_checklist(
    topic_name: str, message_key: str, message_value: Dict[str, Any]
) -> None:
    """
    Log a decision checklist before sending a demo event.

    Checklist helps learners answer:
    - why this topic, key, and payload shape were chosen
    - what assumptions consumers rely on
    """

    logger.info("Pre-produce checklist:")
    logger.info("1. Topic '%s' should already exist in production-style environments.", topic_name)
    logger.info("2. Key '%s' should represent a real business partitioning choice.", message_key)
    logger.info("3. Event should carry a stable name, version, IDs, and timestamp.")
    logger.info("4. Payload should stay reasonably small and JSON serializable.")
    logger.info("5. Consumers should already understand this event schema/version.")
    logger.info("Event preview: %s", message_value)


def read_demo_bootstrap_servers() -> str:
    """
    Resolve bootstrap servers for local/VM demo execution.

    Decision logic:
    - prefer `infrastructure/.env` values when available
    - fall back to localhost defaults for quick start usability
    """

    env_file_path = Path(__file__).resolve().parents[2] / "infrastructure" / ".env"
    default_host = "localhost"
    default_port = "19094"
    if not env_file_path.exists():
        return f"{default_host}:{default_port}"

    env_values: Dict[str, str] = {}
    for raw_line in env_file_path.read_text(encoding="utf-8").splitlines():
        normalized_line = raw_line.strip()
        if not normalized_line or normalized_line.startswith("#") or "=" not in normalized_line:
            continue
        key, value = normalized_line.split("=", maxsplit=1)
        env_values[key.strip()] = value.strip()

    host = env_values.get("KAFKA_EXTERNAL_HOST", default_host)
    port = env_values.get("KAFKA_EXTERNAL_PORT", default_port)
    return f"{host}:{port}"


def build_demo_config_loader(bootstrap_servers: str) -> ConfigLoader:
    """
    Build a config loader that pins demo runs to known bootstrap endpoints.

    Why:
    Demo scenarios should remain deterministic even when default environment
    configuration changes elsewhere in the project.
    """

    def _config_loader(*, environment: Environment) -> Dict[str, Any]:
        producer_config = get_producer_config(environment=environment)
        producer_config["bootstrap.servers"] = bootstrap_servers
        producer_config["client.id"] = "simple-producer-demo"
        return producer_config

    return _config_loader


def ensure_demo_topic_exists(topic_name: str, bootstrap_servers: str) -> None:
    """
    Create demo topic if needed using available admin backend.

    Stage 1.0:
    Prefer Confluent AdminClient for create-topic operation.
    Stage 1.1:
    Fall back to kafka-python admin client if Confluent is unavailable.
    Stage 1.2:
    Raise ImportError when no admin backend is installed.
    """

    if AdminClient is not None and ConfluentNewTopic is not None:
        admin_client = AdminClient({"bootstrap.servers": bootstrap_servers})
        create_futures = admin_client.create_topics(
            [ConfluentNewTopic(topic=topic_name, num_partitions=1, replication_factor=1)]
        )
        topic_future = create_futures.get(topic_name)
        if topic_future is not None:
            try:
                topic_future.result()
            except Exception as create_error:  # noqa: BLE001
                if not _is_topic_already_exists_error(create_error):
                    raise
        return

    if KafkaPythonAdminClient is not None and KafkaPythonNewTopic is not None:
        admin_client = KafkaPythonAdminClient(
            bootstrap_servers=bootstrap_servers,
            client_id="simple-producer-demo-admin",
            request_timeout_ms=15000,
        )
        try:
            admin_client.create_topics(
                new_topics=[
                    KafkaPythonNewTopic(name=topic_name, num_partitions=1, replication_factor=1)
                ],
                validate_only=False,
            )
        except Exception as create_error:  # noqa: BLE001
            if not _is_topic_already_exists_error(create_error):
                raise
        finally:
            admin_client.close()
        return

    raise ImportError("No Kafka admin client is available to create the demo topic.")


def consume_demo_event_from_topic(
    topic_name: str,
    bootstrap_servers: str,
    expected_demo_run_id: str,
    *,
    timeout_seconds: int = 20,
) -> ConsumedEventVerification:
    """
    Verify round-trip delivery by consuming until matching `demo_run_id` is found.

    Failure behavior:
    - returns structured failure result when consumer dependency is unavailable
    - returns timeout failure when expected event is not observed in time
    """

    if KafkaPythonConsumer is None:
        return ConsumedEventVerification(
            consumed=False,
            topic_name=topic_name,
            expected_demo_run_id=expected_demo_run_id,
            error_message=(
                "Round-trip consume verification requires `kafka-python` in this environment."
            ),
        )

    verification_consumer = KafkaPythonConsumer(
        topic_name,
        bootstrap_servers=bootstrap_servers,
        group_id=f"simple-producer-demo-{expected_demo_run_id[-8:]}",
        auto_offset_reset="earliest",
        enable_auto_commit=True,
        consumer_timeout_ms=timeout_seconds * 1000,
        request_timeout_ms=max(timeout_seconds * 1000, 20000),
        value_deserializer=lambda value: json.loads(value.decode("utf-8")),
        key_deserializer=lambda value: value.decode("utf-8") if value else None,
    )
    try:
        for consumed_record in verification_consumer:
            event_payload = consumed_record.value if isinstance(consumed_record.value, dict) else {}
            if event_payload.get("demo_run_id") == expected_demo_run_id:
                return ConsumedEventVerification(
                    consumed=True,
                    topic_name=topic_name,
                    expected_demo_run_id=expected_demo_run_id,
                    received_event=event_payload,
                )
        return ConsumedEventVerification(
            consumed=False,
            topic_name=topic_name,
            expected_demo_run_id=expected_demo_run_id,
            error_message="Produced event was not consumed before timeout.",
        )
    finally:
        verification_consumer.close()


def run_demo_round_trip() -> DemoRoundTripResult:
    """
    Execute full teaching demo: setup, produce, and consume-back verification.

    Returns:
    - DemoRoundTripResult with both produce and consume verification outcomes.
    """

    # Stage 1.0: Build demo inputs and deterministic local config loader.
    bootstrap_servers = read_demo_bootstrap_servers()
    demo_event = build_order_created_demo_event()
    demo_run_id = str(demo_event["demo_run_id"])
    demo_topic_name = f"demo-order-events-v1-{demo_run_id[-8:]}"
    demo_message_key = str(demo_event["customer_id"])
    demo_config_loader = build_demo_config_loader(bootstrap_servers)
    producer_config = demo_config_loader(environment=Environment.DEVELOPMENT)

    logger.info("Demo bootstrap servers: %s", bootstrap_servers)
    logger.info("Demo topic: %s", demo_topic_name)
    logger.info(
        "What event is being sent:\n%s",
        json.dumps(demo_event, indent=2, sort_keys=True),
    )

    # Stage 2.0: Ensure topic exists before produce attempt.
    try:
        ensure_demo_topic_exists(demo_topic_name, bootstrap_servers)
    except Exception as topic_setup_error:
        error_message = (
            "Could not ensure demo topic exists. This usually means Kafka is not "
            f"reachable from this machine. Root cause: {topic_setup_error}"
        )
        logger.error(error_message)
        failed_produce_result = ProduceMessageResult(
            topic_name=demo_topic_name,
            message_key=demo_message_key,
            success=False,
            serialized_size_bytes=0,
            flush_remaining_messages=0,
            readiness=KafkaReadinessReport(
                ready=False,
                bootstrap_servers=bootstrap_servers,
                broker_count=0,
                topic_name=demo_topic_name,
                topic_exists=False,
                topic_partition_count=0,
                error_message=error_message,
            ),
            error_message=error_message,
        )
        return DemoRoundTripResult(
            produce_result=failed_produce_result,
            consume_verification=ConsumedEventVerification(
                consumed=False,
                topic_name=demo_topic_name,
                expected_demo_run_id=demo_run_id,
                error_message="Topic setup failed, so produce/consume steps were skipped.",
            ),
        )

    # Stage 3.0: Explain key config and preflight reasoning to the learner.
    log_producer_teaching_summary(producer_config)
    log_pre_produce_checklist(demo_topic_name, demo_message_key, demo_event)

    # Stage 4.0: Produce event through the shared core workflow.
    produce_result = produce_message(
        demo_topic_name,
        demo_message_key,
        demo_event,
        environment=Environment.DEVELOPMENT,
        config_loader=demo_config_loader,
        verify_readiness=True,
        raise_on_error=False,
    )
    if not produce_result.success:
        return DemoRoundTripResult(
            produce_result=produce_result,
            consume_verification=ConsumedEventVerification(
                consumed=False,
                topic_name=demo_topic_name,
                expected_demo_run_id=demo_run_id,
                error_message="Produce step failed, consume-back verification skipped.",
            ),
        )

    # Stage 5.0: Consume-back verification for end-to-end confidence.
    consume_verification = consume_demo_event_from_topic(
        demo_topic_name,
        bootstrap_servers,
        demo_run_id,
    )
    return DemoRoundTripResult(
        produce_result=produce_result,
        consume_verification=consume_verification,
    )
