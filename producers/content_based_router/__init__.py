"""
producers.content_based_router
==============================
Enterprise Integration Pattern: Content-Based Router (Hohpe & Woolf, ch.7).

Routes outbound Kafka messages to different topic lanes based on message
metadata (priority), payload field values (content-based), or entity hash
(partition fan-out).  Application code never hardcodes a topic name.

Public API
----------
Classes:
    ContentBasedRouter       — main router class; holds the rule chain.
    MessageRoutingContext     — routing envelope attached to each message.
    RoutingDecision          — immutable outcome of one routing evaluation.
    RoutingStrategy          — enum of supported routing variants.
    RoutingRule              — abstract base for custom rules.
    PriorityRoutingRule      — routes by TopicPriority lane (most common).
    ContentBasedRoutingRule  — routes by payload field value.
    HashPartitionRoutingRule — routes by hash(field) % N buckets.

Factories:
    create_priority_router   — pre-configured for priority-lane routing.
    create_content_router    — pre-configured with content-based rules.

Quickstart
----------
    from producers.content_based_router import (
        ContentBasedRouter,
        MessageRoutingContext,
        TopicPriority,
    )

    router = ContentBasedRouter("order-service")
    router.send_with_priority({"order_id": 1}, priority=TopicPriority.HIGH)
"""

from .core import (
    ContentBasedRouter,
    ContentBasedRoutingRule,
    HashPartitionRoutingRule,
    PriorityRoutingRule,
    RoutingRule,
    create_content_router,
    create_priority_router,
)
from .types import (
    MessageRoutingContext,
    RoutingDecision,
    RoutingStrategy,
)

__all__ = [
    # Router
    "ContentBasedRouter",
    # Rules
    "RoutingRule",
    "PriorityRoutingRule",
    "ContentBasedRoutingRule",
    "HashPartitionRoutingRule",
    # Types
    "MessageRoutingContext",
    "RoutingDecision",
    "RoutingStrategy",
    # Factories
    "create_priority_router",
    "create_content_router",
]
