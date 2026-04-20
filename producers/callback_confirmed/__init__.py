"""
Public API for the `producers.callback_confirmed` package.

Why this file exists:
- centralize supported imports for learners and application code
- keep internal file structure flexible without breaking external consumers
- provide one discoverable surface for callback-confirmed producer capabilities
"""

# Constants: stable timing defaults used by one-shot producer scripts.
from .constants import (
    DEFAULT_FLUSH_TIMEOUT_SECONDS,
    DEFAULT_METADATA_TIMEOUT_SECONDS,
    DEFAULT_POLL_TIMEOUT_SECONDS,
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
    run_demo_round_trip,
)

# Result models: structured outcomes used by caller logic and troubleshooting.
from .types import (
    ConsumedEventVerification,
    DemoRoundTripResult,
    KafkaReadinessReport,
    ProduceMessageResult,
)

__all__ = [
    "DEFAULT_POLL_TIMEOUT_SECONDS",
    "DEFAULT_FLUSH_TIMEOUT_SECONDS",
    "DEFAULT_METADATA_TIMEOUT_SECONDS",
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
    "build_demo_config_loader",
    "ensure_demo_topic_exists",
    "consume_demo_event_from_topic",
    "run_demo_round_trip",
]
