"""
Public API for the `producers.fire_and_forget` package.

Why this file exists:
- centralize all supported imports behind a single stable surface
- let internal module structure change without breaking external consumers
- provide one discoverable place for learners to find everything the package offers

Import map:
    constants  → default timing and threshold values
    types      → Protocols, dataclasses, type aliases
    clients    → producer factory (confluent_kafka preferred, kafka-python fallback)
    core       → FireAndForgetProducer class and module-level convenience functions
    demo       → run_demo() for end-to-end educational walkthrough
"""

# Constants: default timing, retry, and health threshold values.
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

# Types: structural contracts and immutable result models.
from .types import (
    ConfigLoader,
    DeliveryCallback,
    KafkaProducerLike,
    ProducerFactory,
    ProducerHealthMetrics,
    RetryResult,
    SendResult,
)

# Core: the shared producer class and one-liner convenience functions.
from .core import (
    FireAndForgetProducer,
    _sanitize_config,
    get_shared_producer,
    produce_event,
    produce_event_with_retry,
)

# Demo: standalone educational walkthrough.
from .demo import run_demo

__all__ = [
    # constants
    "DEFAULT_POLL_TIMEOUT_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_DELIVERY_TIMEOUT_SECONDS",
    "DEFAULT_FLUSH_TIMEOUT_SECONDS",
    "SHUTDOWN_FLUSH_TIMEOUT_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_DELAY_SECONDS",
    "HEALTH_ERROR_RATE_THRESHOLD",
    "HEALTH_MIN_MESSAGE_COUNT",
    "SENSITIVE_CONFIG_KEY_MARKERS",
    # types
    "KafkaProducerLike",
    "DeliveryCallback",
    "ProducerFactory",
    "ConfigLoader",
    "SendResult",
    "RetryResult",
    "ProducerHealthMetrics",
    # core
    "FireAndForgetProducer",
    "get_shared_producer",
    "produce_event",
    "produce_event_with_retry",
    "_sanitize_config",
    # demo
    "run_demo",
]
