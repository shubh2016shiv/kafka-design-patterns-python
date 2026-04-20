"""
Default values for the fire-and-forget producer.

All values are intentionally conservative for a long-lived, shared producer:
- fail fast on health degradation
- generous flush timeout so in-flight messages survive shutdown
- short poll interval so callbacks are served frequently without busy-waiting

Tune these per-environment, not per-message.
"""

# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

# poll(0) is called after every produce() to serve any delivery callbacks that
# are already ready without blocking the caller's thread.
# This constant documents the intent — do not increase it in the hot path.
DEFAULT_POLL_TIMEOUT_SECONDS: float = 0.0

# When waiting for explicit delivery confirmation, poll() is called in short
# intervals until the delivery callback fires or the deadline is reached.
# 100 ms keeps confirmation latency low while avoiding busy-spin CPU waste.
DEFAULT_POLL_INTERVAL_SECONDS: float = 0.1

# ---------------------------------------------------------------------------
# Delivery confirmation
# ---------------------------------------------------------------------------

# Maximum time to wait for a broker ack when require_delivery_confirmation=True.
# 10 s covers normal broker round-trip plus temporary leader elections.
# Increase to 30 s+ for cross-datacenter or high-latency environments.
DEFAULT_DELIVERY_TIMEOUT_SECONDS: float = 10.0

# ---------------------------------------------------------------------------
# Flush / shutdown
# ---------------------------------------------------------------------------

# flush() timeout used during graceful shutdown (atexit handler).
# 60 s is generous enough to drain a full internal queue on a congested broker.
# Messages still in queue after this window are logged as lost.
SHUTDOWN_FLUSH_TIMEOUT_SECONDS: float = 60.0

# flush() timeout for explicit caller-triggered flushes (producer.flush() API).
# 30 s is a reasonable interactive-flush budget; adjust per SLA requirement.
DEFAULT_FLUSH_TIMEOUT_SECONDS: float = 30.0

# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

# Maximum number of retry attempts in send_with_retry().
# Note: this is application-level retry on LOCAL enqueue failure, not broker retry.
# Broker-level retries are controlled by the 'retries' and 'retry.backoff.ms'
# producer config keys in config/kafka_config.py.
DEFAULT_MAX_RETRIES: int = 3

# Initial delay between retry attempts (seconds).
# Doubled on each subsequent attempt (exponential backoff) to avoid hammering a
# producer queue that is full due to back-pressure from a slow broker.
DEFAULT_RETRY_DELAY_SECONDS: float = 1.0

# ---------------------------------------------------------------------------
# Health monitoring
# ---------------------------------------------------------------------------

# Error rate threshold above which the producer marks itself unhealthy.
# 10 % means: more than 1 in 10 delivery callbacks returned an error.
# Tune down (e.g. 0.01) for payment/critical pipelines, up for logging pipelines.
HEALTH_ERROR_RATE_THRESHOLD: float = 0.1

# Minimum number of messages before the error rate is meaningful.
# Prevents a single early failure (1/1 = 100 %) from permanently marking the
# producer unhealthy before it has had a chance to establish a baseline.
HEALTH_MIN_MESSAGE_COUNT: int = 10

# ---------------------------------------------------------------------------
# Credential scrubbing
# ---------------------------------------------------------------------------

# Config key substrings treated as sensitive for sanitized log output.
# Covers Kafka SASL, OAuth, TLS, and generic secret naming patterns.
SENSITIVE_CONFIG_KEY_MARKERS: tuple = (
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
