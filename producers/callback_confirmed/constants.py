"""
Constants for the callback-confirmed producer flow.

These names serve both the reusable publisher and older one-shot helper.

Plain-language terms:
- poll: run ready callbacks and other client events
- flush: wait for queued work to finish or for a timeout to expire
- metadata: Kafka's description of brokers, topics, and partitions
- back-pressure: the local producer queue saying "I am full; slow down"
"""

# Zero serves callbacks that are ready now without delaying a normal publish loop.
DEFAULT_POLL_TIMEOUT_SECONDS: float = 0.0

# The producer delivery deadline is 30 seconds in config.py. A 35-second flush
# normally gives those messages time to reach either success or final failure.
DEFAULT_FLUSH_TIMEOUT_SECONDS: float = 35.0

# Metadata check should fail fast if broker/listener configuration is wrong.
DEFAULT_METADATA_TIMEOUT_SECONDS: float = 5.0

# A full local queue is retried for at most five seconds. Raise this for batch
# jobs or lower it for request paths that must shed load quickly.
DEFAULT_QUEUE_CAPACITY_WAIT_SECONDS: float = 5.0

# Poll in short slices while waiting for queue capacity so the caller stays
# responsive and the overall five-second deadline remains authoritative.
QUEUE_CAPACITY_POLL_INTERVAL_SECONDS: float = 0.25
