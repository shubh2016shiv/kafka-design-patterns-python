"""
Content-Based Router — educational demo entrypoint.

Run this module directly to walk through all three routing strategies with
annotated console output.  No live Kafka broker is required — the demo catches
connection errors and shows which routing decision would have been made.

    python -m producers.content_based_router.demo

What you will observe:
  Stage A — Priority-based routing  (HIGH / MEDIUM / LOW / BACKGROUND lanes)
  Stage B — Content-based routing   (field value → specific topic)
  Stage C — Hash-partition routing  (user_id hash → one of N topic buckets)
  Stage D — Direct send             (bypass all rules, name the topic explicitly)
  Stage E — Routing metrics report
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from .constants import DEFAULT_FLUSH_TIMEOUT_SECONDS
from .core import (
    ContentBasedRouter,
    ContentBasedRoutingRule,
    HashPartitionRoutingRule,
    create_priority_router,
)
from .types import MessageRoutingContext, RoutingDecision

try:
    from config.kafka_config import TopicPriority
except ImportError:  # pragma: no cover
    from kafka.config.kafka_config import TopicPriority  # type: ignore[no-redef]

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("content_based_router.demo")

SERVICE_NAME = "demo-service"


def _print_decision(label: str, decision: RoutingDecision) -> None:
    """Pretty-print a RoutingDecision for educational console output."""
    print(
        f"  ✓ {label}\n"
        f"    topic      = {decision.target_topic}\n"
        f"    resolved by = {decision.resolved_by}\n"
        f"    strategy   = {decision.strategy.value}\n"
        f"    fallback?  = {decision.used_fallback}\n"
    )


def _try_send(
    router: ContentBasedRouter,
    label: str,
    data: Dict[str, Any],
    **kwargs: Any,
) -> None:
    """
    Attempt a send and report the routing decision whether or not Kafka is live.

    The routing decision is always deterministic regardless of broker availability —
    the rule chain runs before any network I/O.  If the broker is unreachable the
    demo prints the error but continues, so you can study routing logic offline.
    """
    try:
        decision = router.send_with_context(data, kwargs.pop("context"), **kwargs)
        _print_decision(label, decision)
    except Exception as exc:
        print(f"  ✗ {label} — broker unreachable ({exc}); routing decision still logged above.\n")


def run_demo() -> None:
    """Execute all routing strategy demonstrations in sequence."""
    timestamp = int(time.time())

    print("\n" + "=" * 64)
    print("  Content-Based Router — Pattern Demonstration")
    print("=" * 64 + "\n")

    # ----------------------------------------------------------------
    # Stage A: Priority-based routing
    # ----------------------------------------------------------------
    print("Stage A — Priority-Based Routing")
    print("  Rule: PriorityRoutingRule (weight=100) maps TopicPriority")
    print("        to separate topic lanes via get_topic_name().\n")

    router = create_priority_router(SERVICE_NAME)

    for priority, label in [
        (TopicPriority.HIGH, "HIGH   → high-priority lane"),
        (TopicPriority.MEDIUM, "MEDIUM → medium-priority lane"),
        (TopicPriority.LOW, "LOW    → low-priority lane"),
        (TopicPriority.BACKGROUND, "BACKGROUND → background-tasks lane"),
    ]:
        payload = {"timestamp": timestamp, "demo_stage": "A", "priority_label": label}
        context = MessageRoutingContext(priority=priority, source_service=SERVICE_NAME)
        try:
            decision = router.send_with_context(payload, context)
            _print_decision(label, decision)
        except Exception as exc:
            # Routing decision already resolved; broker error is expected offline.
            print(f"  ✗ {label} — broker unreachable ({exc})\n")

    # ----------------------------------------------------------------
    # Stage B: Content-based routing
    # ----------------------------------------------------------------
    print("Stage B — Content-Based Routing")
    print("  Rule: ContentBasedRoutingRule inspects payload['event_type']")
    print("        and maps its value directly to a topic.")
    print("        Context priority is intentionally omitted so content rules can lead.\n")

    router.add_routing_rule(
        ContentBasedRoutingRule(
            match_field="event_type",
            value_to_topic={
                "payment.completed": f"{SERVICE_NAME}-payments-completed",
                "payment.failed": f"{SERVICE_NAME}-payments-failed",
                "auth.login": f"{SERVICE_NAME}-auth-events",
            },
            rule_weight=50,
        )
    )

    for event_type in ["payment.completed", "payment.failed", "auth.login"]:
        payload = {"timestamp": timestamp, "demo_stage": "B", "event_type": event_type}
        context = MessageRoutingContext(
            category=event_type,
            source_service=SERVICE_NAME,
        )
        try:
            decision = router.send_with_context(payload, context)
            _print_decision(f"event_type={event_type!r}", decision)
        except Exception as exc:
            print(f"  ✗ event_type={event_type!r} — broker unreachable ({exc})\n")

    # ----------------------------------------------------------------
    # Stage C: Hash-partition routing
    # ----------------------------------------------------------------
    print("Stage C — Hash-Partition Routing")
    print("  Rule: HashPartitionRoutingRule hashes payload['user_id']")
    print("        modulo 3 to distribute across 3 topic buckets.")
    print("        Context priority is intentionally omitted so hash routing can lead.\n")

    NUM_BUCKETS = 3
    router.add_routing_rule(
        HashPartitionRoutingRule(
            hash_field="user_id",
            topic_prefix=f"{SERVICE_NAME}-user-shard",
            num_buckets=NUM_BUCKETS,
            rule_weight=30,
        )
    )

    for user_id in ["user-001", "user-002", "user-003", "user-999"]:
        payload = {"timestamp": timestamp, "demo_stage": "C", "user_id": user_id}
        context = MessageRoutingContext(
            user_id=user_id,
            source_service=SERVICE_NAME,
        )
        try:
            decision = router.send_with_context(payload, context)
            _print_decision(f"user_id={user_id!r}", decision)
        except Exception as exc:
            print(f"  ✗ user_id={user_id!r} — broker unreachable ({exc})\n")

    # ----------------------------------------------------------------
    # Stage D: Direct send (bypass all rules)
    # ----------------------------------------------------------------
    print("Stage D — Direct Send (rule chain bypassed)")
    print("  Use when the correct topic is already known at the call site.\n")

    direct_topic = f"{SERVICE_NAME}-audit-log"
    direct_payload = {"timestamp": timestamp, "demo_stage": "D", "event": "admin_action"}
    try:
        router.send_direct(direct_topic, direct_payload)
        print(f"  ✓ Sent directly to topic='{direct_topic}'\n")
    except Exception as exc:
        print(f"  ✗ Direct send — broker unreachable ({exc})\n")

    # ----------------------------------------------------------------
    # Stage E: Routing metrics
    # ----------------------------------------------------------------
    print("Stage E — Routing Metrics Report")
    print("  These counters power operational dashboards and health checks.\n")

    metrics = router.get_routing_metrics()
    for key, value in metrics.items():
        print(f"  {key:30s} = {value}")

    print("\n" + "=" * 64)
    print("  Demo complete.  Study core.py to see the rule chain in detail.")
    print("=" * 64 + "\n")

    router.flush(timeout=DEFAULT_FLUSH_TIMEOUT_SECONDS)


if __name__ == "__main__":
    run_demo()
