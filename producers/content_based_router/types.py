"""
Content-Based Router — shared type contracts.

Why this module exists:
  All data shapes used by routing rules and the router class live here.
  Centralising them means adding a new routing dimension (e.g. geo-region routing)
  only requires extending MessageRoutingContext — rule and router code stays stable.

ASCII contract map:

    Caller builds MessageRoutingContext
               │
               ▼
    ContentBasedRouter._resolve_target_topic(context, payload)
               │  (evaluates rules in weight order)
               ▼
    RoutingRule.matches(context, payload) ──True──► RoutingRule.get_topic(context, payload)
               │                                              │
               │  (no match — try next rule)                  │
               ▼                                              ▼
          fallback topic                              RoutingDecision(used_fallback=False)
               │
               ▼
    RoutingDecision(used_fallback=True)
"""

from __future__ import annotations

# dataclass: lightweight value objects with auto __repr__ and __eq__ — no boilerplate.
from dataclasses import dataclass

# Enum: named constants prevent magic-string bugs and surface allowed values in IDEs.
from enum import Enum

# Optional, Protocol: explicit nullability and structural typing for clean interfaces.
from typing import Any, Dict, Optional, Protocol

# TopicPriority is the shared vocabulary between the config layer and routing rules.
# It maps to actual Kafka topic suffixes defined in kafka_config.py.
try:
    from config.kafka_config import TopicPriority
except ImportError:  # pragma: no cover — fallback for installed-package layout.
    from kafka.config.kafka_config import TopicPriority  # type: ignore[no-redef]


class RoutingStrategy(Enum):
    """
    Enumerates the routing strategies supported by this package.

    Use this enum instead of magic strings when selecting or logging which
    strategy resolved a target topic.  Adding a new variant here forces
    callers that branch on the value to handle it explicitly.

    Interview note — when to choose each strategy:

        PRIORITY_BASED  — Most common production pattern.  Route to separate
                          HIGH / MEDIUM / LOW / BACKGROUND topic lanes so each
                          tier can be scaled and monitored independently.

        CONTENT_BASED   — Route on a field value inside the payload
                          (e.g. event_type="payment" → payments-topic).
                          Useful when one service produces heterogeneous events.

        HASH_PARTITION  — Deterministic fan-out: hash(user_id) % N selects topic-N.
                          Preserves per-entity ordering across N parallel topics
                          while still enabling horizontal consumer scaling.

        DIRECT          — Caller names the topic explicitly; routing is a pass-through.
                          Use when the topic is already known at the call site.

        CUSTOM          — Application-defined rule registered at runtime via
                          ContentBasedRouter.add_routing_rule().
    """

    PRIORITY_BASED = "priority_based"
    CONTENT_BASED = "content_based"
    HASH_PARTITION = "hash_partition"
    DIRECT = "direct"
    CUSTOM = "custom"


@dataclass
class MessageRoutingContext:
    """
    Routing envelope attached to every outbound message.

    Why a separate object instead of inspecting the raw payload dict directly:
    - Decouples routing logic from payload schema changes.  A payload field rename
      does not break routing rules.
    - Gives rules a stable, typed surface to match against.
    - Makes routing decisions fully observable: log this object to know exactly
      what information drove the topic selection.

    Production tip:
      In high-throughput systems, populate only the fields your rules actually
      use — unused optional fields carry zero routing overhead.

    Fields:
        priority            — Optional Kafka lane selector that maps to TopicPriority.
                              Set this when you want explicit priority-lane routing.
                              Leave as None when content/hash/custom rules should
                              decide the topic first.

        category            — Free-text label for content-based rules.
                              Examples: "payment", "notification", "audit".

        source_service      — Originating service name.  Stamped into Kafka message
                              headers so consumers know who produced the event.

        target_service      — Intended consuming service; used by direct-routing rules
                              to resolve a canonical topic without hardcoding strings.

        user_id             — User scope for hash-partition routing rules.

        tenant_id           — Tenant scope for multi-tenant fan-out architectures.

        routing_key         — Arbitrary string for custom rule matching.

        processing_timeout_ms — Consumer SLA hint in milliseconds.  Informational;
                              no Kafka protocol enforcement occurs.

        retry_count         — Number of prior delivery attempts for this message.
                              Dead-letter routing rules use this to detect poison pills
                              (e.g. retry_count >= 3 → route to DLQ topic).
    """

    priority: Optional[TopicPriority] = None
    category: Optional[str] = None
    source_service: Optional[str] = None
    target_service: Optional[str] = None
    user_id: Optional[str] = None
    tenant_id: Optional[str] = None
    routing_key: Optional[str] = None
    processing_timeout_ms: Optional[int] = None
    retry_count: int = 0


@dataclass(frozen=True)
class RoutingDecision:
    """
    Immutable record of one completed routing evaluation.

    Why this exists:
    - Makes routing decisions observable without string-parsing log output.
    - Tests can assert on structured objects rather than log strings.
    - Enables routing metrics to accumulate without side-effecting core logic.

    Fields:
        target_topic    — Kafka topic the router selected for this message.
        resolved_by     — Class name of the matching rule, or "fallback" when no
                          rule matched and the default topic was used.
        strategy        — RoutingStrategy variant that produced this decision.
        used_fallback   — True when the rule chain exhausted without a match.
    """

    target_topic: str
    resolved_by: str
    strategy: RoutingStrategy
    used_fallback: bool


class KafkaMessageDispatcher(Protocol):
    """
    Minimal interface the ContentBasedRouter requires from its Kafka backend.

    Why a Protocol instead of an ABC:
    - Structural typing lets any object that satisfies the shape be used as a
      dispatcher — no inheritance required.
    - Tests inject a fake dispatcher without touching production Kafka clients.
    - SingletonProducer, confluent Producer, and kafka-python Producer all satisfy
      this contract; the router stays backend-agnostic.
    """

    def send(self, topic: str, data: Dict[str, Any], **kwargs: Any) -> None:
        """Enqueue one message for delivery to the given topic."""

    def flush(self, timeout: float) -> int:
        """
        Wait for queued messages to drain and return remaining queue size.

        A return value > 0 means some messages did not drain within `timeout`
        seconds — the caller should decide whether to retry or raise.
        """
