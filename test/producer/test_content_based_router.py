"""
Unit tests for the content-based router reliability contract.

The scenarios in this suite validate routing behavior without requiring a live
Kafka broker. A lightweight fake dispatcher captures send calls in memory.
"""

import hashlib
import unittest
from typing import Any, Dict, Optional

from config.kafka_config import TopicPriority, get_topic_name
from producers.content_based_router.constants import (
    HEADER_KEY_PRIORITY,
    HEADER_KEY_ROUTING_STRATEGY,
)
from producers.content_based_router.core import (
    ContentBasedRouter,
    ContentBasedRoutingRule,
    HashPartitionRoutingRule,
    create_content_router,
)
from producers.content_based_router.types import MessageRoutingContext, RoutingStrategy


class FakeDispatcher:
    """In-memory dispatcher that stores produced records for assertions."""

    def __init__(self, send_error: Optional[Exception] = None) -> None:
        self.send_error = send_error
        self.records: list[Dict[str, Any]] = []

    def send(self, topic: str, data: Dict[str, Any], **kwargs: Any) -> None:
        """Capture the send call or raise an injected failure."""
        if self.send_error is not None:
            raise self.send_error
        self.records.append({"topic": topic, "data": data, "kwargs": kwargs})

    def flush(self, timeout: float) -> int:
        """Return immediately for unit tests because no real queue exists."""
        _ = timeout
        return 0


class ContentBasedRouterTests(unittest.TestCase):
    """Regression-focused checks for routing decisions and error handling."""

    def test_priority_rule_wins_when_context_priority_is_explicit(self) -> None:
        """Explicit priority should route to the configured priority lane topic."""
        dispatcher = FakeDispatcher()
        router = ContentBasedRouter("orders", dispatcher=dispatcher)
        router.add_routing_rule(
            ContentBasedRoutingRule(
                match_field="event_type",
                value_to_topic={"payment.failed": "orders-payments-failed"},
            )
        )

        decision = router.send_with_context(
            data={"event_type": "payment.failed"},
            context=MessageRoutingContext(
                priority=TopicPriority.HIGH,
                source_service="orders",
            ),
        )

        self.assertEqual(decision.resolved_by, "PriorityRoutingRule")
        self.assertEqual(decision.strategy, RoutingStrategy.PRIORITY_BASED)
        self.assertEqual(
            decision.target_topic,
            get_topic_name("orders", TopicPriority.HIGH),
        )

    def test_content_rule_routes_when_priority_is_omitted(self) -> None:
        """When priority is omitted, content rules should drive topic selection."""
        dispatcher = FakeDispatcher()
        router = ContentBasedRouter("orders", dispatcher=dispatcher)
        router.add_routing_rule(
            ContentBasedRoutingRule(
                match_field="event_type",
                value_to_topic={"payment.failed": "orders-payments-failed"},
            )
        )

        decision = router.send_with_context(
            data={"event_type": "payment.failed"},
            context=MessageRoutingContext(source_service="orders"),
        )

        self.assertEqual(decision.target_topic, "orders-payments-failed")
        self.assertEqual(decision.resolved_by, "ContentBasedRoutingRule")
        self.assertEqual(decision.strategy, RoutingStrategy.CONTENT_BASED)

    def test_create_content_router_factory_routes_with_context_without_priority(self) -> None:
        """Factory-configured content router should route by field mapping."""
        dispatcher = FakeDispatcher()
        router = create_content_router(
            service_name="billing",
            field_routing_rules={"event_type": {"refund.failed": "billing-refunds-failed"}},
        )
        router._dispatcher = dispatcher  # test-only dispatcher injection post-factory construction

        decision = router.send_with_context(
            data={"event_type": "refund.failed"},
            context=MessageRoutingContext(source_service="billing"),
        )

        self.assertEqual(decision.target_topic, "billing-refunds-failed")
        self.assertEqual(decision.strategy, RoutingStrategy.CONTENT_BASED)

    def test_hash_partition_rule_uses_stable_sha256_bucketing(self) -> None:
        """Hash routing should use deterministic bucket selection across runs."""
        dispatcher = FakeDispatcher()
        router = ContentBasedRouter("identity", dispatcher=dispatcher)
        router.add_routing_rule(
            HashPartitionRoutingRule(
                hash_field="user_id",
                topic_prefix="identity-user-shard",
                num_buckets=4,
            )
        )

        payload = {"user_id": "user-42"}
        decision = router.send_with_context(
            data=payload,
            context=MessageRoutingContext(source_service="identity"),
        )

        digest = hashlib.sha256("user-42".encode("utf-8")).digest()
        expected_bucket = int.from_bytes(digest[:8], byteorder="big", signed=False) % 4
        self.assertEqual(decision.target_topic, f"identity-user-shard-{expected_bucket}")
        self.assertEqual(decision.strategy, RoutingStrategy.HASH_PARTITION)

    def test_hash_partition_rule_rejects_invalid_bucket_count(self) -> None:
        """Invalid bucket counts should fail fast with a clear configuration error."""
        with self.assertRaises(ValueError):
            HashPartitionRoutingRule(
                hash_field="user_id",
                topic_prefix="identity-user-shard",
                num_buckets=0,
            )

    def test_send_with_context_increments_error_metric_on_dispatch_failure(self) -> None:
        """Dispatcher failures should increment routing error metrics and bubble up."""
        dispatcher = FakeDispatcher(send_error=RuntimeError("broker unavailable"))
        router = ContentBasedRouter("orders", dispatcher=dispatcher)

        with self.assertRaises(RuntimeError):
            router.send_with_context(
                data={"event_type": "payment.failed"},
                context=MessageRoutingContext(priority=TopicPriority.HIGH),
            )

        metrics = router.get_routing_metrics()
        self.assertEqual(metrics["routing_errors"], 1)
        self.assertEqual(metrics["messages_routed"], 0)

    def test_headers_use_default_priority_when_context_priority_is_absent(self) -> None:
        """Messages without explicit priority should still carry a safe default header value."""
        dispatcher = FakeDispatcher()
        router = ContentBasedRouter(
            "orders",
            default_priority=TopicPriority.LOW,
            dispatcher=dispatcher,
        )
        router.add_routing_rule(
            ContentBasedRoutingRule(
                match_field="event_type",
                value_to_topic={"payment.failed": "orders-payments-failed"},
            )
        )

        router.send_with_context(
            data={"event_type": "payment.failed"},
            context=MessageRoutingContext(source_service="orders"),
        )

        sent_headers = dispatcher.records[0]["kwargs"]["headers"]
        self.assertEqual(sent_headers[HEADER_KEY_PRIORITY], TopicPriority.LOW.value)
        self.assertEqual(
            sent_headers[HEADER_KEY_ROUTING_STRATEGY], RoutingStrategy.CONTENT_BASED.value
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
