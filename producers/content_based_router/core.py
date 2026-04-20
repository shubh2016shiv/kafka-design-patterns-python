"""
Content-Based Router — routing rules and router class.

Pattern origin (Enterprise Integration Patterns, Hohpe & Woolf):
  A Content-Based Router examines each incoming message and routes it to the
  correct channel based on message content or metadata.  In Kafka terms,
  "channels" are topics — separate topic lanes per priority tier, per event
  type, or per entity hash bucket.

Why this pattern matters in production:
  Without routing, one monolithic topic becomes the bottleneck for every
  workload.  A Content-Based Router lets you:
  - Scale HIGH-priority consumers independently of BACKGROUND consumers.
  - Give payment events a dedicated topic with stricter retention and replication.
  - Shard by user_id across N topic partitions without changing the consumer API.

ASCII flow — message lifecycle through ContentBasedRouter:

    ┌─────────────────────────────────────────────────────────────────┐
    │                     ContentBasedRouter                          │
    │                                                                 │
    │  Caller ──► send_with_priority(data, priority)                  │
    │                  │                                              │
    │    Stage 1.0      │  Build MessageRoutingContext from priority   │
    │                  ▼                                              │
    │  Caller ──► send_with_context(data, context)                    │
    │                  │                                              │
    │    Stage 2.0      │  _resolve_target_topic(context, data)        │
    │                  │                                              │
    │           ┌──────▼──────────────────────────────┐              │
    │           │  Rule chain (sorted by weight desc)  │              │
    │           │  ① PriorityRoutingRule     (w=100)   │              │
    │           │  ② ContentBasedRoutingRule (w= 50)   │              │
    │           │  ③ HashPartitionRoutingRule(w= 30)   │              │
    │           └──────┬──────────────────────────────┘              │
    │                  │  first rule where matches() is True wins     │
    │                  │  if none match → fallback_topic              │
    │                  ▼                                              │
    │    Stage 3.0      │  stamp routing headers onto message          │
    │                  ▼                                              │
    │    Stage 4.0      │  dispatcher.send(topic, data, headers=…)    │
    │                  ▼                                              │
    │    Stage 5.0      │  update routing_metrics counter             │
    │                  ▼                                              │
    │            RoutingDecision (returned / logged)                  │
    └─────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

# ABC/abstractmethod: enforce the RoutingRule contract at class-definition time.
from abc import ABC, abstractmethod

# logging: structured operational breadcrumbs without coupling to a specific sink.
import hashlib
import logging

# typing: explicit contracts for every public interface.
from typing import Any, Dict, List, Optional

from .clients import SingletonProducerGateway, build_singleton_dispatcher
from .constants import (
    CONTENT_RULE_DEFAULT_WEIGHT,
    DEFAULT_FLUSH_TIMEOUT_SECONDS,
    HASH_PARTITION_RULE_DEFAULT_WEIGHT,
    HEADER_KEY_CATEGORY,
    HEADER_KEY_PRIORITY,
    HEADER_KEY_ROUTING_STRATEGY,
    HEADER_KEY_SOURCE_SERVICE,
    PRIORITY_RULE_WEIGHT,
)
from .types import (
    KafkaMessageDispatcher,
    MessageRoutingContext,
    RoutingDecision,
    RoutingStrategy,
)

# TopicPriority and get_topic_name are the bridge between this routing layer
# and the Kafka topic naming conventions defined in config/kafka_config.py.
try:
    from config.kafka_config import TopicPriority, get_topic_name
except ImportError:  # pragma: no cover — fallback for installed-package layout.
    from kafka.config.kafka_config import TopicPriority, get_topic_name  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base — the RoutingRule contract
# ---------------------------------------------------------------------------


class RoutingRule(ABC):
    """
    Abstract base class for all routing rules.

    Design pattern: Strategy (Hohpe & Woolf).
    Each concrete rule encapsulates one routing algorithm.  The router calls
    rules in weight order (highest first) and stops at the first match.

    Interview note — Chain of Responsibility vs Strategy:
      This implementation blends both patterns.  The rule chain is a Chain of
      Responsibility (each rule decides whether to handle the message).  Each
      rule's internal algorithm is a Strategy (pluggable, swappable).

    Implementing a custom rule:
        class GeoRoutingRule(RoutingRule):
            def matches(self, ctx, data):
                return ctx.tenant_id in EMEA_TENANTS
            def get_topic(self, ctx, data):
                return "events-emea"
            def weight(self):
                return 75  # between CONTENT and PRIORITY
    """

    @abstractmethod
    def matches(self, context: MessageRoutingContext, data: Dict[str, Any]) -> bool:
        """
        Return True if this rule applies to the given message.

        Args:
            context: Routing envelope with priority, category, and other metadata.
            data:    Raw message payload dict (read-only; do not mutate).

        Returns:
            True  — this rule will handle routing; get_topic() will be called.
            False — skip this rule; evaluate the next one in weight order.
        """

    @abstractmethod
    def get_topic(self, context: MessageRoutingContext, data: Dict[str, Any]) -> str:
        """
        Return the target Kafka topic name for the given message.

        Called only when matches() returned True.

        Args:
            context: Same routing envelope passed to matches().
            data:    Same payload dict passed to matches().

        Returns:
            Fully qualified Kafka topic name string.
        """

    @abstractmethod
    def weight(self) -> int:
        """
        Evaluation priority for this rule.

        Higher weight = evaluated earlier in the chain.
        Rules with equal weight are evaluated in insertion order.

        Returns:
            Integer weight; see constants.py for standard anchors.
        """

    def routing_strategy(self) -> RoutingStrategy:
        """
        Declare which RoutingStrategy this rule represents.

        Used to populate the RoutingDecision.strategy field for observability.
        Override in concrete rules to return the appropriate variant.
        """
        return RoutingStrategy.CUSTOM


# ---------------------------------------------------------------------------
# Concrete rule: Priority-Based
# ---------------------------------------------------------------------------


class PriorityRoutingRule(RoutingRule):
    """
    Routes messages to different topic lanes based on TopicPriority.

    This is the most common production routing pattern.  Separate topic lanes
    (e.g. {service}-high-priority, {service}-medium-priority) let you:
    - Assign faster consumers with more replicas to HIGH-priority lanes.
    - Set longer retention on BACKGROUND lanes without affecting latency-sensitive topics.
    - Alert on HIGH-priority consumer lag without noise from BACKGROUND jobs.

    Topic name contract:
    Delegates to get_topic_name(service_name, priority) from kafka_config.py,
    which produces names like "order-service-high-priority".

    Args:
        service_name: Logical service name used as topic prefix.

    Example produced topics:
        order-service-high-priority
        order-service-medium-priority
        order-service-low-priority
        order-service-background-tasks
    """

    def __init__(self, service_name: str) -> None:
        """
        Args:
            service_name: Logical name of the producing service.
                          Used as the prefix in Kafka topic names.
        """
        if not service_name.strip():
            raise ValueError("service_name must be a non-empty string.")
        self.service_name = service_name

    def matches(self, context: MessageRoutingContext, data: Dict[str, Any]) -> bool:
        """Match any message that carries an explicit TopicPriority."""
        return context.priority is not None

    def get_topic(self, context: MessageRoutingContext, data: Dict[str, Any]) -> str:
        """Resolve the priority-lane topic from config's naming convention."""
        return get_topic_name(self.service_name, context.priority)

    def weight(self) -> int:
        return PRIORITY_RULE_WEIGHT

    def routing_strategy(self) -> RoutingStrategy:
        return RoutingStrategy.PRIORITY_BASED


# ---------------------------------------------------------------------------
# Concrete rule: Content-Based
# ---------------------------------------------------------------------------


class ContentBasedRoutingRule(RoutingRule):
    """
    Routes messages by matching a specific payload field value to a topic.

    Use this when one producer service emits heterogeneous event types and you
    want each type to land in a dedicated topic.

    Example:
        ContentBasedRoutingRule(
            match_field="event_type",
            value_to_topic={
                "payment.completed": "payments-completed",
                "payment.failed":    "payments-failed",
                "auth.login":        "auth-events",
            }
        )

    Production tip:
    Keep value_to_topic small and explicit.  A rule with hundreds of entries
    is a signal that you need a different partitioning strategy.

    Args:
        match_field:    Name of the payload dict key to inspect.
        value_to_topic: Mapping of field value strings to Kafka topic names.
        rule_weight:    Evaluation order relative to other rules (default 50).
    """

    def __init__(
        self,
        match_field: str,
        value_to_topic: Dict[str, str],
        rule_weight: int = CONTENT_RULE_DEFAULT_WEIGHT,
    ) -> None:
        if not match_field.strip():
            raise ValueError("match_field must be a non-empty string.")
        if not value_to_topic:
            raise ValueError("value_to_topic must include at least one field->topic mapping.")

        self.match_field = match_field
        self.value_to_topic = value_to_topic
        self._weight = rule_weight

    def matches(self, context: MessageRoutingContext, data: Dict[str, Any]) -> bool:
        """
        Match when the payload contains match_field and its value is in value_to_topic.

        Returns False (no match) if the field is absent or its value is not mapped,
        allowing lower-weight rules a chance to handle the message.
        """
        return self.match_field in data and data[self.match_field] in self.value_to_topic

    def get_topic(self, context: MessageRoutingContext, data: Dict[str, Any]) -> str:
        """Return the topic mapped to the payload field's value."""
        return self.value_to_topic[data[self.match_field]]

    def weight(self) -> int:
        return self._weight

    def routing_strategy(self) -> RoutingStrategy:
        return RoutingStrategy.CONTENT_BASED


# ---------------------------------------------------------------------------
# Concrete rule: Hash-Partition
# ---------------------------------------------------------------------------


class HashPartitionRoutingRule(RoutingRule):
    """
    Deterministic fan-out across N topics using a hash of a payload field.

    When to use:
    - You need per-entity ordering across consumers (all events for user_id=42
      must land on the same topic so one consumer processes them in sequence).
    - A single high-volume topic is too wide; splitting by hash distributes load.

    Topic naming:
    Produces topic names like "{topic_prefix}-0", "{topic_prefix}-1", …
    up to "{topic_prefix}-{num_buckets - 1}".

    Ordering guarantee:
    A stable SHA-256 digest is used as the hash source, so the same entity
    value always maps to the same topic bucket across different Python
    processes and deployments.

    Args:
        hash_field:  Payload dict key whose value is hashed.
        topic_prefix: Topic name prefix; bucket index is appended as suffix.
        num_buckets:  Number of target topics (N).  Must match the actual
                      number of topics pre-created in Kafka.
        rule_weight:  Evaluation order (default 30).
    """

    def __init__(
        self,
        hash_field: str,
        topic_prefix: str,
        num_buckets: int,
        rule_weight: int = HASH_PARTITION_RULE_DEFAULT_WEIGHT,
    ) -> None:
        if not hash_field.strip():
            raise ValueError("hash_field must be a non-empty string.")
        if not topic_prefix.strip():
            raise ValueError("topic_prefix must be a non-empty string.")
        if num_buckets <= 0:
            raise ValueError("num_buckets must be greater than zero.")

        self.hash_field = hash_field
        self.topic_prefix = topic_prefix
        self.num_buckets = num_buckets
        self._weight = rule_weight

    def matches(self, context: MessageRoutingContext, data: Dict[str, Any]) -> bool:
        """Match when the hash field is present in the payload."""
        return self.hash_field in data

    def get_topic(self, context: MessageRoutingContext, data: Dict[str, Any]) -> str:
        """Compute deterministic bucket index and return the corresponding topic name."""
        bucket_index = self._compute_bucket_index(data[self.hash_field])
        return f"{self.topic_prefix}-{bucket_index}"

    def _compute_bucket_index(self, field_value: Any) -> int:
        """
        Derive a stable bucket index for the given field value.

        Stage 1.0:
        Canonicalise the value to UTF-8 bytes via `str(value).encode(...)`.
        Stage 1.1:
        Compute SHA-256 and convert the first 8 bytes to an unsigned integer.
        Stage 1.2:
        Apply modulo arithmetic to map into the configured bucket range.
        """
        canonical_bytes = str(field_value).encode("utf-8")
        digest = hashlib.sha256(canonical_bytes).digest()
        numeric_hash = int.from_bytes(digest[:8], byteorder="big", signed=False)
        return numeric_hash % self.num_buckets

    def weight(self) -> int:
        return self._weight

    def routing_strategy(self) -> RoutingStrategy:
        return RoutingStrategy.HASH_PARTITION


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------


class ContentBasedRouter:
    """
    Kafka producer that selects target topics dynamically based on message
    context and a configurable chain of routing rules.

    Pattern: Content-Based Router (Enterprise Integration Patterns, ch.7).

    Architectural role:
    Sits between your application code and raw Kafka topics.  Application code
    never hardcodes a topic name — it describes the message with a
    MessageRoutingContext and the router picks the right topic.

    This separation gives you:
    - Topic topology changes (rename, split, merge) without touching application code.
    - Observable routing decisions via RoutingDecision objects and structured headers.
    - Independent scaling of each topic lane's consumer group.

    Rule evaluation:
    Rules are sorted by weight (highest first).  The first rule whose
    matches() returns True wins.  If no rule matches, the fallback_topic is used.
    Add rules via add_routing_rule(); remove by type via remove_routing_rule().

    Usage:

        # Priority-based (most common):
        router = ContentBasedRouter("order-service")
        router.send_with_priority({"order_id": 1}, priority=TopicPriority.HIGH)

        # Full context control:
        ctx = MessageRoutingContext(
            priority=TopicPriority.MEDIUM,
            category="payment",
        )
        router.send_with_context({"amount": 99.99}, ctx)

        # Direct bypass (routing skipped):
        router.send_direct("audit-log", {"event": "admin_login"})
    """

    def __init__(
        self,
        service_name: str,
        default_priority: TopicPriority = TopicPriority.MEDIUM,
        dispatcher: Optional[KafkaMessageDispatcher] = None,
        producer_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Initialise the router with a service name and optional overrides.

        Args:
            service_name:     Logical name of the producing service.  Used as the
                              prefix for priority-lane topic names and as the
                              source_service header value.
            default_priority: Topic lane used when no rule matches (fallback).
                              Default: MEDIUM — a safe middle ground.
            dispatcher:       Inject a custom dispatcher (for tests or alternate
                              backends).  If None, builds a SingletonProducerGateway
                              backed by confluent_kafka.
            producer_config:  Optional Kafka producer config dict forwarded to the
                              dispatcher factory when dispatcher=None.
        """
        self.service_name = service_name
        self.default_priority = default_priority

        # Stage 1.0: Initialise dispatcher — use injection if provided, otherwise
        # build the production-default singleton-backed dispatcher.
        self._dispatcher: KafkaMessageDispatcher = (
            dispatcher if dispatcher is not None else build_singleton_dispatcher(producer_config)
        )

        # Stage 2.0: Set up rule chain with the always-present priority rule.
        # PriorityRoutingRule has weight 100 — it runs first for all messages
        # that carry a TopicPriority (i.e. every message sent through this router).
        self._rules: List[RoutingRule] = []
        self.add_routing_rule(PriorityRoutingRule(service_name))

        # Stage 3.0: Compute fallback topic — the safety net for messages that
        # somehow escape the rule chain (shouldn't happen with PriorityRoutingRule
        # installed, but defensive programming is good practice).
        self.fallback_topic: str = get_topic_name(service_name, default_priority)

        # Stage 4.0: Initialise metrics dict — lightweight counters for operational
        # dashboards.  Replace with Prometheus / StatsD in production.
        self.routing_metrics: Dict[str, Any] = {
            "messages_routed": 0,
            "routing_errors": 0,
            "fallback_used": 0,
            "topic_distribution": {},
        }

        logger.info(
            "ContentBasedRouter initialised | service=%s fallback_topic=%s",
            service_name,
            self.fallback_topic,
        )

    # ------------------------------------------------------------------
    # Rule management
    # ------------------------------------------------------------------

    def add_routing_rule(self, rule: RoutingRule) -> None:
        """
        Register a new routing rule and re-sort the chain by weight (desc).

        Rules are evaluated in weight order — highest first.  The first rule
        whose matches() returns True wins; lower-weight rules are not evaluated.

        Args:
            rule: Any object that extends RoutingRule.

        Production tip:
        Add all rules at startup.  Adding rules during high-throughput periods
        triggers a list sort on every call — negligible cost but worth noting.
        """
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.weight(), reverse=True)
        logger.debug("Routing rule added: %s (weight=%d)", rule.__class__.__name__, rule.weight())

    def remove_routing_rule(self, rule_class: type) -> bool:
        """
        Remove all rules of a given class from the rule chain.

        Args:
            rule_class: Class object (e.g. ContentBasedRoutingRule) to remove.

        Returns:
            True if at least one rule was removed; False if none matched.
        """
        before = len(self._rules)
        self._rules = [r for r in self._rules if not isinstance(r, rule_class)]
        removed_count = before - len(self._rules)
        if removed_count:
            logger.debug("Removed %d rule(s) of type %s", removed_count, rule_class.__name__)
        return removed_count > 0

    # ------------------------------------------------------------------
    # Internal: topic resolution
    # ------------------------------------------------------------------

    def _resolve_target_topic(
        self, context: MessageRoutingContext, data: Dict[str, Any]
    ) -> RoutingDecision:
        """
        Walk the rule chain and return a RoutingDecision for this message.

        Stage 2.0 in the main flow (see module-level ASCII diagram).

        Design: Chain of Responsibility — rules are evaluated in weight order;
        the first match wins.  If a rule raises an exception it is caught,
        logged, and the chain continues to the next rule.  This prevents a
        buggy custom rule from dropping messages.

        Args:
            context: MessageRoutingContext built by the caller.
            data:    Raw payload dict.

        Returns:
            RoutingDecision with target_topic, resolved_by, strategy, and
            used_fallback fields populated.
        """
        # Stage 2.1: Evaluate rules in weight order.
        for rule in self._rules:
            try:
                if rule.matches(context, data):
                    topic = rule.get_topic(context, data)
                    logger.debug(
                        "Topic resolved | rule=%s strategy=%s topic=%s",
                        rule.__class__.__name__,
                        rule.routing_strategy().value,
                        topic,
                    )
                    return RoutingDecision(
                        target_topic=topic,
                        resolved_by=rule.__class__.__name__,
                        strategy=rule.routing_strategy(),
                        used_fallback=False,
                    )
            except Exception as exc:  # noqa: BLE001 — rule errors must not drop messages.
                logger.error(
                    "Routing rule failed — skipping | rule=%s error=%s",
                    rule.__class__.__name__,
                    exc,
                )
                self.routing_metrics["routing_errors"] += 1

        # Stage 2.2: No rule matched — use the configured fallback topic.
        logger.debug("No rule matched; routing to fallback_topic=%s", self.fallback_topic)
        self.routing_metrics["fallback_used"] += 1
        return RoutingDecision(
            target_topic=self.fallback_topic,
            resolved_by="fallback",
            strategy=RoutingStrategy.PRIORITY_BASED,
            used_fallback=True,
        )

    def _build_routing_headers(
        self, context: MessageRoutingContext, decision: RoutingDecision
    ) -> Dict[str, str]:
        """
        Produce a headers dict that stamps routing metadata onto the Kafka message.

        Why headers matter in production:
        - Consumers can filter or branch based on x-routing-priority without
          deserialising the payload.
        - Distributed tracing tools (Jaeger, Zipkin) can correlate events across
          topic lanes using source_service.
        - Ops dashboards can detect topic mis-routing without inspecting payloads.

        Args:
            context:  MessageRoutingContext used to resolve the topic.
            decision: RoutingDecision returned by _resolve_target_topic().

        Returns:
            Dict mapping header key strings to string values (Kafka header values
            must be bytes or strings depending on the client; SingletonProducer
            handles encoding).
        """
        resolved_priority = context.priority or self.default_priority
        return {
            HEADER_KEY_PRIORITY: resolved_priority.value,
            HEADER_KEY_CATEGORY: context.category or "",
            HEADER_KEY_SOURCE_SERVICE: context.source_service or self.service_name,
            HEADER_KEY_ROUTING_STRATEGY: decision.strategy.value,
        }

    def _update_metrics(self, topic: str) -> None:
        """Increment per-topic routing counters (Stage 5.0)."""
        self.routing_metrics["messages_routed"] += 1
        dist = self.routing_metrics["topic_distribution"]
        dist[topic] = dist.get(topic, 0) + 1

    # ------------------------------------------------------------------
    # Public send interface
    # ------------------------------------------------------------------

    def send_with_context(
        self,
        data: Dict[str, Any],
        context: MessageRoutingContext,
        **kwargs: Any,
    ) -> RoutingDecision:
        """
        Send a message with full routing context control.

        This is the primary send method — all convenience helpers delegate here.

        Stage 1.0: Accept data and context from the caller.
        Stage 2.0: Resolve target topic via the rule chain.
        Stage 3.0: Stamp routing metadata headers.
        Stage 4.0: Delegate to the dispatcher.
        Stage 5.0: Update routing metrics.

        Args:
            data:    Message payload as a Python dict.
            context: MessageRoutingContext describing the message's routing needs.
            **kwargs: Forwarded to the dispatcher (e.g. key=, partition=).

        Returns:
            RoutingDecision: The routing outcome.  Useful for logging and testing.

        Raises:
            Exception: Propagated from the dispatcher if the send fails.
        """
        # Stage 2.0: Topic resolution.
        decision = self._resolve_target_topic(context, data)

        # Stage 3.0: Build and merge routing headers.
        routing_headers = self._build_routing_headers(context, decision)
        existing_headers = kwargs.pop("headers", {})
        merged_headers = {**routing_headers, **existing_headers}

        try:
            # Stage 4.0: Dispatch the message.
            self._dispatcher.send(decision.target_topic, data, headers=merged_headers, **kwargs)

            # Stage 5.0: Record the routing outcome.
            self._update_metrics(decision.target_topic)

        except Exception:
            self.routing_metrics["routing_errors"] += 1
            raise

        return decision

    def send_with_priority(
        self,
        data: Dict[str, Any],
        priority: TopicPriority,
        category: Optional[str] = None,
        **kwargs: Any,
    ) -> RoutingDecision:
        """
        Send a message to the appropriate priority lane.

        Convenience wrapper that builds a MessageRoutingContext from a
        TopicPriority value.  This is the most common usage pattern.

        Args:
            data:     Message payload dict.
            priority: TopicPriority.HIGH / MEDIUM / LOW / BACKGROUND.
            category: Optional category label for content-based rules that also
                      inspect the context (e.g. combined priority + category routing).
            **kwargs: Forwarded to send_with_context().

        Returns:
            RoutingDecision describing which topic was selected and why.
        """
        context = MessageRoutingContext(
            priority=priority,
            category=category,
            source_service=self.service_name,
        )
        return self.send_with_context(data, context, **kwargs)

    def send_high_priority(self, data: Dict[str, Any], **kwargs: Any) -> RoutingDecision:
        """Route to the HIGH-priority topic lane."""
        return self.send_with_priority(data, TopicPriority.HIGH, **kwargs)

    def send_medium_priority(self, data: Dict[str, Any], **kwargs: Any) -> RoutingDecision:
        """Route to the MEDIUM-priority topic lane."""
        return self.send_with_priority(data, TopicPriority.MEDIUM, **kwargs)

    def send_low_priority(self, data: Dict[str, Any], **kwargs: Any) -> RoutingDecision:
        """Route to the LOW-priority topic lane."""
        return self.send_with_priority(data, TopicPriority.LOW, **kwargs)

    def send_background(self, data: Dict[str, Any], **kwargs: Any) -> RoutingDecision:
        """Route to the BACKGROUND task topic lane."""
        return self.send_with_priority(data, TopicPriority.BACKGROUND, **kwargs)

    def send_direct(self, topic: str, data: Dict[str, Any], **kwargs: Any) -> None:
        """
        Send directly to a named topic, bypassing the rule chain entirely.

        Use this when the correct topic is already known at the call site and
        routing overhead or rule evaluation is undesirable.

        Args:
            topic:   Target Kafka topic name.
            data:    Message payload dict.
            **kwargs: Forwarded to the dispatcher.
        """
        headers = kwargs.pop("headers", {})
        headers[HEADER_KEY_ROUTING_STRATEGY] = RoutingStrategy.DIRECT.value

        logger.info("Direct send | topic=%s", topic)
        try:
            self._dispatcher.send(topic, data, headers=headers, **kwargs)
            self._update_metrics(topic)
        except Exception:
            self.routing_metrics["routing_errors"] += 1
            raise

    # ------------------------------------------------------------------
    # Operations / observability
    # ------------------------------------------------------------------

    def get_routing_metrics(self) -> Dict[str, Any]:
        """
        Return a snapshot of routing counters for dashboards or health checks.

        Returns:
            Dict with:
            - messages_routed: total successful sends
            - routing_errors: total rule or dispatch failures
            - fallback_used: times no rule matched
            - topic_distribution: per-topic send counts
            - active_rules: current rule count
            - fallback_topic: configured fallback topic name
            - rule_registry: list of active rule class names in weight order
        """
        return {
            **self.routing_metrics,
            "active_rules": len(self._rules),
            "fallback_topic": self.fallback_topic,
            "rule_registry": [r.__class__.__name__ for r in self._rules],
        }

    def flush(self, timeout: float = DEFAULT_FLUSH_TIMEOUT_SECONDS) -> int:
        """
        Block until all queued messages are delivered or timeout expires.

        Args:
            timeout: Maximum seconds to wait.  Defaults to DEFAULT_FLUSH_TIMEOUT_SECONDS
                     (30 s) which matches the Kafka client default.

        Returns:
            Number of messages still queued after the timeout.  Non-zero does not
            mean messages were lost — retry flush() or let the producer background
            thread continue.
        """
        logger.debug("Flushing dispatcher | timeout=%.1fs", timeout)
        remaining = self._dispatcher.flush(timeout)
        logger.debug("Flush complete | remaining_in_queue=%d", remaining)
        return remaining


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def create_priority_router(
    service_name: str,
    default_priority: TopicPriority = TopicPriority.MEDIUM,
) -> ContentBasedRouter:
    """
    Build a ContentBasedRouter pre-configured for priority-lane routing.

    This is the recommended factory for the most common use case: routing
    messages to HIGH / MEDIUM / LOW / BACKGROUND topic lanes by priority.

    Args:
        service_name:     Logical service name (used as topic prefix).
        default_priority: Fallback priority lane if no rule matches.

    Returns:
        ContentBasedRouter with PriorityRoutingRule already installed.
    """
    return ContentBasedRouter(service_name, default_priority)


def create_content_router(
    service_name: str,
    field_routing_rules: Dict[str, Dict[str, str]],
) -> ContentBasedRouter:
    """
    Build a ContentBasedRouter with content-based rules pre-installed.

    Args:
        service_name:        Logical service name.
        field_routing_rules: Mapping of payload field names to value→topic maps.

                             Example:
                             {
                                 "event_type": {
                                     "payment.completed": "payments-done",
                                     "payment.failed":    "payments-failed",
                                 },
                                 "region": {
                                     "eu-west": "events-eu",
                                     "us-east": "events-us",
                                 },
                             }

    Returns:
        ContentBasedRouter with one ContentBasedRoutingRule per field entry,
        plus the default PriorityRoutingRule.
    """
    router = ContentBasedRouter(service_name)
    for field_name, value_topic_map in field_routing_rules.items():
        router.add_routing_rule(ContentBasedRoutingRule(field_name, value_topic_map))
    return router
