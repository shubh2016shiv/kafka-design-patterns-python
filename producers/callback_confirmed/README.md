# Callback-Confirmed Producer

A complete, production-minded Kafka producer pattern with input validation, metadata preflight
checks, delivery callbacks, and structured result objects.

This document assumes you are starting from zero. By the end you will understand what a Kafka
producer is, why this specific pattern exists, how every configuration key behaves, how to tune
it for production, and how to talk about it confidently in an interview.

---

## Table of Contents

1. [What Is Apache Kafka — 60-Second Primer](#1-what-is-apache-kafka--60-second-primer)
2. [What Is a Producer](#2-what-is-a-producer)
3. [Producer Pattern Hierarchy](#3-producer-pattern-hierarchy)
4. [System Architecture](#4-system-architecture)
5. [Module Structure](#5-module-structure)
6. [The 5-Stage Workflow — Step by Step](#6-the-5-stage-workflow--step-by-step)
7. [Configuration Reference](#7-configuration-reference)
8. [Data Models (types.py)](#8-data-models-typespy)
9. [Protocol and Adapter Design (clients.py)](#9-protocol-and-adapter-design-clientspy)
10. [Production Decision Framework](#10-production-decision-framework)
11. [Running the Demo](#11-running-the-demo)
12. [Interview Talking Points](#12-interview-talking-points)
13. [What Comes Next](#13-what-comes-next)

---

## 1. What Is Apache Kafka — 60-Second Primer

Imagine a company where dozens of services need to talk to each other. The checkout service
needs to tell the inventory service, the notification service, the analytics service, and the
fraud service all at once when an order is placed. Without Kafka, every service has to directly
call every other service — this creates a tangled web of dependencies.

Kafka solves this by acting as a **shared message log** — a durable, ordered, append-only stream
of events. Instead of services calling each other, they publish events to Kafka and let interested
services consume them independently.

```
BEFORE KAFKA (direct coupling)                 AFTER KAFKA (decoupled)
─────────────────────────────                  ──────────────────────────────────
checkout → inventory                           checkout
checkout → notifications         ────────►         │
checkout → analytics                               ▼
checkout → fraud                              [ Kafka Topic ]
                                                   │    │    │    │
                                               inventory  notif  analytics  fraud
```

**Core vocabulary:**

| Term | What it means |
|---|---|
| **Topic** | A named, append-only log. Like a database table, but for events. |
| **Partition** | A topic is split into N parallel partitions for scalability. |
| **Offset** | Each message in a partition gets a sequential integer ID (its offset). |
| **Producer** | Code that writes messages into a topic. |
| **Consumer** | Code that reads messages from a topic. |
| **Broker** | A Kafka server that stores and serves messages. |
| **Bootstrap servers** | The initial broker addresses your client connects to for metadata. |

---

## 2. What Is a Producer

A Kafka producer is client code that takes a message (a key + a value) and writes it to a
topic on the Kafka broker. The message is serialized to bytes, routed to a partition, and
appended to that partition's log.

**What a producer must do correctly:**

1. Serialize the message to bytes (Kafka speaks bytes, not Python objects).
2. Choose a partition (via key hash, round-robin, or custom logic).
3. Handle acknowledgement — did the broker actually receive it?
4. Retry on transient failures.
5. Buffer and batch messages for throughput efficiency.
6. Report delivery status so the caller knows what happened.

The simplest possible producer can be written in 5 lines. The problem is that those 5 lines
give you no visibility into failures, no retry guarantees, no structured result to inspect, and
no way to test without a live Kafka broker. This package solves all of that.

---

## 3. Producer Pattern Hierarchy

There are three core producer patterns in Kafka literature. Each is a trade-off between
simplicity, reliability, and performance.

```
┌─────────────────────────────────────────────────────────────────────┐
│  PATTERN 1 — Fire and Forget                                        │
│  produce() → done. No callback. No confirmation.                    │
│  Risk: message may be lost silently. No way to know.                │
│  When to use: logging, telemetry, non-critical events.              │
├─────────────────────────────────────────────────────────────────────┤
│  PATTERN 2 — Callback-Confirmed ◄─── THIS PACKAGE                  │
│  produce() → poll() → flush() → delivery callback fires.            │
│  Guarantees: callback tells you exactly what succeeded/failed.      │
│  When to use: orders, payments, inventory, any critical business     │
│  event where you need proof of delivery.                            │
├─────────────────────────────────────────────────────────────────────┤
│  PATTERN 3 — Transactional                                          │
│  produce() multiple messages atomically. All or nothing.            │
│  Risk: higher latency, more broker coordination overhead.           │
│  When to use: exactly-once across multiple topics/partitions.       │
└─────────────────────────────────────────────────────────────────────┘
```

**Why Callback-Confirmed is the right starting point:**

- It mirrors how most production services are built.
- Callbacks give you real delivery proof without the coordination overhead of transactions.
- It forces you to understand the async send / poll / flush lifecycle, which underpins all
  other patterns.
- The structured result objects make it testable without a live broker.

---

## 4. System Architecture

### 4.1 High-Level: Where This Producer Fits

```
┌────────────────────────────────────────────────────────────────────────┐
│  APPLICATION LAYER                                                     │
│  (your service, a demo script, a batch job)                            │
│                    │                                                   │
│                    │ calls produce_message(topic, key, value)          │
│                    ▼                                                   │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  CALLBACK-CONFIRMED PRODUCER  (this package)                    │  │
│  │                                                                 │  │
│  │  Stage 1: Validate inputs                                       │  │
│  │  Stage 2: Load environment config                               │  │
│  │  Stage 3: Preflight — check broker + topic metadata             │  │
│  │  Stage 4: Serialize → produce → poll → flush                    │  │
│  │  Stage 5: Return ProduceMessageResult                           │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                    │                                                   │
│                    │ TCP connection (bootstrap.servers)                │
│                    ▼                                                   │
│  ┌───────────────────────────────────────────────────────────────┐    │
│  │  KAFKA BROKER  (Docker / localhost:9094)                       │    │
│  │                                                               │    │
│  │  Topic: orders.created.v1                                     │    │
│  │  ├── Partition 0: [msg@0] [msg@1] [msg@2] ...                 │    │
│  │  ├── Partition 1: [msg@0] [msg@1] ...                         │    │
│  │  └── Partition 2: [msg@0] ...                                 │    │
│  └───────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Inside the Producer: The Async Send Pipeline

This is the most important thing to understand about any Kafka producer client.

When you call `producer.produce(...)`, the message does **not** go to Kafka immediately.
It goes into a local in-memory buffer. The producer client runs background I/O threads that
drain this buffer in batches. This is what makes Kafka producers fast — they batch many
messages into fewer network round trips.

```
  YOUR CODE               PRODUCER CLIENT (confluent_kafka / kafka-python)        BROKER
  ─────────               ─────────────────────────────────────────────────        ──────
  produce(msg)
      │
      ▼
  Message placed into ──► [  In-Memory Send Buffer  ]
  local buffer                      │
                                    │  Background I/O threads
                                    │  batch and send over TCP
                                    ▼
                           Network → Kafka Broker
                                    │
                                    │  Broker writes to partition log
                                    │
                                    ▼
                           Broker sends ACK back
                                    │
  poll(timeout) ──────────────────► │
  (serve callbacks)                 │
                                    ▼
                           delivery_callback(error=None, msg=metadata)
                                    │
  flush(timeout) ─────────────────► Wait for buffer to drain completely
```

**Why `poll()` before `flush()`?**

`poll()` gives the producer's I/O thread a chance to fire delivery callbacks for messages
that have already been acknowledged. In a one-shot script (run → send → exit), without
`poll()`, the callbacks may never fire at all because the process exits before the
background threads can deliver them.

`flush()` then blocks until the send buffer is fully drained. The return value is the number
of messages still queued — zero means all messages reached the broker.

---

## 5. Module Structure

Every file in this package has a single responsibility. Understanding why each file exists
is as important as understanding what it contains.

```
producers/callback_confirmed/
├── __init__.py          ← Public API surface. What callers import from.
├── constants.py         ← Timing defaults. Conservative values for one-shot scripts.
├── types.py             ← Contracts and result models. The "shape" of every I/O boundary.
├── clients.py           ← Kafka client adapters. Isolates confluent_kafka vs kafka-python.
├── core.py              ← The 5-stage workflow. All business logic lives here.
└── demo.py              ← Teaching demo. Realistic event + round-trip verification.
```

### Why separate these files at all?

In a single-file script, everything is tangled: the Kafka client, the config, the business
logic, and the demo all share the same namespace. This makes it impossible to:

- Test the workflow logic without a live Kafka broker.
- Swap the Kafka client library (confluent vs kafka-python) without touching business logic.
- Change configuration without touching logic.
- Import only what you need in application code.

The separation follows the principle that **code that changes for different reasons should
live in different files**. Config changes when environments change. Clients change when
library versions change. Core logic changes when requirements change.

| File | Changes when... |
|---|---|
| `constants.py` | Timeout defaults need tuning for a new environment. |
| `types.py` | The shape of results or contracts changes. |
| `clients.py` | You add a new Kafka library or update client versions. |
| `core.py` | Produce workflow steps or validation rules change. |
| `demo.py` | The teaching flow or demo event schema changes. |

---

## 6. The 5-Stage Workflow — Step by Step

This is the most important section. Follow the stages in [`core.py`](core.py) as you read.

```
  INPUT: topic_name, message_key, message_value
       │
       ▼
  ┌─────────────────────────────────────────────────────┐
  │  Stage 1.0  VALIDATE INPUTS                          │
  │  Guard clause before any network call.               │
  │  Raises ValueError for invalid topic/key/value.      │
  │  Why first: fail fast without wasting broker I/O.    │
  └───────────────────────┬─────────────────────────────┘
                          │ ✓ inputs valid
                          ▼
  ┌─────────────────────────────────────────────────────┐
  │  Stage 2.0  LOAD CONFIG                             │
  │  config_loader(environment=...) → Dict              │
  │  Why injected: tests pass fake configs.             │
  │  Why environment-aware: dev ≠ prod bootstrap.       │
  └───────────────────────┬─────────────────────────────┘
                          │ ✓ config loaded
                          ▼
  ┌─────────────────────────────────────────────────────┐
  │  Stage 3.0  KAFKA READINESS PREFLIGHT               │
  │  Stage 3.1: Is bootstrap.servers present in config? │
  │  Stage 3.2: Can admin client fetch cluster metadata? │
  │  Stage 3.3: Is there at least 1 visible broker?     │
  │  Stage 3.4: Does the target topic exist?            │
  │  Returns KafkaReadinessReport(ready=True/False).    │
  │  If ready=False: return early with error details.   │
  │  Why optional: readiness checks add latency. High-  │
  │  throughput services skip them after initial check. │
  └───────────────────────┬─────────────────────────────┘
                          │ ✓ broker + topic confirmed
                          ▼
  ┌─────────────────────────────────────────────────────┐
  │  Stage 4.0  PRODUCE + POLL + FLUSH                  │
  │  Stage 4.1: Build producer client from config.      │
  │  Stage 4.2: Serialize message_value → UTF-8 JSON.   │
  │  Stage 4.3: producer.produce(topic, key, bytes,     │
  │             callback=delivery_report)               │
  │             → message enters local send buffer.     │
  │  Stage 4.4: producer.poll(timeout)                  │
  │             → serve callbacks for already-acked     │
  │             messages. Needed in short-lived scripts. │
  │  Stage 4.5: remaining = producer.flush(timeout)     │
  │             → drain buffer. remaining==0 means ok.  │
  │  Stage 4.6: remaining > 0 → timeout failure.        │
  └───────────────────────┬─────────────────────────────┘
                          │ ✓ flush drained (or error)
                          ▼
  ┌─────────────────────────────────────────────────────┐
  │  Stage 5.0  RETURN ProduceMessageResult             │
  │  Structured outcome: topic, key, success, size,     │
  │  flush_remaining, nested readiness, error_message.  │
  │  Why structured: callers can inspect, log, test     │
  │  each field without parsing log strings.            │
  └─────────────────────────────────────────────────────┘
```

### Stage 3 in depth: Why Readiness Preflight Matters

Without a preflight check, a failed produce attempt gives you one of these unhelpful outcomes:

- A timeout after 30 seconds because the broker was unreachable.
- A cryptic broker error code about an unknown topic.
- Silence — message went into the buffer, flush timed out, no clear reason why.

With the preflight check, you get an actionable `KafkaReadinessReport` that tells you exactly:
- Can I reach the broker at all?
- How many brokers are visible?
- Does the target topic exist?
- Is there a metadata error on the topic?

This turns a 30-second mystery into a 5-second diagnosis.

### Stage 4.3 in depth: The Delivery Callback

The delivery callback (`delivery_report`) is called by the producer client's I/O thread
when a message is acknowledged (success) or fails permanently (after retries exhausted).

```python
def delivery_report(delivery_error, delivered_message):
    if delivery_error is not None:
        # Permanent failure after retries — log and investigate.
        logger.error("Message delivery failed: %s", delivery_error)
        return
    # Success — broker confirmed write to topic/partition/offset.
    logger.info(
        "Message delivered to %s [%s] @ offset %s",
        delivered_message.topic(),
        delivered_message.partition(),
        delivered_message.offset(),
    )
```

**Critical interview point:** The callback fires asynchronously on the client's I/O thread,
not on your application thread. This is why `poll()` must be called to give the callback a
chance to run. Without `poll()`, in a short-lived script, the callback may never execute.

### Stage 4.4 in depth: callback-confirmed success contract

In this pattern, `success=True` is intentionally strict and requires all three conditions:

1. `flush_remaining_messages == 0`,
2. delivery callback was observed,
3. callback did not report a delivery error.

Why this matters:
- queue-drain alone is not enough for callback-confirmed semantics,
- callback failure must be treated as delivery failure,
- missing callback after `poll()+flush()` is an "unknown outcome" and should be treated as failure.

### Stage 4.5 in depth: non-strict mode and structured failure results

When `raise_on_error=False`, runtime failures should return a structured
`ProduceMessageResult(success=False, error_message=...)` instead of raising unexpectedly.
This includes producer-factory errors, serialization errors, callback failures, and flush timeouts.
That makes calling code simpler and safer in scripts, jobs, and operational tooling.

---

## 7. Configuration Reference

Every key in the producer configuration controls a specific trade-off. Understanding each one
lets you tune for your workload and explain your choices in production or in an interview.

The configuration is assembled by `get_producer_config()` in [`config/kafka_config.py`](../../config/kafka_config.py).

### 7.1 Connection

#### `bootstrap.servers`

```
Default:  10.60.1.204:9092,10.60.1.205:9092,10.60.1.206:9092 (prod)
          localhost:9094 (local dev)
```

The initial broker addresses your producer connects to on startup. **This is not the full
broker list** — it is just the entry point for fetching cluster metadata. After connecting to
one bootstrap server, the client discovers all other brokers automatically.

**Production decision:** List 3+ brokers so that if one is down during startup, another
can serve the initial metadata request. Never list only one server in production.

```
bootstrap.servers = "broker1:9092,broker2:9092,broker3:9092"
```

#### `client.id`

```
Default: "simple-producer-demo" (demo), "enterprise-producer" (prod)
```

A logical name for this producer instance. Shows up in broker logs, metrics dashboards,
and Kafka UI. Has no effect on correctness — it exists entirely for observability.

**Production decision:** Use `{service-name}-{instance-role}-producer`. For example:
`checkout-service-order-events-producer`. This lets you instantly identify the source of
a problem in Grafana or CloudWatch.

### 7.2 Durability and Reliability

#### `acks`

```
Options: "0" | "1" | "all"
Default: "all" (with idempotence enabled)
```

This is the most important durability setting. It controls how many broker replicas must
acknowledge a write before the producer considers it successful.

```
acks=0  →  Producer does not wait for any acknowledgement.
           Maximum throughput. No durability guarantee.
           A broker crash between receive and replicate loses the message silently.
           Use for: metrics fire-and-forget, non-critical logging.

acks=1  →  Only the partition leader must acknowledge.
           Faster than "all". Still at risk: if the leader crashes before
           replicating to followers, the message is lost.
           Use for: moderate-criticality events where some loss is acceptable.

acks=all → The leader AND all in-sync replicas (ISR) must acknowledge.
           Slowest. Strongest durability guarantee.
           Use for: orders, payments, inventory, any business-critical event.
```

**Interview talking point:** `acks=all` does not mean all replicas in the cluster — it means
all replicas in the **In-Sync Replica set (ISR)**. If a follower falls behind (lag exceeds
`replica.lag.time.max.ms`), it is removed from ISR. This is why ISR size matters in
production monitoring.

#### `enable.idempotence`

```
Default: true
Requires: acks=all, retries > 0, max.in.flight.requests.per.connection <= 5
```

Without idempotence, retrying a failed produce can create duplicate messages. The broker
has no way to tell "is this a new message or a retry of the same message?"

With idempotence enabled, each producer gets a unique Producer ID (PID) and each message
gets a sequence number. The broker deduplicates by (PID, partition, sequence). A retry of
the same message is recognized and discarded at the broker level.

**Production decision:** Always enable idempotence for business events. The cost is
negligible. The benefit — no silent duplicates — is significant.

#### `retries`

```
Default: 2147483647 (effectively unlimited)
```

How many times the producer will retry a failed send before giving up. The default of
max-int means the producer retries forever (bounded by `delivery.timeout.ms`).

**Why max-int?** Kafka recommends this in combination with `enable.idempotence=true`.
Unlimited retries + idempotence = "retry forever but never produce duplicates." The
`delivery.timeout.ms` (default 120 seconds) is the real time-bound limit.

**Production decision:** Keep at max-int when idempotence is enabled. Only reduce retries
if you have a specific latency SLA that requires fast failure over reliable delivery.

#### `retry.backoff.ms`

```
Default: 100 (milliseconds)
```

Wait time between retry attempts. Prevents hammering a struggling broker.

**Production decision:** 100ms is fine for most cases. Increase to 500ms–1000ms if your
broker tier is underpowered or if retries are causing cascading load.

### 7.3 Throughput and Batching

#### `linger.ms`

```
Default: 5 (milliseconds)
```

How long the producer waits before sending a batch, hoping more messages arrive to fill it.

```
linger.ms=0  →  Send immediately when produce() is called.
               Lowest latency. Worst throughput (many tiny network requests).
               Use for: interactive UIs, real-time gaming events.

linger.ms=5  →  Wait 5ms for the batch to fill.
               Good balance for most backend services.

linger.ms=50 →  Wait 50ms. Excellent throughput for batch pipelines.
               Acceptable for async event streaming.
               Use for: analytics pipelines, ETL, high-volume log shipping.
```

**Interview talking point:** `linger.ms` and `batch.size` work together. A batch is sent
when either the batch is full OR `linger.ms` has elapsed — whichever comes first.

#### `batch.size`

```
Default: 16384 (16 KB)
```

Maximum bytes per batch per partition. Once a batch reaches this size, it is sent
regardless of `linger.ms`.

**Production decision:**

```
Low-latency services:     batch.size=16384  (16 KB, default)
High-throughput pipelines: batch.size=65536  (64 KB)
Maximum throughput:        batch.size=1048576 (1 MB) with linger.ms=50+
```

Increasing `batch.size` uses more memory but reduces network round trips. The right value
depends on your message size distribution and latency tolerance.

#### `compression.type`

```
Options: "none" | "gzip" | "snappy" | "lz4" | "zstd"
Default: "snappy" (in this project's config)
```

Compression happens at the batch level before network send. The broker stores compressed
batches and consumers decompress on read.

```
none   →  No compression. Maximum CPU efficiency. Highest network/storage cost.
gzip   →  High compression ratio. High CPU cost. Slow.
snappy →  Moderate compression ratio. Low CPU cost. Fast. Google's format.
lz4    →  Slightly better ratio than snappy. Even faster. Good default.
zstd   →  Best ratio and good speed. Requires Kafka 2.1+ and recent client.
```

**Production decision:** Use `lz4` or `zstd` for modern stacks. Use `snappy` for
compatibility with older clients. Use `gzip` only when storage cost dominates network cost
and you have CPU headroom.

### 7.4 Message Size

#### `message.max.bytes`

```
Default: 20000000 (20 MB in this project's config)
         1048576  (1 MB, standard Kafka default)
```

Maximum size of a single message in bytes. Must be set consistently with the broker's
`message.max.bytes` and the topic's `max.message.bytes` — the smallest value wins.

**Production decision:** The standard 1 MB default is right for most event-driven systems.
Increase only if your payload schema genuinely requires it (e.g., embedded images, large
JSON documents). Large messages hurt batching efficiency and increase GC pressure.

### 7.5 Ordering and Concurrency

#### `max.in.flight.requests.per.connection`

```
Default: 1 (when idempotence is enabled)
         5 (when idempotence is disabled)
```

How many unacknowledged requests can be in-flight to a single broker at the same time.

**Why 1 with idempotence?** With multiple in-flight requests, if request 1 fails and
request 2 succeeds, then request 1 is retried — it may now be delivered after request 2,
breaking ordering. Setting this to 1 ensures strict ordering. With idempotence enabled,
Kafka allows up to 5 in-flight requests while still guaranteeing ordering (via sequence
numbers), but setting to 1 is the safest choice for strict ordering guarantees.

**Production decision:** Set to 1 for ordered, idempotent producers. Set to 5 if you
need maximum throughput and strict ordering is not required.

### 7.6 Summary Table

| Config Key | Controls | Conservative (safe) | Aggressive (fast) |
|---|---|---|---|
| `acks` | Durability | `"all"` | `"1"` |
| `enable.idempotence` | Deduplication | `true` | `false` |
| `retries` | Fault tolerance | `2147483647` | `3` |
| `retry.backoff.ms` | Retry pacing | `100` | `50` |
| `linger.ms` | Latency vs throughput | `0` | `50` |
| `batch.size` | Batching efficiency | `16384` | `1048576` |
| `compression.type` | CPU vs network | `"snappy"` | `"lz4"` |
| `max.in.flight.requests` | Ordering vs throughput | `1` | `5` |

---

## 8. Data Models (types.py)

This package uses `@dataclass(frozen=True)` for all result objects. Frozen dataclasses are
immutable — once created, no field can be changed. This prevents accidental mutation of
results after they are returned from a function.

There are no Pydantic models in this package because validation of producer inputs happens
via explicit `validate_produce_request()` logic, not schema parsing. Pydantic would be
appropriate here if the producer accepted structured event schemas from an external source
(an API or message bus) that needed to be parsed and validated against a schema contract.

### 8.1 `KafkaReadinessReport`

Returned by `verify_kafka_readiness()` before any produce attempt.

```python
@dataclass(frozen=True)
class KafkaReadinessReport:
    ready: bool                    # True only if broker AND topic checks passed.
    bootstrap_servers: str         # What endpoint was used for the metadata check.
    broker_count: int              # How many brokers are visible in cluster metadata.
    topic_name: str                # Which topic was checked.
    topic_exists: bool             # Was the topic found in metadata?
    topic_partition_count: int     # How many partitions does the topic have?
    error_message: Optional[str]   # Actionable error string when ready=False.
```

**Why does this exist?** Without it, you would have to parse log strings to understand why
a produce failed. With it, you can write code like:

```python
if not report.ready:
    alert(f"Kafka not ready: {report.error_message}")
    return
```

**Production scenario:** A deployment check script uses `KafkaReadinessReport` to verify
the target topic exists before the service starts accepting traffic. If `ready=False`,
the deployment is rolled back automatically.

### 8.2 `ProduceMessageResult`

Returned by `produce_message()` — the top-level public API.

```python
@dataclass(frozen=True)
class ProduceMessageResult:
    topic_name: str                # Which topic was targeted.
    message_key: Optional[str]     # What key was used (affects partitioning).
    success: bool                  # True only if flush drained completely.
    serialized_size_bytes: int     # How large was the serialized payload.
    flush_remaining_messages: int  # Messages still in buffer after flush timeout.
    readiness: KafkaReadinessReport # Nested preflight result.
    error_message: Optional[str]   # Actionable error when success=False.
```

**Why `flush_remaining_messages`?** A non-zero value means messages were in the local
buffer when flush timed out. The produce call returned before those messages reached
the broker. In a long-running service, the next flush attempt will drain them. In a
short-lived script (cron job, Lambda, Cloud Run), the process may exit before the flush
completes — those messages are lost.

**Production scenario:** A batch job checks `result.flush_remaining_messages > 0` and
logs a warning to a PagerDuty alert channel before exiting.

### 8.3 `ConsumedEventVerification`

Used by the teaching demo to confirm a produced message can be read back.

```python
@dataclass(frozen=True)
class ConsumedEventVerification:
    consumed: bool                  # True if the expected event was found.
    topic_name: str                 # Which topic was consumed from.
    expected_demo_run_id: str       # The unique run ID we were looking for.
    received_event: Optional[Dict]  # The actual event payload if found.
    error_message: Optional[str]    # Why consume failed if consumed=False.
```

**Why does this exist?** It separates two different failure modes:
- "Produce succeeded but consume verification timed out" — broker issue or slow consumer.
- "Produce failed" — problem is upstream, not in consume path.

Without this distinction, a timeout in consume verification would look the same as a
produce failure.

### 8.4 `DemoRoundTripResult`

Wraps produce + consume verification into a single return value for the demo.

```python
@dataclass(frozen=True)
class DemoRoundTripResult:
    produce_result: ProduceMessageResult
    consume_verification: ConsumedEventVerification
```

**Why nested rather than flat?** Each sub-result carries its own complete context.
If you log `produce_result` separately from `consume_verification`, you get the right
level of detail for each phase without mixing concerns.

### 8.5 Protocol Interfaces: `KafkaProducerLike` and `KafkaAdminClientLike`

These are not dataclasses — they are Python `Protocol` types. A Protocol defines a
structural interface without requiring inheritance.

```python
class KafkaProducerLike(Protocol):
    def produce(self, topic, key, value, callback) -> None: ...
    def poll(self, timeout) -> int: ...
    def flush(self, timeout) -> int: ...
```

**Why use Protocol instead of abstract base class?**

With a Protocol, any class that implements `produce`, `poll`, and `flush` is automatically
compatible — including `confluent_kafka.Producer`, `KafkaPythonProducerAdapter`, and
`FakeProducer` in tests. No inheritance required. This is called **structural subtyping**
(or duck typing with static checking).

**What this enables:** The test file passes a `FakeProducer` to `produce_message()` without
touching a real Kafka broker. The core workflow code never knows the difference.

---

## 9. Protocol and Adapter Design (clients.py)

The Kafka Python ecosystem has two dominant client libraries:

| Library | Notes |
|---|---|
| `confluent_kafka` | Wraps the official C library (librdkafka). Fast, production-grade. |
| `kafka-python` | Pure Python. Easier to install. Slower. Good for learning environments. |

This package supports both via a **try/except import + adapter pattern**:

```
clients.py import resolution:
──────────────────────────────
try:
    import confluent_kafka         ← prefer: production-grade, fast
except ImportError:
    confluent_kafka = None

try:
    import kafka                   ← fallback: pure Python, portable
except ImportError:
    kafka = None

default_producer_factory():
    if confluent_kafka available → return confluent_kafka.Producer(config)
    if kafka available          → return KafkaPythonProducerAdapter(config)
    else                        → raise ImportError (no client installed)
```

**Why an adapter?** `confluent_kafka.Producer` and `kafka.KafkaProducer` have different
method signatures. The adapter (`KafkaPythonProducerAdapter`) wraps `kafka.KafkaProducer`
and exposes the Confluent-style `produce(topic, key, value, callback)` interface. The core
workflow code only speaks the `KafkaProducerLike` Protocol — it never sees the library-
specific differences.

**Interview talking point:** This is the **Adapter design pattern**. The adapter translates
one interface into another without changing the underlying behavior. It is also why
`kafka-python`'s future-based delivery is wrapped into Confluent-style callbacks.

### 9.1 Topic creation idempotency strategy in demo flows

The demo topic setup path should treat "already exists" as a successful idempotent outcome.
A robust approach is:

1. check typed exceptions first (most reliable),
2. then inspect structured error attributes like `code`/`name`,
3. use message-text matching only as a compatibility fallback.

This is safer than string-only checks because client error text can vary by library version
or environment.

---

## 10. Production Decision Framework

Use this framework when tuning the producer for a real service.

### Step 1: Classify your message criticality

```
TIER A — Business-critical (orders, payments, inventory mutations)
  acks=all, enable.idempotence=true, retries=max, linger.ms=0–5

TIER B — Operational (audit logs, notifications, state change events)
  acks=all, enable.idempotence=true, retries=max, linger.ms=5–10

TIER C — Analytics / telemetry (page views, click events, metrics)
  acks=1, enable.idempotence=false, linger.ms=50, batch.size=65536
```

### Step 2: Set `linger.ms` based on your throughput need

```
< 100 messages/sec      → linger.ms=0 or 5 (latency matters more than throughput)
100–10,000 messages/sec → linger.ms=5 or 10
> 10,000 messages/sec   → linger.ms=20–50 (throughput matters more)
```

### Step 3: Size `batch.size` to match your message size

A good rule: `batch.size` should hold roughly 10–20 average messages. If your average
message is 1 KB, use 16 KB. If your average message is 5 KB, try 65–100 KB.

### Step 4: Choose compression based on environment

```
Local dev / testing:  compression.type=none (less CPU, easier debugging)
Production services:  compression.type=lz4 or snappy
Data pipelines:       compression.type=zstd (best ratio if broker supports it)
```

### Step 5: Decide on `max.in.flight.requests.per.connection`

```
Strict ordering required (e.g., user profile updates): 1
High throughput, no strict ordering:                   5
```

### Step 6: Set timeouts proportionate to your SLA

The three timeout constants in `constants.py` are tuned for one-shot demo scripts:

```python
DEFAULT_POLL_TIMEOUT_SECONDS    = 1.0   # Fine for one-shot scripts.
DEFAULT_FLUSH_TIMEOUT_SECONDS   = 10.0  # 10 seconds to drain buffer.
DEFAULT_METADATA_TIMEOUT_SECONDS = 5.0  # 5 seconds to verify broker reachability.
```

For a long-running service:
- `flush_timeout`: increase to 30–60 seconds for large buffers.
- `metadata_timeout`: keep at 5 seconds (fast failure on startup is better than slow).
- `poll_timeout`: not applicable — long-running services use event loops or threads.

### Step 7: secure configuration logging

Always sanitize configs before logging. Redact a broad set of secret markers, not only
`password` and `secret`. In production, also treat keys containing terms such as `token`,
`api_key`, `private_key`, `keyfile`, and OAuth/JWT/credential markers as sensitive.

Operational principle: a false-positive redaction is acceptable; leaking credentials is not.

## 11. Running the Demo

### Prerequisites

Start the local Kafka infrastructure first (from the project root):

```bash
python infrastructure/scripts/kafka_infrastructure.py up
python infrastructure/scripts/kafka_infrastructure.py health
```

Kafka UI will be available at [http://localhost:8080](http://localhost:8080).

### Run the callback-confirmed producer demo

From the project root:

```bash
python -m producers.callback_confirmed_producer
```

What happens:

1. Reads bootstrap server from `infrastructure/.env` (falls back to `localhost:9094`).
2. Builds a demo `order_created` event with a unique `demo_run_id`.
3. Creates the demo topic if it does not exist.
4. Logs the pre-produce checklist (topic, key, event preview).
5. Logs the sanitized producer config and per-key explanations.
6. Runs the 5-stage workflow: validate → config → preflight → produce+flush → result.
7. Consumes the message back from the topic and verifies the `demo_run_id` matches.
8. Logs the full `DemoRoundTripResult`.

### Expected output structure

```
INFO  Stage 1: bootstrap servers resolved
INFO  Demo topic: demo-order-events-v1-{suffix}
INFO  What event is being sent: { "event_name": "order_created", ... }
INFO  Producer config (sanitized): { "bootstrap.servers": "localhost:9094", ... }
INFO  Config bootstrap.servers → Initial broker endpoints used for metadata bootstrap.
INFO  Config acks → Durability threshold for successful publish acknowledgement.
INFO  Pre-produce checklist: ...
INFO  Kafka preflight: broker_count=1, topic_exists=True, partitions=1
INFO  Message delivered to demo-order-events-v1-{suffix} [0] @ offset 0
INFO  Produce result: ProduceMessageResult(success=True, ...)
INFO  Event received back from topic: { "event_name": "order_created", ... }
```

### Run the unit tests (no Kafka required)

```bash
python -m unittest test.producer.test_callback_confirmed_producer -v
```

All 11 tests run with fake producers and fake admin clients — no broker needed.

---

## 12. Interview Talking Points

These are the questions interviewers ask most often about Kafka producers, and how to
answer them using concepts from this package.

**Q: What is the difference between `acks=1` and `acks=all`?**

`acks=1` means only the partition leader must acknowledge. If the leader crashes before
replicating to followers, the message is lost. `acks=all` means all in-sync replicas must
acknowledge — the message is safe as long as at least one ISR member remains alive.
The tradeoff is latency: `acks=all` waits for replica acknowledgements, which adds
one additional network round trip compared to `acks=1`.

**Q: What does `enable.idempotence` do and when should you use it?**

Idempotence ensures that retrying a failed produce never creates duplicate messages.
The producer sends a unique sequence number with each message. If the broker receives
the same (producer ID, partition, sequence) twice, it discards the duplicate silently.
Use it whenever duplicates would cause correctness problems — orders, inventory mutations,
financial transactions. The cost is negligible.

**Q: What is the difference between `poll()` and `flush()`?**

`poll()` serves already-pending callbacks — it processes acknowledgements the broker has
already sent. `flush()` blocks until all buffered messages are sent and acknowledged.
In a short-lived script, you call `poll()` first to let pending callbacks fire, then
`flush()` to drain any remaining messages from the buffer. In a long-running service,
the client's background threads handle this automatically.

**Q: How does Kafka decide which partition to route a message to?**

If a key is provided, the producer hashes it (using murmur2 by default) and assigns
the message to `hash(key) % num_partitions`. All messages with the same key always
go to the same partition, preserving ordering for that key. If no key is provided,
the producer uses round-robin or sticky partitioning across available partitions.

**Q: What happens if the flush timeout expires before the buffer is empty?**

`flush()` returns the number of messages still queued. In this package, a non-zero
`flush_remaining_messages` sets `success=False` in the result and logs a warning.
In a short-lived script, those messages are at risk of being lost when the process exits.
In a long-running service, the producer will drain them on the next flush cycle — but
that depends on whether the process stays alive long enough.

**Q: Why do you perform a metadata preflight check before producing?**

Because Kafka's default error behavior on a missing topic is ambiguous — it may
auto-create the topic (if `auto.create.topics.enable=true` on the broker), return a
metadata error, or time out. In a production environment, you want to know explicitly
before sending whether your topic exists. The preflight check converts a 30-second
mystery into a 5-second actionable error. It also gives you broker health information
that is valuable for operational dashboards.

---

## 13. What Comes Next

This package implements the baseline producer pattern. The next patterns in this project
build on top of it:

| Pattern | What it adds | When to use |
|---|---|---|
| **Singleton Producer** (`producers/singleton/`) | One producer instance shared across all threads. | Services that produce from multiple threads simultaneously. |
| **Resilient Producer** (`producers/resilient/`) | Circuit breaker, dead letter queue, structured retry policy. | Services that must degrade gracefully under broker pressure. |
| **Topic Routing Producer** (`producers/topic_routing/`) | Route messages to different topics based on priority or type. | Multi-topic architectures, priority queues. |
| **Callback-Confirmed with Callbacks** (`producers/callback_confirmed/`) | This package — your starting point. | ← You are here |

A good learning sequence:

1. Understand this package completely (run the demo, read every source file).
2. Move to `singleton/` to understand producer lifecycle in multi-threaded services.
3. Move to `resilient/` to understand failure handling at scale.
4. Move to `topic_routing/` to understand message classification and fan-out.
