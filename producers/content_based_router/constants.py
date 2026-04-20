"""
Content-Based Router — tunable defaults.

Why this module exists:
  Keep every default value in one auditable file so operators can find and
  tune them without hunting through logic files.  Each constant below includes
  a production-tuning note so the rationale survives beyond the initial commit.
"""

# ---------------------------------------------------------------------------
# Rule evaluation weights
# ---------------------------------------------------------------------------

PRIORITY_RULE_WEIGHT: int = 100
"""
Evaluation weight for PriorityRoutingRule.

Higher weight = evaluated first in the sorted rule chain.

Priority-based routing is the most common production pattern — routing HIGH /
MEDIUM / LOW / BACKGROUND traffic to separate topic lanes is almost always the
first dimension you add.  Keeping this weight the highest ensures it acts as
the primary lane selector before more specific content or hash rules fire.

Tune: if content-based rules should override priority lanes, raise their
weight above 100.
"""

CONTENT_RULE_DEFAULT_WEIGHT: int = 50
"""
Default evaluation weight for ContentBasedRoutingRule.

Content rules are more specific than priority rules but require a matching
field to be present in the payload, so they sit below the priority lane by
default.  Pass a custom `weight=` to ContentBasedRoutingRule.__init__ to
promote or demote individual rules relative to each other.
"""

HASH_PARTITION_RULE_DEFAULT_WEIGHT: int = 30
"""
Default evaluation weight for HashPartitionRoutingRule.

Hash rules act as a load-distribution tie-breaker; they should run after
business-logic rules have had a chance to match first.

Tune: if you want strict per-entity partitioning regardless of content rules,
raise this weight above CONTENT_RULE_DEFAULT_WEIGHT.
"""

# ---------------------------------------------------------------------------
# Flush / timeout defaults
# ---------------------------------------------------------------------------

DEFAULT_FLUSH_TIMEOUT_SECONDS: float = 30.0
"""
Maximum seconds to block on producer.flush().

Matches the Kafka client default request.timeout.ms (30 000 ms).
Reducing to 5 s is appropriate for integration tests that use a local broker.
In production, 30 s provides enough headroom for transient broker slowness
without blocking the application thread indefinitely.
"""

# ---------------------------------------------------------------------------
# Header keys stamped onto every routed message
# ---------------------------------------------------------------------------

HEADER_KEY_PRIORITY: str = "x-routing-priority"
"""Kafka message header key carrying the TopicPriority value string."""

HEADER_KEY_CATEGORY: str = "x-routing-category"
"""Kafka message header key carrying the optional message category label."""

HEADER_KEY_SOURCE_SERVICE: str = "x-routing-source-service"
"""Kafka message header key identifying the originating service."""

HEADER_KEY_ROUTING_STRATEGY: str = "x-routing-strategy"
"""Kafka message header key recording which RoutingStrategy resolved the topic."""
