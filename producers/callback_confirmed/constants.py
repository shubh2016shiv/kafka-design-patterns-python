"""
Constants for the callback-confirmed producer flow.

These values are intentionally conservative defaults for a one-shot script:
- quick feedback on readiness failures
- enough time for flush in local/dev environments
- no infinite waiting
"""

# `poll()` only gives callbacks time to fire in one-shot scripts.
DEFAULT_POLL_TIMEOUT_SECONDS = 1.0

# `flush()` should wait long enough to complete normal sends before process exit.
DEFAULT_FLUSH_TIMEOUT_SECONDS = 10.0

# Metadata check should fail fast if broker/listener configuration is wrong.
DEFAULT_METADATA_TIMEOUT_SECONDS = 5.0
