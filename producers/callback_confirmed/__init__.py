"""
Public API for the `producers.callback_confirmed` package.

Why this file exists:
- centralize supported imports for learners and application code
- keep internal file structure flexible without breaking external consumers
- provide one discoverable surface for callback-confirmed producer capabilities

Recommended starting point:

    from producers.callback_confirmed import CallbackConfirmedProducer

    publisher = CallbackConfirmedProducer(topic="orders.created.v1")
    try:
        publisher.publish({
            "event_id": "evt-1",
            "event_type": "order.created",
            "order_id": "order-1",
        })
        publisher.poll(0.0)
    finally:
        summary = publisher.finish()

``publish`` means local acceptance. ``summary.all_confirmed`` means every
accepted message later produced a successful Kafka delivery callback. Neither
statement proves that a consumer completed its business work.

This package exposes two producer entry points; pick the one that matches
your lifecycle:
- ``CallbackConfirmedProducer`` (above): a long-lived publisher for a process
  that sends many events and should reuse one Kafka client. Prefer this.
- ``produce_message``: a one-shot function for a single send in a short
  script, cron job, or migration task. See `producers.callback_confirmed.core`
  for when to reach for it instead.
"""

# Constants: stable timing defaults used by one-shot producer scripts.
from .constants import (
    DEFAULT_FLUSH_TIMEOUT_SECONDS,
    DEFAULT_METADATA_TIMEOUT_SECONDS,
    DEFAULT_POLL_TIMEOUT_SECONDS,
    DEFAULT_QUEUE_CAPACITY_WAIT_SECONDS,
    QUEUE_CAPACITY_POLL_INTERVAL_SECONDS,
)

# Reusable configuration: reliable defaults with environment-based broker lookup.
from .config import (
    build_callback_producer_config,
    read_bootstrap_servers,
)

# Core workflow: validation, readiness checks, and produce execution APIs.
from .core import (
    Environment,
    create_callback_confirmed_producer,
    delivery_report,
    explain_producer_config,
    get_producer_config,
    produce_message,
    sanitize_config_for_logging,
    serialize_message_value,
    validate_produce_request,
    verify_kafka_readiness,
)

# Demo helpers: educational event generation and round-trip verification utilities.
from .demo import (
    build_demo_config_loader,
    build_order_created_demo_event,
    consume_demo_event_from_topic,
    ensure_demo_topic_exists,
    log_pre_produce_checklist,
    log_producer_teaching_summary,
    read_demo_bootstrap_servers,
    run_callback_confirmed_demo,
    run_demo_round_trip,
)

# Primary notebook-based API: create once, publish many, poll, then finish once.
from .publisher import CallbackConfirmedProducer

# Result models: structured outcomes used by caller logic and troubleshooting.
from .types import (
    ConsumedEventVerification,
    DeliveryFailure,
    DeliverySummary,
    DemoRoundTripResult,
    JsonObject,
    JsonValue,
    KafkaReadinessReport,
    ProduceMessageResult,
)

__all__ = [
    "DEFAULT_POLL_TIMEOUT_SECONDS",  # Normal callback-service wait limit.
    "DEFAULT_FLUSH_TIMEOUT_SECONDS",  # Final publisher drain wait limit.
    "DEFAULT_METADATA_TIMEOUT_SECONDS",  # Compatible readiness-check limit.
    "DEFAULT_QUEUE_CAPACITY_WAIT_SECONDS",  # Total queue-full retry budget.
    "QUEUE_CAPACITY_POLL_INTERVAL_SECONDS",  # One queue-full poll slice.
    "build_callback_producer_config",  # Creates reviewed Kafka defaults.
    "read_bootstrap_servers",  # Reads broker addresses from the environment.
    "CallbackConfirmedProducer",  # Primary publish/poll/finish abstraction.
    "DeliveryFailure",  # One compact event-id/error diagnostic sample.
    "DeliverySummary",  # Honest final callback-aware counters.
    "JsonObject",  # Top-level JSON event shape accepted by publish().
    "JsonValue",  # Any nested value representable in JSON.
    "Environment",
    "get_producer_config",
    "KafkaReadinessReport",
    "ProduceMessageResult",
    "ConsumedEventVerification",
    "DemoRoundTripResult",
    "sanitize_config_for_logging",
    "explain_producer_config",
    "validate_produce_request",
    "serialize_message_value",
    "create_callback_confirmed_producer",
    "verify_kafka_readiness",
    "delivery_report",
    "produce_message",
    "build_order_created_demo_event",
    "log_producer_teaching_summary",
    "log_pre_produce_checklist",
    "read_demo_bootstrap_servers",
    "run_callback_confirmed_demo",
    "build_demo_config_loader",
    "ensure_demo_topic_exists",
    "consume_demo_event_from_topic",
    "run_demo_round_trip",
]
