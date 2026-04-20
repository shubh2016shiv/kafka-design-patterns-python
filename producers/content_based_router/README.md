# Content-Based Router Producer

A production-minded Kafka producer pattern that selects the correct target topic for every
message dynamically — based on message priority, payload content, or entity identity — without
the calling code ever hardcoding a topic name.

This document assumes you are starting from zero. By the end you will understand what the
Content-Based Router pattern is, why topic routing is essential in real systems, how every
class and rule works, how to tune it for production, and how to talk about it confidently
in an interview.

---

## Table of Contents

1. [The Problem: Why One Topic Is Not Enough](#1-the-problem-why-one-topic-is-not-enough)
2. [What Is the Content-Based Router — Pattern Origin](#2-what-is-the-content-based-router--pattern-origin)
3. [The Three Routing Dimensions](#3-the-three-routing-dimensions)
4. [Where This Fits in the Producer Hierarchy](#4-where-this-fits-in-the-producer-hierarchy)
5. [System Architecture](#5-system-architecture)
6. [Module Structure](#6-module-structure)
7. [The 5-Stage Send Flow — Step by Step](#7-the-5-stage-send-flow--step-by-step)
8. [Routing Rule Reference](#8-routing-rule-reference)
9. [Data Models (types.py)](#9-data-models-typespy)
10. [Constants and Tuning (constants.py)](#10-constants-and-tuning-constantspy)
11. [Dispatcher Adapter Design (clients.py)](#11-dispatcher-adapter-design-clientspy)
12. [Production Decision Framework](#12-production-decision-framework)
13. [Running the Demo](#13-running-the-demo)
14. [Interview Talking Points](#14-interview-talking-points)
15. [What Comes Next](#15-what-comes-next)

---

## 1. The Problem: Why One Topic Is Not Enough

Consider a payment service that handles three completely different workloads:

- A user submits a card payment — needs to be processed in under 200ms (SLA: HIGH priority).
- A merchant runs a monthly payout job — can wait up to 10 minutes (BACKGROUND).
- A fraud rule triggers an account freeze — must be processed before anything else (CRITICAL).

If all three go to the same Kafka topic, you have no way to give them different processing
guarantees. A slow batch payout job sitting in the queue blocks the latency-critical card
payment. The fraud freeze waits behind both.

```
SINGLE TOPIC — ALL WORKLOADS MIXED               MULTI-TOPIC — ROUTED BY PRIORITY
─────────────────────────────────                ──────────────────────────────────────
                                                 payments-high-priority
payment-fraud-freeze  ──►                            │ → dedicated consumer, 10 replicas
payment-card-charge   ──►  payments-events           │   SLA: p99 < 100ms
payment-batch-payout  ──►  (all mixed)           payments-background-tasks
                           │ → one consumer          │ → single consumer, 1 replica
                           │   no priority           │   SLA: best-effort
                           │   no SLA isolation
```

The Content-Based Router solves this by sitting between your application code and the
Kafka broker. Your code says "this is a HIGH-priority payment" — the router decides
which topic it belongs to. Topic topology becomes an infrastructure concern, not an
application concern.

---

## 2. What Is the Content-Based Router — Pattern Origin

The Content-Based Router is a formal Enterprise Integration Pattern (EIP), first described
by Gregor Hohpe and Bobby Woolf in their 2003 book *Enterprise Integration Patterns*. It is
pattern number 7 in the Message Routing chapter.

**The formal definition:**

> "A Content-Based Router examines each incoming message and routes it to the correct
> channel based on the message's content."

In Kafka terms: "channels" are topics. The router examines the message and its metadata,
evaluates a chain of rules, and writes the message to the correct topic.

```
┌──────────────────────────────────────────────────────────────┐
│                CONTENT-BASED ROUTER (EIP ch.7)               │
│                                                              │
│          ┌─────────────────────────────────────┐            │
│          │          Incoming Message            │            │
│          └────────────────┬────────────────────┘            │
│                           │                                  │
│                    ┌──────▼──────┐                           │
│                    │ Rule Chain  │                           │
│                    │ (evaluate   │                           │
│                    │  in order)  │                           │
│                    └──────┬──────┘                           │
│             ┌─────────────┼──────────────┐                  │
│             ▼             ▼              ▼                   │
│        Channel A      Channel B      Channel C               │
│    (high-priority) (background) (payments-failed)            │
└──────────────────────────────────────────────────────────────┘
```

**Why this pattern name matters for interviews:**

When you say "Content-Based Router" in an interview, you are referencing a pattern from
the canonical distributed systems literature. It signals that you understand Kafka not as
a single queue but as a distributed channel infrastructure with routing semantics.

---

## 3. The Three Routing Dimensions

This implementation supports three routing strategies, each solving a different problem.

### 3.1 Priority-Based Routing

Route to different topic lanes based on business priority level.

```
MessageRoutingContext(priority=TopicPriority.HIGH)
    │
    ▼
PriorityRoutingRule
    │
    ▼
payments-service-high-priority  ← 10 consumer instances, SLA: p99 < 50ms
payments-service-medium-priority ← 3 consumer instances, SLA: p99 < 500ms
payments-service-low-priority    ← 1 consumer instance,  SLA: minutes
payments-service-background-tasks ← 1 consumer instance, SLA: hours
```

**When to use:** Any service that handles multiple tiers of urgency. This is the most
common production routing pattern and is always installed by default.

### 3.2 Content-Based Routing

Route to specific topics based on a value inside the message payload.

```
payload = {"event_type": "payment.completed", "amount": 99.99}
    │
    ▼
ContentBasedRoutingRule(match_field="event_type", value_to_topic={
    "payment.completed": "payments-completed",
    "payment.failed":    "payments-failed",
    "auth.login":        "auth-events",
})
    │
    ▼
payments-completed  ← settlement consumers only
payments-failed     ← retry / alerting consumers only
auth-events         ← security consumers only
```

**When to use:** One producer service emits heterogeneous event types and each type has
different consumers, retention policies, or replication factors.

### 3.3 Hash-Partition Routing

Deterministically distribute messages across N topics based on a hash of a field value.

```
payload = {"user_id": "user-042", "action": "profile_update"}
    │
    ▼
HashPartitionRoutingRule(hash_field="user_id", topic_prefix="user-events", num_buckets=4)
    │
    ▼  hash("user-042") % 4 = 2
    │
user-events-2  ← all events for user-042 always land here
               ← one consumer handles user-042 in sequence
               ← ordering per user is preserved
```

**When to use:** You need per-entity ordering (all events for the same user_id must be
processed in sequence) while still scaling consumer throughput horizontally across N buckets.

---

## 4. Where This Fits in the Producer Hierarchy

Each producer pattern in this project builds on the previous one.

```
┌────────────────────────────────────────────────────────────────────┐
│  LAYER 1 — Callback-Confirmed Producer                             │
│  Single topic, explicit delivery callbacks, structured results.    │
│  This is the baseline. Every producer pattern uses these concepts. │
├────────────────────────────────────────────────────────────────────┤
│  LAYER 2 — Singleton Producer                                      │
│  One shared producer instance per process.                        │
│  Solves: connection overhead in multi-threaded services.           │
├────────────────────────────────────────────────────────────────────┤
│  LAYER 3 — Content-Based Router  ◄─── THIS PACKAGE                │
│  Dynamic topic selection via pluggable rule chain.                 │
│  Solves: multi-topic architectures, priority queues, fan-out.      │
│  Depends on: Singleton Producer (via SingletonProducerGateway).    │
├────────────────────────────────────────────────────────────────────┤
│  LAYER 4 — Dead Letter Queue Producer                              │
│  Fault-tolerant send with circuit breaker and DLQ fallback.        │
│  Depends on: Content-Based Router for its routing decisions.       │
├────────────────────────────────────────────────────────────────────┤
│  LAYER 5 — Resilient Producer                                      │
│  Retry policies, bulkhead, sliding-window health monitor.          │
│  Depends on: Content-Based Router for multi-topic delivery.        │
└────────────────────────────────────────────────────────────────────┘
```

**Interview note:** This layered dependency is intentional. Each layer adds one concern.
Keeping concerns separated means you can test the Content-Based Router's routing logic
in isolation — without a circuit breaker, without DLQ logic, without a live broker.

---

## 5. System Architecture

### 5.1 High-Level: Message Flow Through the Router

```
┌────────────────────────────────────────────────────────────────────────┐
│  APPLICATION LAYER                                                     │
│  (your service, batch job, or demo script)                             │
│                                                                        │
│   router.send_with_priority(data, priority=TopicPriority.HIGH)         │
│                    │                                                   │
│                    ▼                                                   │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  CONTENT-BASED ROUTER (this package)                            │  │
│  │                                                                 │  │
│  │  Stage 1:  Build MessageRoutingContext                          │  │
│  │  Stage 2:  Walk rule chain → resolve target topic               │  │
│  │  Stage 3:  Stamp routing metadata into message headers          │  │
│  │  Stage 4:  dispatcher.send(topic, payload, headers)             │  │
│  │  Stage 5:  Update routing_metrics, return RoutingDecision       │  │
│  └───────────────────────────┬─────────────────────────────────────┘  │
│                              │                                         │
│                              ▼                                         │
│  ┌───────────────────────────────────────────────────────────────┐    │
│  │  SingletonProducerGateway  (clients.py)                       │    │
│  │  Wraps SingletonProducer → delegates to confluent_kafka       │    │
│  └───────────────────────────┬───────────────────────────────────┘    │
│                              │                                         │
│                    TCP / Kafka protocol                                 │
│                              │                                         │
│  ┌───────────────────────────▼───────────────────────────────────┐    │
│  │  KAFKA BROKER CLUSTER                                         │    │
│  │                                                               │    │
│  │  payments-service-high-priority                               │    │
│  │  ├── Partition 0: [msg@0] [msg@1] …                          │    │
│  │  └── Partition 1: [msg@0] …                                   │    │
│  │                                                               │    │
│  │  payments-service-background-tasks                            │    │
│  │  └── Partition 0: [msg@0] [msg@1] …                          │    │
│  └───────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Inside the Rule Chain: Chain of Responsibility

The rule chain is sorted by weight (highest first). The router walks the chain and stops
at the first rule whose `matches()` returns True. This is the **Chain of Responsibility**
design pattern.

```
  send_with_context(data, context)
          │
          ▼
  _resolve_target_topic(context, data)
          │
          │  Evaluate rules in weight order:
          │
          ├──► Rule ①: PriorityRoutingRule  (weight=100)
          │            matches() → True (priority is always set)
          │            get_topic() → "payments-service-high-priority"
          │            STOP — first match wins
          │
          │   (Rules below are never evaluated if ① matches first)
          │
          ├──► Rule ②: ContentBasedRoutingRule  (weight=50)
          │            matches() → True only if payload has the match_field
          │
          └──► Rule ③: HashPartitionRoutingRule  (weight=30)
                       matches() → True only if payload has the hash_field

          If no rule matches → fallback_topic (default priority lane)
```

**Critical insight for interviews:** The rule that matches first wins — rules below it
are never evaluated. This means **rule weight determines priority, not insertion order**.
If you add a ContentBasedRoutingRule with weight=150, it will run before the
PriorityRoutingRule even though priority routing was installed first.

### 5.3 The Routing Headers: Why Stamp Metadata onto Messages

Every message sent through this router carries four Kafka headers:

```
x-routing-priority       = "high-priority"
x-routing-category       = "payment"
x-routing-source-service = "payments-service"
x-routing-strategy       = "priority_based"
```

**Why this matters in production:**

- **Consumer filtering without payload deserialization.** A consumer can read the
  `x-routing-priority` header and decide whether to process or skip a message without
  parsing the JSON payload. This is significantly faster for message triage.

- **Distributed tracing.** `x-routing-source-service` links a message in a DLQ back to
  the originating service across topic boundaries.

- **Ops dashboards.** When messages land on the wrong topic (a routing bug), the
  `x-routing-strategy` header tells you immediately which rule misfired.

- **Audit trails.** The full routing context is embedded in the message itself, not just
  in application logs that may be rotated away.

---

## 6. Module Structure

Every file in this package has exactly one responsibility. This is not over-engineering —
it is the minimum separation needed to make the routing logic testable without a live broker.

```
producers/content_based_router/
├── __init__.py     ← Public API surface. Only import from here in application code.
├── constants.py    ← Rule weights, flush timeouts, header key strings. All defaults tunable.
├── types.py        ← Data contracts: MessageRoutingContext, RoutingDecision, RoutingStrategy,
│                      KafkaMessageDispatcher Protocol.
├── clients.py      ← SingletonProducerGateway adapter + build_singleton_dispatcher() factory.
├── core.py         ← All routing logic: RoutingRule ABC, three concrete rules, ContentBasedRouter.
├── demo.py         ← Educational walkthrough of all routing strategies.
├── __main__.py     ← Package CLI entrypoint for `python -m producers.content_based_router`.
└── README.md       ← This document.
```

### Why separate these files at all?

| File | Changes when... |
|---|---|
| `constants.py` | You tune rule weights or flush timeouts for a new environment. |
| `types.py` | You add a new routing dimension to `MessageRoutingContext` (e.g. `geo_region`). |
| `clients.py` | You swap the Kafka backend or add metrics instrumentation to the dispatcher. |
| `core.py` | Routing logic, rule evaluation order, or the `ContentBasedRouter` API changes. |
| `demo.py` | The educational flow or demo routing scenarios change. |

The most important separation is between `core.py` and `clients.py`. Core logic never
imports `confluent_kafka` directly. It speaks only the `KafkaMessageDispatcher` Protocol
defined in `types.py`. This means you can unit-test all routing decisions by injecting
a fake dispatcher — no broker, no network, no test containers required.

---

## 7. The 5-Stage Send Flow — Step by Step

Follow this in [`core.py`](core.py) — every stage is marked with a `# Stage N.N` comment.

```
  Caller: router.send_with_priority(data, priority=TopicPriority.HIGH)
                     │
                     │
         ┌───────────▼───────────────────────────────────────────┐
         │  Stage 1.0  BUILD MessageRoutingContext                │
         │                                                        │
         │  priority=HIGH, source_service="payments-service"      │
         │                                                        │
         │  Why: Separates routing concerns from payload schema.  │
         │  A payload rename never breaks routing logic.          │
         └───────────────────────┬───────────────────────────────┘
                                 │ context built
                                 ▼
         ┌───────────────────────────────────────────────────────┐
         │  Stage 2.0  RESOLVE TARGET TOPIC                       │
         │                                                        │
         │  Walk rule chain (highest weight first):               │
         │    ① PriorityRoutingRule.matches(ctx, data) → True    │
         │    ① PriorityRoutingRule.get_topic(ctx, data)         │
         │      → get_topic_name("payments-service", HIGH)       │
         │      → "payments-service-high-priority"               │
         │  STOP — first match wins.                              │
         │                                                        │
         │  Returns: RoutingDecision(                             │
         │    target_topic="payments-service-high-priority",      │
         │    resolved_by="PriorityRoutingRule",                  │
         │    strategy=RoutingStrategy.PRIORITY_BASED,            │
         │    used_fallback=False,                                │
         │  )                                                     │
         └───────────────────────┬───────────────────────────────┘
                                 │ topic resolved
                                 ▼
         ┌───────────────────────────────────────────────────────┐
         │  Stage 3.0  STAMP ROUTING HEADERS                     │
         │                                                        │
         │  {                                                     │
         │    "x-routing-priority":       "high-priority",        │
         │    "x-routing-category":       "",                     │
         │    "x-routing-source-service": "payments-service",     │
         │    "x-routing-strategy":       "priority_based",       │
         │  }                                                     │
         │                                                        │
         │  Caller headers (if any) are merged in and take        │
         │  precedence over routing headers.                      │
         └───────────────────────┬───────────────────────────────┘
                                 │ headers built
                                 ▼
         ┌───────────────────────────────────────────────────────┐
         │  Stage 4.0  DISPATCHER.SEND(topic, data, headers)     │
         │                                                        │
         │  Delegates to SingletonProducerGateway.send()          │
         │  → SingletonProducer.send()                            │
         │  → confluent_kafka.Producer.produce()                  │
         │                                                        │
         │  The message enters the producer's local send buffer.  │
         │  It is not yet on the broker at this point.            │
         │  A background I/O thread drains the buffer in batches. │
         └───────────────────────┬───────────────────────────────┘
                                 │ enqueued
                                 ▼
         ┌───────────────────────────────────────────────────────┐
         │  Stage 5.0  UPDATE ROUTING METRICS                    │
         │                                                        │
         │  routing_metrics["messages_routed"] += 1               │
         │  routing_metrics["topic_distribution"]                 │
         │    ["payments-service-high-priority"] += 1             │
         │                                                        │
         │  Returns: RoutingDecision (same object from Stage 2)   │
         └───────────────────────────────────────────────────────┘
```

### Stage 2.0 in depth: What happens when a rule raises an exception?

If a rule's `matches()` or `get_topic()` raises an exception, the router **catches it,
logs it, and continues to the next rule**. It does not crash.

This is a deliberate safety contract:

```python
for rule in self._rules:
    try:
        if rule.matches(context, data):
            topic = rule.get_topic(context, data)
            return RoutingDecision(...)
    except Exception as exc:
        logger.error("Routing rule %s failed: %s", rule.__class__.__name__, exc)
        self.routing_metrics["routing_errors"] += 1
        # Continue to next rule — never drop a message because of a bad rule.
```

**Production implication:** A buggy custom rule added at runtime cannot bring down the
entire routing infrastructure. It is skipped; the next rule handles the message. The error
counter increments and will appear in your monitoring dashboard.

### Stage 4.0 in depth: The message is not on the broker yet

When `dispatcher.send()` returns, the message is in the **producer client's in-memory
send buffer** — not on the Kafka broker. The broker receives it only after:

1. The batch fills (`batch.size` bytes reached), OR
2. The linger timer fires (`linger.ms` elapsed), OR
3. `flush()` is called explicitly.

For a long-running service this is fine — the background threads drain the buffer
continuously. For a short-lived script or a Lambda function, you **must** call
`router.flush()` before the process exits or messages will be lost.

---

## 8. Routing Rule Reference

### 8.1 `PriorityRoutingRule`

**Weight:** 100 (always runs first)

**What it does:** Routes to a topic lane named `{service_name}-{priority_value}` using the
`get_topic_name()` function from `config/kafka_config.py`.

**When it matches:** Any message that carries a `TopicPriority` in its context — which is
every message, since `priority` is a required field on `MessageRoutingContext`.

**Produced topics:**

```
payments-service-high-priority      ← TopicPriority.HIGH
payments-service-medium-priority    ← TopicPriority.MEDIUM
payments-service-low-priority       ← TopicPriority.LOW
payments-service-background-tasks   ← TopicPriority.BACKGROUND
```

**Consumer architecture this enables:**

```
payments-service-high-priority ──► Consumer Group A (10 instances, c5.xlarge, SLA: 50ms p99)
payments-service-medium-priority ► Consumer Group B (3 instances, t3.medium, SLA: 500ms p99)
payments-service-background-tasks ► Consumer Group C (1 instance, t3.small, SLA: minutes)
```

Each consumer group can be scaled, alerted on, and monitored entirely independently.
An incident on the background-tasks topic has zero impact on the high-priority lane.

**Convenience send methods built on this rule:**

```python
router.send_high_priority(data)       # → TopicPriority.HIGH
router.send_medium_priority(data)     # → TopicPriority.MEDIUM
router.send_low_priority(data)        # → TopicPriority.LOW
router.send_background(data)          # → TopicPriority.BACKGROUND
```

**Production tip:** This rule is always installed in `ContentBasedRouter.__init__()` before
any custom rules are added. Even if all your custom rules fail, priority routing is your
safety net.

---

### 8.2 `ContentBasedRoutingRule`

**Default weight:** 50 (runs after priority, before hash)

**What it does:** Inspects a specific field in the message payload and maps its value to
a topic name.

**Constructor:**

```python
ContentBasedRoutingRule(
    match_field="event_type",          # which payload field to inspect
    value_to_topic={                   # value → topic mapping
        "payment.completed": "payments-completed",
        "payment.failed":    "payments-failed",
        "auth.login":        "auth-events",
    },
    rule_weight=50,                    # optional: override default weight
)
```

**When it matches:** The payload dict contains `match_field` AND `data[match_field]` is
a key in `value_to_topic`. If either condition is false, the rule returns False and the
next rule in the chain is evaluated.

**What happens when the field is absent:**

```python
payload = {"amount": 99.99}  # no "event_type" field
# ContentBasedRoutingRule.matches() → False
# Rule is skipped. PriorityRoutingRule handles it via its fallback.
```

**Production tip:** Keep `value_to_topic` small and explicit. A rule with 100 entries is a
signal that you need a different partitioning strategy (e.g. hash-based) or a lookup table
backed by a configuration service.

**Stacking multiple content rules:**

```python
router.add_routing_rule(ContentBasedRoutingRule(
    match_field="region",
    value_to_topic={"eu-west": "events-eu", "us-east": "events-us"},
    rule_weight=80,   # evaluate before the default content rule
))

router.add_routing_rule(ContentBasedRoutingRule(
    match_field="event_type",
    value_to_topic={"payment.failed": "payments-failed"},
    rule_weight=50,
))
```

Both rules coexist in the chain. The region rule (weight=80) runs first; if it matches,
the event_type rule is never evaluated for that message.

---

### 8.3 `HashPartitionRoutingRule`

**Default weight:** 30 (runs last)

**What it does:** Hashes a payload field value modulo N to select one of N topic buckets.
The same field value always produces the same bucket index — this is deterministic.

**Constructor:**

```python
HashPartitionRoutingRule(
    hash_field="user_id",          # payload field to hash
    topic_prefix="user-events",    # topic name prefix
    num_buckets=4,                 # number of parallel topic buckets
    rule_weight=30,                # optional: override default weight
)
```

**Produced topics:**

```
user-events-0   ← receives messages where hash(user_id) % 4 == 0
user-events-1   ← receives messages where hash(user_id) % 4 == 1
user-events-2   ← receives messages where hash(user_id) % 4 == 2
user-events-3   ← receives messages where hash(user_id) % 4 == 3
```

**Ordering guarantee:** All messages with the same `user_id` always land on the same
bucket topic. If one consumer owns `user-events-2`, all events for user-042 are
processed by that consumer in order. Per-entity ordering is preserved.

**How the hash works:**

```python
bucket_index = abs(hash(str(data[self.hash_field]))) % self.num_buckets
topic = f"{self.topic_prefix}-{bucket_index}"
```

`abs()` is required because Python's `hash()` can return negative values. Modulo of
a negative number is negative in Python, which would produce invalid topic names.

**Important warning about Python's hash randomization:**

Python 3.3+ randomizes `hash()` per process startup (controlled by `PYTHONHASHSEED`).
`hash("user-042")` in process A will produce a different value than in process B.

However, `hash(str(value))` **is stable for string inputs** within a single producer
process — all messages sent in one session route consistently. But across deployments or
process restarts, the bucket assignment may change.

**Production decision:** For strict cross-deployment consistency, use a stable hash
function like `hashlib.md5` or `hashlib.sha256`. For most use cases where consumers are
stateless (no per-entity state pinned to a specific consumer), Python's `hash()` is
fine — rebalancing across buckets on restart causes no correctness problems.

---

### 8.4 `send_direct`: Bypassing the Rule Chain

Sometimes the correct topic is already known at the call site. Use `send_direct()` to
bypass all rules entirely:

```python
router.send_direct("audit-log-topic", {"event": "admin_action", "actor": "user-99"})
```

`send_direct()` stamps `x-routing-strategy: direct` into the headers and writes
directly to the named topic. This is appropriate for:

- Audit / security events that must go to a fixed topic regardless of priority.
- DLQ routing where the dead-letter topic is determined by the caller.
- Integration tests that need to target a specific topic predictably.

---

### 8.5 Writing a Custom Rule

Extend `RoutingRule` and implement three methods:

```python
from producers.content_based_router.core import RoutingRule
from producers.content_based_router.types import MessageRoutingContext, RoutingStrategy

class TenantRoutingRule(RoutingRule):
    """
    Routes messages to tenant-specific topics for multi-tenant architectures.
    Each enterprise tenant gets message isolation and independent consumer scaling.
    """

    TENANT_TOPICS = {
        "acme-corp":      "acme-corp-events",
        "globex-inc":     "globex-events",
        "initech-llc":    "initech-events",
    }

    def matches(self, context: MessageRoutingContext, data: dict) -> bool:
        # Match if the routing context carries a known tenant_id.
        return context.tenant_id in self.TENANT_TOPICS

    def get_topic(self, context: MessageRoutingContext, data: dict) -> str:
        return self.TENANT_TOPICS[context.tenant_id]

    def weight(self) -> int:
        return 90  # Run before content rules, after... well, adjust as needed.

    def routing_strategy(self) -> RoutingStrategy:
        return RoutingStrategy.CUSTOM

# Register it:
router.add_routing_rule(TenantRoutingRule())
```

**The rule is immediately active.** `add_routing_rule()` re-sorts the chain so the
new rule is evaluated in the correct weight position on the very next send.

---

## 9. Data Models (types.py)

### 9.1 `MessageRoutingContext`

The routing envelope that travels with every outbound message.

```python
@dataclass
class MessageRoutingContext:
    priority: TopicPriority           # Required. Routing lane selector.
    category: Optional[str] = None   # Free-text label for content rules.
    source_service: Optional[str] = None  # Stamped into message headers.
    target_service: Optional[str] = None  # For direct-routing rules.
    user_id: Optional[str] = None    # For hash-partition rules.
    tenant_id: Optional[str] = None  # For multi-tenant routing.
    routing_key: Optional[str] = None  # Arbitrary string for custom rules.
    processing_timeout_ms: Optional[int] = None  # Consumer SLA hint.
    retry_count: int = 0             # Re-attempt count for DLQ poison-pill detection.
```

**Why not just inspect the payload dict?**

Inspecting the raw payload couples routing rules to payload schema. If someone renames
`event_type` to `event_name`, every routing rule that reads that field breaks. With
`MessageRoutingContext`, routing rules read a stable typed envelope. Payload schema
changes never affect routing correctness.

**`retry_count` — the DLQ integration point:**

The Dead Letter Queue producer (see `producers/dead_letter_queue/`) increments
`retry_count` before re-routing a failed message. A custom routing rule can detect
poison pills:

```python
class PoisonPillRoutingRule(RoutingRule):
    def matches(self, context, data):
        return context.retry_count >= 5  # Tried 5 times — it's a poison pill.

    def get_topic(self, context, data):
        return "poison-pill-quarantine"  # Isolate it from the normal flow.

    def weight(self):
        return 200  # Must run FIRST — before priority rule even fires.
```

### 9.2 `RoutingDecision`

Immutable record of one completed routing evaluation.

```python
@dataclass(frozen=True)
class RoutingDecision:
    target_topic: str      # The Kafka topic selected.
    resolved_by: str       # Class name of the matching rule, or "fallback".
    strategy: RoutingStrategy  # Which routing strategy was used.
    used_fallback: bool    # True when no rule matched.
```

**Why return this instead of just routing silently?**

In a system without `RoutingDecision`, the only way to know which topic a message went
to is to parse the application log. If logging is disabled or logs are lost, you have
no audit trail.

`RoutingDecision` is returned by every public send method:

```python
decision = router.send_with_priority(data, TopicPriority.HIGH)
logger.info(
    "Routed to topic=%s via rule=%s strategy=%s fallback=%s",
    decision.target_topic,
    decision.resolved_by,
    decision.strategy.value,
    decision.used_fallback,
)
```

**`used_fallback=True` is a signal worth alerting on.** It means no rule matched and the
default topic was used. In a well-configured system this should not happen in steady state.
An alert on `routing_metrics["fallback_used"] > 0` catches misconfigured routing rules
before they cause consumer-side problems.

**`frozen=True` — why immutable?**

A `RoutingDecision` describes something that already happened. Mutating it after the fact
would corrupt the audit trail. Frozen dataclasses also work as dict keys and in sets,
making them useful for deduplication and metrics aggregation.

### 9.3 `RoutingStrategy` Enum

```python
class RoutingStrategy(Enum):
    PRIORITY_BASED = "priority_based"   # TopicPriority → lane selection
    CONTENT_BASED  = "content_based"    # Payload field value → topic
    HASH_PARTITION = "hash_partition"   # hash(field) % N → bucket topic
    DIRECT         = "direct"           # Bypass routing; caller names topic
    CUSTOM         = "custom"           # Application-defined rule
```

**Why an Enum instead of string constants?**

- IDEs auto-complete valid values. Typos are caught at import time, not at runtime.
- If you add `GEOGRAPHIC_SHARD` to the enum, any code that switches on `RoutingStrategy`
  gets a static analysis warning that it has an unhandled case.
- Log output is human-readable (`"priority_based"`) not a raw integer.

### 9.4 `KafkaMessageDispatcher` Protocol

The interface that `ContentBasedRouter` requires from its Kafka backend.

```python
class KafkaMessageDispatcher(Protocol):
    def send(self, topic: str, data: Dict[str, Any], **kwargs: Any) -> None: ...
    def flush(self, timeout: float) -> int: ...
```

**Why a Protocol instead of an abstract base class?**

With a Protocol, any class that implements `send()` and `flush()` is automatically
compatible — no inheritance required. This is **structural subtyping**.

```python
# Production: SingletonProducerGateway satisfies KafkaMessageDispatcher.
router = ContentBasedRouter("payments-service")

# Tests: a simple fake also satisfies it — no real broker needed.
class FakeDispatcher:
    def __init__(self):
        self.sent_messages = []

    def send(self, topic, data, **kwargs):
        self.sent_messages.append({"topic": topic, "data": data})

    def flush(self, timeout):
        return 0  # nothing queued

fake = FakeDispatcher()
router = ContentBasedRouter("payments-service", dispatcher=fake)
router.send_high_priority({"order_id": 1})
assert fake.sent_messages[0]["topic"] == "payments-service-high-priority"
```

**This is the correct way to unit-test routing logic** — inject the fake, send messages,
assert on `fake.sent_messages`. No Docker, no Kafka, no network.

---

## 10. Constants and Tuning (constants.py)

### Rule Evaluation Weights

Rule weights determine evaluation order. Higher weight = evaluated first.

| Constant | Default | Controls |
|---|---|---|
| `PRIORITY_RULE_WEIGHT` | 100 | `PriorityRoutingRule` evaluation order |
| `CONTENT_RULE_DEFAULT_WEIGHT` | 50 | Default `ContentBasedRoutingRule` order |
| `HASH_PARTITION_RULE_DEFAULT_WEIGHT` | 30 | Default `HashPartitionRoutingRule` order |

**Choosing weights for custom rules:**

```
200+   →  Must run before everything, including the priority rule.
          Use for: poison-pill detection, security overrides.

101–199  →  Runs before priority but after critical overrides.
            Use for: tenant routing, feature flags.

100    →  PriorityRoutingRule sits here.

51–99  →  Runs after priority, before content rules.
          Use for: geo-routing, A/B testing overrides.

50     →  ContentBasedRoutingRule default.

31–49  →  Runs after content rules, before hash rules.
          Use for: supplementary content rules with lower specificity.

30     →  HashPartitionRoutingRule default.

1–29   →  Last resort before fallback.
          Use for: catch-all rules that handle uncategorized messages.
```

### Flush Timeout

```
DEFAULT_FLUSH_TIMEOUT_SECONDS = 30.0
```

Controls how long `router.flush()` blocks waiting for the producer buffer to drain.
30 seconds matches the Kafka client default `request.timeout.ms`.

**When to tune:**

```
Integration tests (local broker):    5.0 seconds
Batch jobs (flush once at end):     60.0 seconds
Interactive services (frequent flush): 5.0 seconds
```

### Routing Header Key Strings

```python
HEADER_KEY_PRIORITY         = "x-routing-priority"
HEADER_KEY_CATEGORY         = "x-routing-category"
HEADER_KEY_SOURCE_SERVICE   = "x-routing-source-service"
HEADER_KEY_ROUTING_STRATEGY = "x-routing-strategy"
```

These are the exact strings stamped into Kafka message headers. Document them in your
team's internal Kafka schema registry so consumer teams know what to expect. If you rename
them, coordinate with all consumer teams — consumers reading headers by key name will
silently miss the new header values.

---

## 11. Dispatcher Adapter Design (clients.py)

The `ContentBasedRouter` never speaks directly to `confluent_kafka`. It speaks to a
`KafkaMessageDispatcher` — specifically, a `SingletonProducerGateway`.

```
ContentBasedRouter
        │  calls
        ▼
KafkaMessageDispatcher (Protocol — type only, no runtime cost)
        │  satisfied by
        ▼
SingletonProducerGateway
        │  wraps
        ▼
SingletonProducer.get_instance()
        │  wraps
        ▼
confluent_kafka.Producer  (the actual network layer)
```

### Why this layering?

**Layer 1: `KafkaMessageDispatcher` Protocol**

The router depends on a contract, not a concrete class. This means tests inject a fake
dispatcher with zero Kafka infrastructure.

**Layer 2: `SingletonProducerGateway`**

The gateway is the **seam** — the narrow, replaceable boundary between routing logic and
Kafka internals. Future additions (metrics counters, header pre-processing, circuit
breaking) are added to the gateway class without touching `ContentBasedRouter`.

**Layer 3: `SingletonProducer`**

Ensures one shared `confluent_kafka.Producer` instance per process. All routing strategies
(priority, content, hash) share the same underlying connection pool and send buffer. You
never have multiple TCP connections to the broker for the same producer application.

### The `build_singleton_dispatcher()` factory

```python
def build_singleton_dispatcher(config=None) -> SingletonProducerGateway:
    # Stage 1.0: Resolve config (caller-supplied or PRODUCTION defaults).
    # Stage 2.0: Obtain the singleton producer instance.
    # Stage 3.0: Wrap in gateway and return.
```

Called automatically by `ContentBasedRouter.__init__()` when no `dispatcher=` is
injected. In production code you never call this directly — just instantiate the router.

### Injecting a custom dispatcher

```python
# Scenario: test with a recording fake.
class RecordingDispatcher:
    def __init__(self):
        self.calls = []

    def send(self, topic, data, **kwargs):
        self.calls.append({"topic": topic, "payload": data, "headers": kwargs.get("headers")})

    def flush(self, timeout):
        return 0

fake = RecordingDispatcher()
router = ContentBasedRouter("order-service", dispatcher=fake)
router.send_high_priority({"order_id": 42})

assert len(fake.calls) == 1
assert fake.calls[0]["topic"] == "order-service-high-priority"
assert fake.calls[0]["headers"]["x-routing-strategy"] == "priority_based"
```

---

## 12. Production Decision Framework

Use this checklist when deploying the Content-Based Router in a production service.

### Step 1: Define your topic topology before writing any routing rules

Topics must exist in Kafka before messages are routed to them (unless
`auto.create.topics.enable=true` on the broker, which should be disabled in production).

Draw your topic map first:

```
order-service-high-priority     → 12 partitions, replication=3, retention=7 days
order-service-medium-priority   → 6 partitions,  replication=3, retention=7 days
order-service-low-priority      → 3 partitions,  replication=2, retention=3 days
order-service-background-tasks  → 1 partition,   replication=2, retention=1 day
```

Rule of thumb for partition count: `partitions = max_consumer_throughput / single_partition_throughput`.
For a service producing 10,000 messages/sec with each partition handling ~1,000 msg/sec,
use 10–12 partitions on the high-priority topic.

### Step 2: Choose default priority conservatively

```python
router = ContentBasedRouter("order-service", default_priority=TopicPriority.MEDIUM)
```

The `default_priority` controls the `fallback_topic` — what happens when no rule matches.
`MEDIUM` is a safe default: it is processed, just not at the highest SLA. If `fallback_used`
increases in your metrics, investigate why rules are not matching.

**Never set the fallback to `BACKGROUND` for a service that also produces critical events.**
If a routing rule bug causes everything to fall back, your critical messages silently land
in the background lane and may not be processed for hours.

### Step 3: Set rule weights deliberately

| Scenario | Recommended weight |
|---|---|
| Poison-pill / circuit-breaker override | 200 |
| Tenant isolation in multi-tenant system | 150 |
| A/B test overrides | 110 |
| `PriorityRoutingRule` (do not change) | 100 |
| Event-type routing (payments, auth) | 60–80 |
| `ContentBasedRoutingRule` default | 50 |
| Hash-based fan-out | 30 |
| Catch-all / fallback custom rules | 10 |

### Step 4: Monitor `routing_metrics` in production

```python
metrics = router.get_routing_metrics()
# {
#   "messages_routed": 14820,
#   "routing_errors": 0,
#   "fallback_used": 3,           ← Non-zero means unmatched messages. Investigate.
#   "topic_distribution": {
#     "order-service-high-priority": 8230,
#     "order-service-medium-priority": 5987,
#     "order-service-background-tasks": 600,
#   },
#   "active_rules": 2,
#   "fallback_topic": "order-service-medium-priority",
#   "rule_registry": ["PriorityRoutingRule", "ContentBasedRoutingRule"],
# }
```

**What to alert on:**

- `routing_errors > 0` — A rule is crashing. Messages are still delivered (via fallback)
  but the rule chain has a bug. Fix the rule.
- `fallback_used` growing — Messages are reaching the router without matching any rule.
  Either the rule configuration is wrong or a new message type was introduced without
  a corresponding routing rule.
- `topic_distribution` skewed unexpectedly — If `high-priority` suddenly carries 95% of
  traffic, something upstream is misclassifying message priorities.

### Step 5: Flush before process exit

For Lambda functions, cron jobs, and CLI tools:

```python
remaining = router.flush(timeout=30.0)
if remaining > 0:
    logger.warning(
        "Flush incomplete: %d messages still in buffer. "
        "They will be lost when the process exits.",
        remaining,
    )
    sys.exit(1)  # Signal to the orchestrator that cleanup failed.
```

For long-running services, the producer background threads flush continuously. An explicit
flush at shutdown still ensures in-flight messages drain before the process dies.

### Step 6: Use `send_direct()` sparingly

`send_direct()` bypasses all rules. If overused, it re-introduces the problem of
hardcoded topic names in application code — the exact thing the Content-Based Router
was designed to eliminate.

**Appropriate uses:**
- DLQ producers that know their dead-letter topic at the point of failure.
- Integration test setup that needs a predictable topic.
- Admin/audit log producers with a fixed, compliance-mandated destination.

**Inappropriate uses:**
- Normal business events. If you always know the topic at the call site, you don't
  need the router — use `SingletonProducer` directly.

---

## 13. Running the Demo

### Prerequisites

Start the local Kafka infrastructure from the project root:

```bash
python infrastructure/scripts/kafka_infrastructure.py up
python infrastructure/scripts/kafka_infrastructure.py health
```

Kafka UI will be available at [http://localhost:8080](http://localhost:8080).

### Run the Content-Based Router demo

```bash
# Recommended package entrypoint (new)
python -m producers.content_based_router

# Equivalent legacy module path
python -m producers.content_based_router.demo
```

The demo walks through all routing strategies in sequence without requiring you to touch
any code:

```
================================================================
  Content-Based Router — Pattern Demonstration
================================================================

Stage A — Priority-Based Routing
  Rule: PriorityRoutingRule (weight=100) maps TopicPriority
        to separate topic lanes via get_topic_name().

  ✓ HIGH   → high-priority lane
    topic       = demo-service-high-priority
    resolved by = PriorityRoutingRule
    strategy    = priority_based
    fallback?   = False

  ✓ MEDIUM → medium-priority lane
    topic       = demo-service-medium-priority
    ...

Stage B — Content-Based Routing
  Rule: ContentBasedRoutingRule inspects payload['event_type']
        and maps its value directly to a topic.

  ✓ event_type='payment.completed'
    topic       = demo-service-payments-completed
    resolved by = ContentBasedRoutingRule
    strategy    = content_based
    fallback?   = False

Stage C — Hash-Partition Routing
  Rule: HashPartitionRoutingRule hashes payload['user_id']
        modulo 3 to distribute across 3 topic buckets.

  ✓ user_id='user-001'
    topic       = demo-service-user-shard-2
    resolved by = HashPartitionRoutingRule
    strategy    = hash_partition
    fallback?   = False

Stage D — Direct Send (rule chain bypassed)

  ✓ Sent directly to topic='demo-service-audit-log'

Stage E — Routing Metrics Report

  messages_routed             = 10
  routing_errors              = 0
  fallback_used               = 0
  topic_distribution          = {demo-service-high-priority: 1, ...}
  active_rules                = 3
  fallback_topic              = demo-service-medium-priority
  rule_registry               = ['PriorityRoutingRule', ...]
```

**You can run the demo without a live Kafka broker.** The routing decisions (Stages A–D)
are fully deterministic and happen before any network call. If the broker is unavailable,
the demo catches the `send` exception, prints the error, and continues — so you can study
routing logic offline.

### Run with the unit tests (no Kafka required)

```bash
python -m unittest test.producer.test_dead_letter_queue_producer -v
```

The dead letter queue tests exercise `ContentBasedRouter` routing logic via shims, with
zero broker dependency.

---

## 14. Interview Talking Points

**Q: What is the Content-Based Router pattern and where does it come from?**

The Content-Based Router is a formal Enterprise Integration Pattern (EIP) introduced by
Hohpe and Woolf in 2003. It examines each incoming message and routes it to the correct
channel — in Kafka terms, the correct topic — based on message content or metadata.
The pattern is pattern #7 in EIP's Message Routing chapter. In Kafka implementations,
channels are topics, and the router evaluates a chain of rules to select the right one.

**Q: Why would you have multiple topics instead of one?**

A single topic cannot give different consumer groups independent scaling, SLAs, or resource
allocation. If a batch job's messages sit in the same topic as fraud alerts, the fraud
consumer has no way to process alerts ahead of batch work. Multiple topics per priority tier
let you assign different numbers of consumer instances, different consumer thread pools,
different Kafka ACLs, and different retention policies to each tier independently.
The Content-Based Router eliminates the need for every producer call site to know which
specific topic is correct.

**Q: How does the rule chain work? What happens if two rules both match?**

Rules are sorted by weight (highest first). The router evaluates them in that order and
stops at the **first match** — lower-weight rules are never evaluated for that message.
This is the Chain of Responsibility design pattern. If two rules both match the same
message, the one with higher weight always wins. If no rule matches, the router falls
back to the configured `fallback_topic`.

**Q: What is the difference between `send_with_priority()` and `send_with_context()`?**

`send_with_priority()` is a convenience wrapper for the most common case. It builds a
`MessageRoutingContext` with just the priority and category fields set, then delegates to
`send_with_context()`. `send_with_context()` is the primary send method — it accepts a
fully populated `MessageRoutingContext` with all routing dimensions (user_id for hash rules,
tenant_id for tenant rules, routing_key for custom rules) and handles the full five-stage
flow: resolve topic, stamp headers, dispatch, update metrics, return RoutingDecision.

**Q: How do you test routing logic without a live Kafka broker?**

Inject a fake dispatcher that satisfies the `KafkaMessageDispatcher` Protocol. Since
`ContentBasedRouter` depends on the Protocol, not on `confluent_kafka` directly, any
object with `send()` and `flush()` methods works. The fake records calls to
`sent_messages`. After routing, assert that the right topic was selected based on the
rule chain's deterministic evaluation. All routing logic can be verified this way — no
Docker, no broker, no integration infrastructure needed for unit tests.

**Q: What does `used_fallback=True` in a RoutingDecision mean in production?**

It means no rule in the chain matched this particular message, and the router fell back to
the default topic (configured via `default_priority` in the constructor). This should not
happen in a well-configured system. Monitor `routing_metrics["fallback_used"]` and alert if
it grows, because it indicates either a misconfigured rule, a new message type without a
corresponding rule, or a routing rule bug that caused unexpected non-matches.

**Q: What is the `MessageRoutingContext` and why not inspect the payload directly?**

`MessageRoutingContext` is a typed routing envelope separate from the message payload.
Rules inspect the envelope, not the payload dict. This separation means a payload schema
change (renaming a field) never breaks routing rules. Rules read a stable typed surface.
It also makes routing decisions fully observable — you can log the full context to know
exactly what information drove the topic selection, without deserializing the message body.

**Q: What does `flush()` return and why does it matter for short-lived processes?**

`flush()` blocks until the producer's internal send buffer drains or the timeout expires.
It returns the number of messages still in the buffer after the timeout. A non-zero return
means those messages are in memory but have not yet reached the Kafka broker. In a
long-running service, background threads will eventually drain them. In a Lambda function,
cron job, or CLI script, the process will exit and those messages will be permanently lost.
Always call `router.flush()` and check the return value before exiting a short-lived process.

---

## 15. What Comes Next

The Content-Based Router is the third layer in this producer pattern hierarchy. Having
understood it, you can now study the patterns that depend on it:

| Pattern | Location | What it adds | Depends on |
|---|---|---|---|
| **Callback-Confirmed** | `producers/callback_confirmed/` | Delivery callbacks, structured results | ← Start here |
| **Singleton Producer** | `producers/singleton/` | Shared producer instance, thread safety | Callback-Confirmed |
| **Content-Based Router** | `producers/content_based_router/` | Dynamic topic selection, routing rules | Singleton Producer |
| **Dead Letter Queue** | `producers/dead_letter_queue/` | Fault tolerance, DLQ fallback on failure | Content-Based Router |
| **Resilient Producer** | `producers/resilient/` | Circuit breaker, retry policy, bulkhead | Content-Based Router |

**A good learning sequence:**

1. Read `callback_confirmed/README.md` and run its demo — this is the foundational pattern.
2. Read `singleton/singleton_producer.py` — understand why one shared producer instance matters.
3. Study this package completely: read `types.py` → `constants.py` → `clients.py` → `core.py`.
4. Run the demo (`python -m producers.content_based_router`) with and without a broker.
5. Write a custom routing rule following the pattern in section 8.5.
6. Move to `dead_letter_queue/` — observe how it depends on `ContentBasedRouter` for routing
   messages to the DLQ topic.
7. Move to `resilient/` — understand how circuit breaking and retry policies layer on top
   of the routing infrastructure built here.

**Key concepts to lock in before moving on:**

- The rule chain is a Chain of Responsibility. First match wins.
- `MessageRoutingContext` decouples routing logic from payload schema.
- `RoutingDecision` makes routing observable without log parsing.
- The `KafkaMessageDispatcher` Protocol is the test boundary — inject a fake there.
- `flush()` is mandatory before process exit in short-lived processes.
- `fallback_used > 0` in production metrics is a routing misconfiguration signal.
