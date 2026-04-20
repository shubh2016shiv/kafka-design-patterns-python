"""
Module-level constants for the Dead Letter Queue producer pattern.

Why constants live in their own file:
- prevents circular imports between types.py and core.py
- makes every tuneable knob discoverable without reading implementation code
- allows test overrides at import time without monkey-patching classes

Naming convention for DLQ topics follows the Confluent Platform standard
(also used by Kafka Connect and ksqlDB error topics):
  <originating-topic>-dead-letter-queue
e.g. "payments" → "payments-dead-letter-queue"
"""

# Suffix appended to the originating service/topic name to form the DLQ topic.
# Confluent docs reference: https://docs.confluent.io/platform/current/connect/concepts.html#dead-letter-queues
DLQ_TOPIC_SUFFIX: str = "-dead-letter-queue"

# ── HIGH_AVAILABILITY preset ───────────────────────────────────────────────────
# These values are used by create_ha_dlq_producer() in core.py.
# They trade slower initial response for faster circuit recovery and stricter
# health signals — appropriate for SLA-critical pipelines.

# Trip the circuit after only 2 failures (vs default 5) to detect outages fast.
HA_FAILURE_THRESHOLD: int = 2

# Allow recovery probe after 30 s (vs default 60 s) to reduce downtime window.
HA_RECOVERY_TIMEOUT_SECONDS: int = 30

# Tolerate up to 5 retries (vs default 3) — more patient with transient blips.
HA_MAX_RETRIES: int = 5

# Allow 200 concurrent sends (vs default 100) to handle higher-throughput workloads.
HA_MAX_CONCURRENT_SENDS: int = 200

# Flag unhealthy at 5 % error rate (vs default 10 %) — stricter health signal.
HA_ERROR_RATE_UNHEALTHY_THRESHOLD: float = 0.05
