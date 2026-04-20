# Dead Letter Queue Producer

A fault-tolerant Kafka producer pattern that combines four safety layers — Circuit Breaker,
Bulkhead, Retry with Exponential Backoff, and Dead Letter Queue — to guarantee that messages
are either delivered to the target topic or preserved for replay. Nothing is silently dropped.

This document assumes you have already read the `callback_confirmed` README and understand the
basics of Kafka producers, `produce()`, `poll()`, and `flush()`. Here we go one level deeper:
into what happens when things go wrong, and how to design a producer that degrades gracefully
under broker failures without bringing down the rest of your application.

---

## Table of Contents

1. [What Is a Dead Letter Queue](#1-what-is-a-dead-letter-queue)
2. [Why Four Layers Instead of One](#2-why-four-layers-instead-of-one)
3. [Producer Pattern Hierarchy](#3-producer-pattern-hierarchy)
4. [System Architecture](#4-system-architecture)
5. [Module Structure](#5-module-structure)
6. [The 6-Stage Workflow — Step by Step](#6-the-6-stage-workflow--step-by-step)
7. [The Four Fault-Tolerance Layers in Depth](#7-the-four-fault-tolerance-layers-in-depth)
8. [Configuration Reference (FaultToleranceConfig)](#8-configuration-reference-faulttoleranceconfig)
9. [Data Models (types.py)](#9-data-models-typespy)
10. [Protocol and Adapter Design (clients.py)](#10-protocol-and-adapter-design-clientspy)
11. [Production Decision Framework](#11-production-decision-framework)
12. [Running the Demo](#12-running-the-demo)
13. [Interview Talking Points](#13-interview-talking-points)
14. [What Comes Next](#14-what-comes-next)

---

## 1. What Is a Dead Letter Queue

When a Kafka producer exhausts all retry attempts and still cannot deliver a message, the
default behaviour is to log an error and move on. The message is gone. In a payment service,
an inventory system, or a healthcare record pipeline, "the message is gone" is unacceptable.

A **Dead Letter Queue (DLQ)** is a separate Kafka topic that captures every message that failed
permanent delivery. Instead of being silently dropped, the failed message — along with its
original payload, the error that caused the failure, and enough metadata to replay it — is
written to the DLQ topic for later inspection, reprocessing, or alerting.

```
NORMAL FLOW
──────────────────────────────────────────────────────────────────
  producer.send("payments-high", {transaction: "txn_001"})
                        │
                        │  retry 1 → retry 2 → retry 3
                        │       (all succeed)
                        ▼
               [payments-high topic]


FAILURE FLOW — WITHOUT DLQ
──────────────────────────────────────────────────────────────────
  producer.send("payments-high", {transaction: "txn_001"})
                        │
                        │  retry 1 → retry 2 → retry 3
                        │       (all FAIL — broker down)
                        ▼
                  *** message silently dropped ***
                  no trace, no replay, no alert


FAILURE FLOW — WITH DLQ (this package)
──────────────────────────────────────────────────────────────────
  producer.send("payments-high", {transaction: "txn_001"})
                        │
                        │  retry 1 → retry 2 → retry 3
                        │       (all FAIL — broker down)
                        │
                        ▼
         DLQ send: "payments-dead-letter-queue"
         {
           "original_topic":  "payments-high",
           "original_data":   {"transaction": "txn_001"},
           "error_type":      "KafkaException",
           "error_message":   "Broker: Unknown topic or partition",
           "service_name":    "payments",
           "failed_at_unix":  1745123456.78
         }
         ▼
  An operator replays the DLQ after fixing the broker.
  Zero message loss.
```

**The DLQ topic naming convention** (following Confluent Platform standards):

```
{service-name}-dead-letter-queue

Examples:
  "payments"       → "payments-dead-letter-queue"
  "inventory"      → "inventory-dead-letter-queue"
  "user-events"    → "user-events-dead-letter-queue"
```

---

## 2. Why Four Layers Instead of One

The DLQ alone is not enough. Consider what happens if the broker is down:

- Without a **Circuit Breaker**: every send attempt waits the full retry delay before
  failing. Under load, hundreds of threads pile up inside the retry loop, exhausting your
  application's thread pool. The producer's problem has now cascaded into an application crash.

- Without a **Bulkhead**: even with a circuit breaker, a slow flush can tie up threads.
  The Bulkhead caps the number of threads that can be inside the send path simultaneously.
  Other threads fail fast at the bulkhead gate, leaving resources available for health checks
  and other operations.

- Without **Exponential Backoff**: all retry attempts hit the broker at exactly the same
  interval. When hundreds of instances restart simultaneously (rolling deployment, AZ failover),
  they hammer the recovering broker in synchronized waves. This is the **thundering herd** problem.
  Exponential backoff randomizes the retry timing and lets the broker recover.

- Without a **DLQ**: once the circuit breaker trips and retries are exhausted, the message
  is gone. There is no recovery path for the operator.

Each layer protects against a different failure mode:

```
┌──────────────────────────────────────────────────────────────────┐
│ Layer              │ Failure it prevents                         │
├──────────────────────────────────────────────────────────────────┤
│ Circuit Breaker    │ Cascade: thread pool exhaustion from        │
│                    │ retrying against a known-bad broker         │
├──────────────────────────────────────────────────────────────────┤
│ Bulkhead           │ Resource starvation: a slow broker ties up  │
│                    │ all threads, starving health checks and UI  │
├──────────────────────────────────────────────────────────────────┤
│ Retry + Backoff    │ Transient failures: network blips, broker   │
│                    │ leader elections, momentary GC pauses       │
├──────────────────────────────────────────────────────────────────┤
│ Dead Letter Queue  │ Permanent failures: message is preserved    │
│                    │ for replay instead of being silently dropped│
└──────────────────────────────────────────────────────────────────┘
```

These patterns are not Kafka-specific inventions — they are from distributed systems
literature and used across every major language and platform:

| Pattern | Origin |
|---|---|
| Dead Letter Queue | "Enterprise Integration Patterns" — Hohpe & Woolf (2003) |
| Circuit Breaker | Michael Nygard, "Release It!" (2007, 2nd ed. 2018) |
| Bulkhead | Also Nygard — named after ship hull compartments |
| Exponential Backoff | TCP congestion control literature, AWS best practices |

---

## 3. Producer Pattern Hierarchy

This package builds on top of the `callback_confirmed` pattern. Read the patterns in order —
each one adds one layer of capability.

```
┌─────────────────────────────────────────────────────────────────────┐
│  PATTERN 1 — Fire and Forget                                        │
│  produce() → done. No callback. No confirmation.                    │
│  Risk: message may be lost silently. No way to know.                │
│  Use for: metrics, telemetry, non-critical logging.                 │
├─────────────────────────────────────────────────────────────────────┤
│  PATTERN 2 — Callback-Confirmed                                     │
│  produce() → poll() → flush() → delivery callback fires.            │
│  Guarantees: callback gives exact success/failure per message.      │
│  Use for: orders, payments, any business-critical event.            │
├─────────────────────────────────────────────────────────────────────┤
│  PATTERN 3 — Dead Letter Queue Producer ◄─── THIS PACKAGE          │
│  callback-confirmed + Circuit Breaker + Bulkhead + Retry + DLQ.    │
│  Guarantees: messages are delivered OR preserved for replay.        │
│  Use for: payment processors, healthcare records, audit logs,       │
│  any pipeline where silent message loss is unacceptable.            │
├─────────────────────────────────────────────────────────────────────┤
│  PATTERN 4 — Transactional Producer                                 │
│  Multiple messages committed atomically. All-or-nothing.            │
│  Use for: exactly-once across multiple topics (Kafka Streams,       │
│  Kafka Connect, consume-transform-produce pipelines).               │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 4. System Architecture

### 4.1 High-Level: Where This Producer Fits

```
┌────────────────────────────────────────────────────────────────────────┐
│  APPLICATION LAYER                                                     │
│  (payment service, inventory service, audit log service)               │
│                                                                        │
│           producer.send_with_fault_tolerance("payments-high", data)   │
│                              │                                         │
│                              ▼                                         │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  DEAD LETTER QUEUE PRODUCER  (this package)                     │  │
│  │                                                                 │  │
│  │  Stage 1: Circuit Breaker gate — fast-fail if broker known bad  │  │
│  │  Stage 2: Bulkhead — cap in-flight concurrency                  │  │
│  │  Stage 3: Retry loop — send with exponential backoff            │  │
│  │  Stage 4: Record outcome — update circuit breaker + health      │  │
│  │  Stage 4.1 (on failure): send to DLQ topic                     │  │
│  │  Stage 5: Release bulkhead slot                                 │  │
│  │  Stage 6: Return SendAttemptResult                              │  │
│  └──────────────────────────┬──────────────────────────────────────┘  │
│                             │                                          │
│              ┌──────────────┴────────────────┐                        │
│              │                               │                        │
│              ▼                               ▼                        │
│  ┌───────────────────────┐     ┌───────────────────────────────────┐  │
│  │  TARGET TOPIC         │     │  DLQ TOPIC (on failure)           │  │
│  │  payments-high        │     │  payments-dead-letter-queue       │  │
│  │  (Kafka broker)       │     │  (Kafka broker, same cluster)     │  │
│  └───────────────────────┘     └───────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

### 4.2 The Circuit Breaker State Machine

The circuit breaker is a state machine with three states. Understanding the transitions is
critical for production troubleshooting — the circuit state tells you what the broker was
doing at the time of failure.

```
  ┌──────────────────────────────────────────────────────────────────┐
  │                   Circuit Breaker State Machine                  │
  │                                                                  │
  │                         INITIAL STATE                           │
  │                              │                                  │
  │                              ▼                                  │
  │  ┌──────────────────────────────────────────────────────────┐   │
  │  │  CLOSED  — Normal operation. All sends pass through.     │   │
  │  │                                                          │   │
  │  │  failure_count++ on each error                           │   │
  │  │  failure_count-- (decay) on each success                 │   │
  │  └──────────────────────────────────────────────────────────┘   │
  │         │                                         ▲             │
  │         │  failure_count ≥ failure_threshold      │             │
  │         │  (default: 5 consecutive failures)      │             │
  │         ▼                                         │             │
  │  ┌──────────────────────────────────────────────────────────┐   │
  │  │  OPEN  — Fast-fail mode. All sends rejected immediately. │   │
  │  │                                                          │   │
  │  │  No network calls. DLQ still receives the envelope.      │   │
  │  │  Protects broker from thundering-herd of retries.        │   │
  │  └──────────────────────────────────────────────────────────┘   │
  │         │                                         │             │
  │         │  recovery_timeout_seconds elapses       │ any failure  │
  │         │  (default: 60 seconds)                  │ during probe │
  │         ▼                                         │             │
  │  ┌──────────────────────────────────────────────────────────┐   │
  │  │  HALF_OPEN  — Recovery probe. One send allowed through.  │   │
  │  │                                                          │   │
  │  │  success_count++ on each success                         │   │
  │  │  success_count ≥ success_threshold → CLOSED              │   │
  │  │  (default: 3 consecutive successes to close)             │   │
  │  └──────────────────────────────────────────────────────────┘   │
  │                              │ success_threshold reached         │
  │                              └──────────────────────────────────┘
  │                                       (back to CLOSED)           │
  └──────────────────────────────────────────────────────────────────┘
```

**Why HALF_OPEN matters in production:**

Without a HALF_OPEN state, you would either:
- Never recover automatically (the circuit stays OPEN forever), or
- Recover immediately after the timeout (risking a second wave of failures).

HALF_OPEN is the "probe" — it lets a single request test whether the broker has truly
recovered before reopening the circuit to full traffic. If the probe fails, the circuit
returns to OPEN and the timeout resets.

### 4.3 The Bulkhead — Thread Isolation in Practice

```
  WITHOUT BULKHEAD                      WITH BULKHEAD (max=100)
  ─────────────────────                 ─────────────────────────────
  Thread 1 → send (waiting)            Thread 1 → send (slot 1/100)
  Thread 2 → send (waiting)            Thread 2 → send (slot 2/100)
  Thread 3 → send (waiting)            ...
  ...                                  Thread 100 → send (slot 100/100)
  Thread 500 → send (waiting)          Thread 101 → REJECTED immediately
  Thread 501 → send (waiting)          Thread 102 → REJECTED immediately
  ...                                  ...
  ────────────────────────────         ────────────────────────────────
  All 500 threads blocked.             100 threads working.
  Health check endpoint:               101+ threads: fast-fail result.
    no thread available → timeout      Health check: threads available.
  Application: effectively dead.       Application: still responsive.
```

The bulkhead is named after ship hull compartments that contain flooding. If one compartment
floods (broker is slow), the bulkhead prevents the flooding from spreading to other compartments
(the rest of the application). In Kafka terms: Kafka slowness cannot sink your API server.

---

## 5. Module Structure

Every file in this package has a single responsibility. The structure mirrors
`producers/callback_confirmed/` for consistency.

```
producers/dead_letter_queue/
├── __init__.py          ← Public API surface. What callers import from.
├── constants.py         ← DLQ topic suffix and HA preset values.
├── types.py             ← CircuitState, FaultToleranceConfig, SendAttemptResult, Protocols.
├── clients.py           ← Factory adapters for SingletonProducer and TopicRoutingProducer.
├── core.py              ← CircuitBreaker, Bulkhead, SlidingWindowHealthMonitor,
│                           DeadLetterQueueProducer. All logic lives here.
└── demo.py              ← Annotated demo showing the pattern in action.
```

### Why separate these files?

| File | Changes when... |
|---|---|
| `constants.py` | DLQ topic naming convention changes, HA presets need tuning. |
| `types.py` | SendAttemptResult fields change, new Protocol contracts needed. |
| `clients.py` | Underlying producer or routing producer implementations change. |
| `core.py` | Circuit breaker logic, retry strategy, or DLQ envelope schema changes. |
| `demo.py` | Demo flow or educational content changes. |

**The critical separation: `clients.py` vs `core.py`**

`core.py` never imports `confluent_kafka` directly. It receives `UnderlyingProducerProtocol`
and `RoutingProducerProtocol` via injection (from `clients.py` or from tests). This means:

- Tests can inject fake producers without starting a real Kafka broker.
- The circuit breaker, bulkhead, and retry logic can be tested in isolation.
- Switching from `confluent_kafka` to another Kafka client only touches `clients.py`.

---

## 6. The 6-Stage Workflow — Step by Step

Follow the stages in [`core.py`](core.py) as you read. Every `# Stage N.M —` comment
in the source maps directly to a box in this diagram.

```
  INPUT: topic, data, routing_metadata (optional)
       │
       ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Stage 1.0  CIRCUIT BREAKER GATE                         │
  │                                                          │
  │  Is circuit OPEN?                                        │
  │    YES → return SendAttemptResult(success=False,         │
  │           error="Circuit breaker OPEN — broker           │
  │           flagged unavailable")                          │
  │    NO (CLOSED or HALF_OPEN) → continue                   │
  │                                                          │
  │  Why first: no point touching the network if we already  │
  │  know the broker is down. Fast-fail saves thread time    │
  │  and protects the recovering broker from load.           │
  └───────────────────────┬──────────────────────────────────┘
                          │ ✓ circuit allows execution
                          ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Stage 2.0  BULKHEAD SLOT ACQUISITION                    │
  │                                                          │
  │  Semaphore.acquire(timeout=send_timeout_seconds)         │
  │                                                          │
  │  Slot available? → continue                              │
  │  Full or timeout? → return SendAttemptResult(            │
  │    success=False, error="Bulkhead capacity exhausted")   │
  │                                                          │
  │  Why second: limits concurrency BEFORE retrying, so that │
  │  a broker slowdown cannot exhaust all application threads│
  └───────────────────────┬──────────────────────────────────┘
                          │ ✓ bulkhead slot acquired
                          ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Stage 3.0  RETRY LOOP (up to max_retries+1 attempts)    │
  │                                                          │
  │  ┌──────────────────────────────────────────────────┐    │
  │  │  Stage 3.1  Invoke routing producer.             │    │
  │  │    If routing_metadata set → send_with_metadata  │    │
  │  │    Else                    → send_to_topic       │    │
  │  │                                                  │    │
  │  │  Stage 3.2  Success → exit loop immediately.     │    │
  │  │                                                  │    │
  │  │  Stage 3.3  Failure → log warning, sleep(delay). │    │
  │  │    delay = min(initial * multiplier^n, max_delay)│    │
  │  │    attempt < max_retries → retry                 │    │
  │  │    attempt == max_retries → log error, exit loop │    │
  │  └──────────────────────────────────────────────────┘    │
  └───────────────────────┬──────────────────────────────────┘
                          │ ✓ loop exited (success or exhausted)
                          ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Stage 4.0  RECORD OUTCOME                               │
  │                                                          │
  │  success → circuit_breaker.record_success()              │
  │    CLOSED: decay failure_count by 1                      │
  │    HALF_OPEN: increment success_count;                   │
  │              close circuit if threshold met              │
  │                                                          │
  │  failure → circuit_breaker.record_failure()              │
  │    CLOSED: increment failure_count;                      │
  │            open circuit if threshold met                 │
  │    HALF_OPEN: reopen circuit immediately                 │
  │                                                          │
  │  In both cases: health_monitor.record_operation(...)     │
  │                                                          │
  │  Stage 4.1  (failure only, if enable_dlq=True)           │
  │  DLQ SEND: write envelope to DLQ topic via               │
  │  underlying_producer (not routing_producer, to avoid     │
  │  infinite retry loop on the routing layer).              │
  └───────────────────────┬──────────────────────────────────┘
                          │ ✓ outcome recorded
                          ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Stage 5.0  RELEASE BULKHEAD SLOT                        │
  │                                                          │
  │  bulkhead.release()  — always, via finally block.        │
  │  Why finally: an unexpected exception in Stage 4         │
  │  must not leak the semaphore slot permanently.           │
  └───────────────────────┬──────────────────────────────────┘
                          │ ✓ slot released
                          ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Stage 6.0  RETURN SendAttemptResult                     │
  │                                                          │
  │  Fields: success, error, execution_time_seconds,         │
  │          retry_count, circuit_state.                     │
  │                                                          │
  │  Callers inspect the result object — no exception        │
  │  parsing required. success=False + circuit_state=OPEN    │
  │  tells you instantly what happened and why.              │
  └──────────────────────────────────────────────────────────┘
```

### Stage 4.1 in depth: Why DLQ sends through the underlying producer

There are two producer objects in play:

- `routing_producer` — the TopicRoutingProducer. This is the one that selected the topic
  and attempted delivery. **It is the one that just failed.** Sending the DLQ envelope through
  the same routing producer risks an infinite failure loop: the routing producer fails → DLQ
  send through routing producer also fails → tries to send to DLQ again → infinite loop.

- `underlying_producer` — the SingletonProducer. This writes directly to the named DLQ topic,
  bypassing all routing rules. It is a different code path and is much less likely to fail for
  the same reason the routing producer failed.

This is a subtle but critical design decision. **The DLQ send path must be independent of
the normal send path.**

### Stage 3.3 in depth: Exponential Backoff Formula

```
attempt 0: send (no delay before first try)
attempt 1: delay = 1.0 * 2.0^1 = 2.0 seconds
attempt 2: delay = 1.0 * 2.0^2 = 4.0 seconds
attempt 3: delay = min(1.0 * 2.0^3, max_delay) = min(8.0, 30.0) = 8.0 seconds

General formula:
  delay_n = min(initial_retry_delay_seconds * retry_backoff_multiplier^n, max_retry_delay_seconds)
```

The cap (`max_retry_delay_seconds`) prevents the delay from growing to hours in long retry chains.
The exponential growth means the producer is increasingly patient on successive failures,
giving the broker progressively more recovery time.

---

## 7. The Four Fault-Tolerance Layers in Depth

### 7.1 Circuit Breaker

**What triggers OPEN state?**

Five consecutive failures (default `failure_threshold=5`). Each `record_failure()` increments
a counter. When the counter exceeds the threshold, the state transitions to OPEN.

**What counts as a failure?**

Any exception raised by the routing producer — `confluent_kafka.KafkaException`, connection
timeouts, unknown topic errors, broker unavailable errors, and any other exception that
escapes the retry loop.

**Why does `can_execute()` trigger the HALF_OPEN transition?**

There is no background watchdog thread. The circuit is lazy — it only checks the timeout
when a send is attempted. This is by design: it avoids a separate thread that needs lifecycle
management. The first send attempt after `recovery_timeout_seconds` elapses acts as the probe.

**Production tip:** If you need the circuit to recover faster after a broker is manually repaired,
call `producer.reset_circuit_breaker()`. This is the administrative reset path that forces
the circuit back to CLOSED without waiting for the timeout.

### 7.2 Bulkhead

**How many slots are reasonable for production?**

```
Low-traffic microservice       : max_concurrent_sends=50
Standard backend service       : max_concurrent_sends=100 (default)
High-throughput event pipeline : max_concurrent_sends=500
```

Set it to roughly 2× the number of threads you expect to be actively producing simultaneously.
Too low → legitimate traffic is rejected. Too high → the bulkhead provides no real protection.

**What happens to rejected requests?**

They receive a `SendAttemptResult(success=False, error="Bulkhead capacity exhausted")` immediately.
They do NOT go to the DLQ — the DLQ is for messages that were attempted and failed, not for
messages that were rejected before being attempted.

**When to disable the bulkhead:**

Never disable it in production. In tests, inject a fake producer that always succeeds
instantly — the bulkhead will never fill up and will be invisible to the test.

### 7.3 Retry with Exponential Backoff

**Why not just use Kafka's built-in retries (`retries` config key)?**

Kafka's built-in retries operate at the broker-connection level — they retry individual
`produce()` calls. This pattern's retry loop operates at the application level — it retries
the entire send operation including topic routing decisions.

The two layers are complementary:
- Kafka retries handle broker-level transient errors (leader elections, network glitches).
- Application-level retries handle routing failures, serialization errors, and errors
  that the Kafka client itself propagates up to application code.

**Interview point:** Kafka's internal retries also use exponential backoff (`retry.backoff.ms`).
This package adds a second backoff layer at the application level. Make sure the total
retry budget of both layers doesn't exceed your caller's SLA timeout.

### 7.4 Dead Letter Queue

**What is in the DLQ envelope?**

```python
{
    "original_topic":   "payments-high",           # Where the message was going
    "original_data":    {"transaction": "txn_001"}, # The original payload, unchanged
    "error_type":       "KafkaException",           # Python exception class name
    "error_message":    "Broker: Request timed out",# Human-readable failure reason
    "routing_metadata": {"priority": "high", ...}, # Routing context if present
    "service_name":     "payments",                 # Which service failed to deliver
    "failed_at_unix":   1745123456.78               # When the failure occurred
}
```

**How do you replay from the DLQ?**

1. Write a consumer that reads from `{service}-dead-letter-queue`.
2. For each message, extract `original_topic` and `original_data`.
3. Re-submit to the original topic using a fresh producer.
4. Track which DLQ offsets have been replayed (commit consumer offset after replay).

This is a manual operation — DLQ replay requires human judgement about whether the
underlying cause has been fixed. Never automate DLQ replay without understanding why
the messages failed in the first place.

**What if the DLQ send also fails?**

The DLQ send failure is logged at CRITICAL level and NOT re-raised. This is intentional:

- Raising would replace the original error context.
- It could cause an infinite exception chain.
- The CRITICAL log is your signal to alert on-call.

Monitor your CRITICAL log lines in production. A DLQ send failure means potential message
loss, which is the worst-case outcome this pattern was designed to prevent.

---

## 8. Configuration Reference (FaultToleranceConfig)

All tuneable parameters for the four fault-tolerance layers live in a single `FaultToleranceConfig`
dataclass (defined in [`types.py`](types.py)). Every field has a documented rationale.

### 8.1 Circuit Breaker Parameters

#### `failure_threshold`

```
Default: 5
Range:   1 – ∞ (practical: 2–10)
```

How many consecutive failures trip the circuit from CLOSED to OPEN. Lower values detect
outages faster but are noisier on flaky networks.

```
failure_threshold=2 → trips after 2 failures (HA services, strict SLAs)
failure_threshold=5 → trips after 5 failures (default, most services)
failure_threshold=10 → trips after 10 failures (very flaky networks)
```

#### `recovery_timeout_seconds`

```
Default: 60
Range:   10 – 300 (practical)
```

How long the circuit stays OPEN before probing recovery via HALF_OPEN. Trade-off: too short
means premature retries against a still-recovering broker; too long means extended downtime.

#### `success_threshold`

```
Default: 3
Range:   1 – 10 (practical)
```

How many consecutive successes in HALF_OPEN are required to close the circuit. Setting this
to 1 recovers faster but risks reopening on a lucky single success during a partial recovery.

### 8.2 Retry Parameters

#### `max_retries`

```
Default: 3
Range:   0 – 10 (practical)
```

Attempts after the initial failure before routing to DLQ. Setting to 0 means: try once,
fail immediately to DLQ. Setting high means: patient with transient failures, higher latency.

#### `initial_retry_delay_seconds`

```
Default: 1.0
Range:   0.1 – 10.0 (practical)
```

Starting delay before first retry. Multiplied by `retry_backoff_multiplier` on each attempt.
Too low: hammers a restarting broker. Too high: caller waits a long time on the first retry.

#### `max_retry_delay_seconds`

```
Default: 30.0
Range:   5.0 – 120.0 (practical)
```

Cap on inter-retry delay. Prevents the retry from stalling for minutes on high retry counts.
Always set this to less than your caller's SLA timeout.

#### `retry_backoff_multiplier`

```
Default: 2.0 (doubles each attempt)
Range:   1.5 – 3.0 (practical)
```

The exponential growth factor. `2.0` (doubling) is the standard choice.
Values less than 1.5 approach linear backoff; values above 3.0 grow too fast.

### 8.3 Bulkhead Parameters

#### `max_concurrent_sends`

```
Default: 100
Range:   10 – 5000 (practical)
```

Maximum number of in-flight send operations allowed simultaneously.

```
Low-traffic service:  50
Standard service:     100 (default)
High-throughput:      500–1000
```

#### `send_timeout_seconds`

```
Default: 30.0
Range:   1.0 – 120.0 (practical)
```

Maximum time a thread waits to acquire a bulkhead slot. Must be shorter than the caller's
own SLA timeout so the caller can react to the failure before its deadline expires.

### 8.4 Health Monitor Parameters

#### `health_window_size`

```
Default: 100
Range:   10 – 10000 (practical)
```

Number of recent operations in the sliding window for error-rate computation. A window of
100 means the error rate reflects the last 100 operations, regardless of how long ago they
happened.

Implementation note for production monitoring: health snapshots should compute all window metrics
inside one lock scope. Re-entering the same non-reentrant lock through nested property calls can
deadlock monitoring endpoints during incidents, exactly when observability is most needed.

#### `error_rate_unhealthy_threshold`

```
Default: 0.10 (10%)
Range:   0.01 – 0.50 (practical)
```

Error rate above which `is_healthy` returns False. Use this threshold for alerting or
auto-scaling decisions. A 10% error rate means 1 in 10 messages is failing — this is
a signal worth paging someone for in a payments service.

### 8.5 Configuration Summary Table

| Parameter | Default | HA Preset | Conservative | Aggressive |
|---|---|---|---|---|
| `failure_threshold` | 5 | 2 | 10 | 2 |
| `recovery_timeout_seconds` | 60 | 30 | 120 | 10 |
| `success_threshold` | 3 | 3 | 5 | 1 |
| `max_retries` | 3 | 5 | 5 | 1 |
| `initial_retry_delay_seconds` | 1.0 | 1.0 | 2.0 | 0.5 |
| `max_retry_delay_seconds` | 30.0 | 30.0 | 60.0 | 5.0 |
| `max_concurrent_sends` | 100 | 200 | 50 | 500 |
| `send_timeout_seconds` | 30.0 | 30.0 | 60.0 | 5.0 |
| `error_rate_unhealthy_threshold` | 0.10 | 0.05 | 0.05 | 0.20 |

Use `create_dlq_producer()` for default settings. Use `create_ha_dlq_producer()` for the HA preset.
For anything custom, instantiate `FaultToleranceConfig(...)` directly.

For production safety, always pass explicit connection context when possible:
- `environment=...` for environment-driven config selection.
- `bootstrap_servers=...` for explicit endpoint pinning in demos, tests, and canaries.

If this context is implicit or hardcoded, a local run can accidentally publish to the wrong cluster.

---

## 9. Data Models (types.py)

### 9.1 `CircuitState`

An `Enum` with three values: `CLOSED`, `OPEN`, `HALF_OPEN`. Using an Enum instead of string
constants prevents typos and enables exhaustive match checking in type checkers.

```python
from producers.dead_letter_queue import CircuitState

result = producer.send_with_fault_tolerance(topic, data)
if result.circuit_state is CircuitState.OPEN:
    # broker is flagged unavailable — alert on-call
    alert("Circuit breaker OPEN for payments service")
```

**In production monitoring:** Log `result.circuit_state.value` in your observability pipeline.
An unexpected spike in `OPEN` state is a leading indicator of broker trouble — often visible
before the monitoring dashboards catch it.

### 9.2 `FaultToleranceConfig`

A mutable `@dataclass` (not frozen — allows `set_dlq_enabled()` to modify the producer at
runtime). All fields have documented defaults with production tuning rationale. See
Section 8 for the full reference.

```python
from producers.dead_letter_queue import FaultToleranceConfig

# Custom config for a high-frequency, lower-criticality analytics pipeline
analytics_config = FaultToleranceConfig(
    failure_threshold=10,       # more tolerant of flaky network
    max_retries=2,              # fewer retries — latency matters
    max_concurrent_sends=500,   # high throughput
    error_rate_unhealthy_threshold=0.20,  # 20% — alert threshold
)
```

### 9.3 `SendAttemptResult`

The structured outcome of every `send_with_fault_tolerance()` call.

```python
@dataclass
class SendAttemptResult:
    success: bool                # True only if send reached the broker.
    error: Optional[Exception]   # Last exception across all retry attempts.
    execution_time_seconds: float # Total wall-clock time (incl. retry delays).
    retry_count: int             # How many retries were used (0 = first try worked).
    circuit_state: CircuitState  # Circuit state AFTER this attempt was recorded.
```

**Why a result object instead of raising exceptions?**

Compare these two caller patterns:

```python
# Exception-based (hard to handle gracefully):
try:
    producer.send(topic, data)
except KafkaException as e:
    # Was it a timeout? A broker error? A circuit breaker trip?
    # You have to parse the exception message to find out.
    logger.error("Send failed: %s", e)
except BulkheadFullException as e:
    logger.warning("Bulkhead full: %s", e)
except CircuitOpenException as e:
    logger.warning("Circuit open: %s", e)
# ... 4 more exception types

# Result-based (explicit, testable):
result = producer.send_with_fault_tolerance(topic, data)
if result.success:
    metrics.increment("payments.sent", tags={"retries": result.retry_count})
elif result.circuit_state is CircuitState.OPEN:
    alert("Circuit breaker OPEN — broker unavailable")
else:
    logger.error("Send failed after %d retries: %s", result.retry_count, result.error)
```

The result object makes the caller's logic explicit and each field independently testable.

### 9.4 `UnderlyingProducerProtocol` and `RoutingProducerProtocol`

These are Python `Protocol` types (structural interfaces) that define the minimum contract
required from the injected dependencies.

```python
class UnderlyingProducerProtocol(Protocol):
    def send(self, topic: str, data: Dict[str, Any], **kwargs: Any) -> None: ...
    def flush(self, timeout: float) -> int: ...

class RoutingProducerProtocol(Protocol):
    def send_to_topic(self, topic: str, data: Dict[str, Any], **kwargs: Any) -> None: ...
    def send_with_metadata(self, data: Dict[str, Any], metadata: Any, **kwargs: Any) -> None: ...
```

**What this enables in tests:**

```python
class FakeProducer:
    """Test double — records all send calls for assertion."""
    def __init__(self):
        self.sent = []

    def send(self, topic, data, **kwargs):
        self.sent.append({"topic": topic, "data": data})

    def flush(self, timeout):
        return 0  # always succeeds instantly

class FakeRoutingProducer:
    def send_to_topic(self, topic, data, **kwargs):
        pass

    def send_with_metadata(self, data, metadata, **kwargs):
        pass

# Test the circuit breaker logic without any real Kafka:
fake = FakeProducer()
fake_router = FakeRoutingProducer()
producer = DeadLetterQueueProducer(
    "payments",
    config=FaultToleranceConfig(failure_threshold=2),
    underlying_producer=fake,
    routing_producer=fake_router,
)
```

---

## 10. Protocol and Adapter Design (clients.py)

### 10.1 Why Two Producers?

This pattern uses two different producer objects for two different purposes:

| Producer | Type | Used for | Why separate? |
|---|---|---|---|
| `underlying_producer` | `SingletonProducer` | DLQ writes, raw topic sends | Bypass routing on failure; avoid infinite loop |
| `routing_producer` | `TopicRoutingProducer` | All normal sends | Priority-based topic selection without coupling |

The routing producer wraps the underlying producer internally — both share the same
`confluent_kafka.Producer` connection. Injecting both as separate objects lets you
swap either one independently in tests.

### 10.2 The Singleton Connection

`clients.py` resolves the underlying producer via `SingletonProducer.get_instance()`.
This ensures that all `DeadLetterQueueProducer` instances within the same process share
one `confluent_kafka.Producer` connection — one set of I/O threads, one connection pool,
one internal send buffer.

**Interview point:** Creating a new `confluent_kafka.Producer` on every request is a common
mistake. Each `Producer` object starts its own background I/O threads and establishes its
own broker connections. In a service that handles 1,000 requests/second, that would be 1,000
separate producer connections to the broker cluster — orders of magnitude more connections
than necessary.

---

## 11. Production Decision Framework

### Step 1: Is a DLQ Producer the right choice?

Use the DLQ producer when the answer to any of these is YES:

- "If this message is lost, does money move incorrectly?" (payments)
- "If this message is lost, does a patient miss a dose?" (healthcare)
- "If this message is lost, does our audit trail have a gap?" (compliance)
- "If this message is lost, does inventory show the wrong count?" (inventory)

Use a simpler producer (callback-confirmed) when:

- The message is analytics/telemetry and occasional loss is acceptable.
- The consumer can re-derive the data from another source.
- The produce rate is so high that DLQ overhead would itself become a bottleneck.

### Step 2: Configure the circuit breaker for your recovery time objective

```
RTO < 30 seconds:  failure_threshold=2, recovery_timeout_seconds=15
RTO 1–5 minutes:   failure_threshold=5, recovery_timeout_seconds=60  (default)
RTO > 5 minutes:   failure_threshold=10, recovery_timeout_seconds=120
```

Your `recovery_timeout_seconds` should be shorter than your RTO so the circuit recovers
automatically within the objective window.

### Step 3: Set retry budget proportionate to your SLA

```
Caller SLA = 5 seconds:
  max_retries=2, initial_retry_delay=0.5, max_retry_delay=2.0
  Worst case: 0.5 + 1.0 + 2.0 = 3.5 seconds of retries → fits in 5s

Caller SLA = 30 seconds:
  max_retries=4, initial_retry_delay=1.0, max_retry_delay=10.0
  Worst case: 1 + 2 + 4 + 8 + 10 = 25 seconds → fits in 30s
```

Always verify: `sum of all retry delays < caller SLA - network overhead`.

### Step 4: Monitor the circuit state

```python
health = producer.health_status()

# These signals warrant a PagerDuty alert:
if health["circuit_breaker"]["state"] == "open":
    alert("CRITICAL: Payment producer circuit open")

if health["health_monitor"]["window_error_rate"] > 0.05:
    alert("WARNING: Payment producer error rate above 5%")

if health["bulkhead"]["active_count"] > 80:
    alert("WARNING: Bulkhead filling up (>80% capacity)")
```

### Step 5: Build a DLQ replay pipeline

Every service that uses the DLQ producer needs a replay plan:

1. **Detection:** Alert when the DLQ topic has unconsumed messages (consumer lag > 0).
2. **Triage:** Inspect DLQ envelopes — are all failures the same error type and topic?
3. **Fix:** Resolve the root cause (broker config, topic ACL, message schema issue).
4. **Replay:** Consume from DLQ, re-submit `original_data` to `original_topic`.
5. **Verify:** Confirm consumer lag on the original topic drops after replay.

Never replay from DLQ without first fixing the root cause. Replaying into a broken pipeline
just re-populates the DLQ.

---

## 12. Running the Demo

### Prerequisites

Start the local Kafka infrastructure first (from the project root):

```bash
python infrastructure/scripts/kafka_infrastructure.py up
python infrastructure/scripts/kafka_infrastructure.py health
```

Kafka UI will be available at [http://localhost:8080](http://localhost:8080). You should see
the broker is healthy and topics are accessible before running the demo.

### Run the DLQ producer demo

From the project root:

```bash
python -m producers.dead_letter_queue.demo
```

What happens:

1. Logs the full four-layer pattern diagram to show where you are in the architecture.
2. Builds a `DeadLetterQueueProducer` with short retry delays (demo-friendly).
   You can pin endpoints explicitly via `run_demo(bootstrap_servers=\"localhost:19094\")`
   to prevent accidental cluster targeting in multi-environment setups.
3. Logs the initial circuit state and health snapshot (everything CLOSED and healthy).
4. Sends a `demo-service-events` message through all four layers.
5. Logs the `SendAttemptResult` — success, retry count, elapsed time, circuit state.
6. Logs the post-send health snapshot to show the sliding window updated.
7. Explains every `FaultToleranceConfig` field so you understand each knob.

### Expected output (healthy broker)

```
INFO  ============================================================
INFO  Dead Letter Queue Producer — pattern walkthrough
INFO  ...pattern diagram...
INFO  Stage 1.0 — Building DeadLetterQueueProducer...
INFO  CircuitBreaker initialised — failure_threshold=3, recovery_timeout=10s
INFO  Bulkhead initialised — max_concurrent_sends=100
INFO  DeadLetterQueueProducer ready — service=demo-service, dlq_enabled=True
INFO  Stage 2.0 — Initial health status:
      { "circuit_breaker": {"state": "closed", "failure_count": 0}, ... }
INFO  Stage 3.0 — Sending message to topic 'demo-service-events'...
INFO  Message delivered to demo-service-events [0] @ offset 3
INFO  Stage 3.1 ✓ Message delivered. retry_count=0, elapsed=0.045s, circuit=closed
INFO  Stage 4.0 — Post-send health status:
      { "health_monitor": {"window_error_rate": 0.0, "is_healthy": true, ...}, ... }
INFO  Stage 5.0 — Configuration explanation:
      failure_threshold   Trips the circuit after 3 consecutive failures...
      ...
```

### Expected output (broker unreachable)

```
INFO  Stage 3.0 — Sending message to topic 'demo-service-events'...
WARN  Send attempt 1/3 failed for service=demo-service — retrying in 0.50s. Error: ...
WARN  Send attempt 2/3 failed for service=demo-service — retrying in 1.00s. Error: ...
ERROR All 3 send attempts exhausted for service=demo-service.
WARN  CircuitBreaker → OPEN (failure_threshold=3 exceeded)
INFO  DLQ preserved — service=demo-service, dlq_topic=demo-service-dead-letter-queue
WARN  Stage 3.1 ✗ Message delivery failed after 2 retries (3.241s elapsed).
      Message was routed to DLQ: demo-service-dead-letter-queue
      Circuit state after failure: open
      Error: <KafkaException: ...>
```

The DLQ topic `demo-service-dead-letter-queue` will contain the full failure envelope
visible in Kafka UI under Topics → demo-service-dead-letter-queue → Messages.

### Run with High Availability preset

```python
from producers.dead_letter_queue import create_ha_dlq_producer

producer = create_ha_dlq_producer("payments")
result = producer.send_with_fault_tolerance(
    topic="payments-high",
    data={"transaction_id": "txn_001", "amount_cents": 9900},
)
```

---

## 13. Interview Talking Points

These are the questions interviewers ask most often about fault-tolerant Kafka producers,
and how to answer them using concepts from this package.

**Q: What is a Dead Letter Queue in Kafka and why do you need one?**

A DLQ is a separate Kafka topic that captures messages that could not be delivered after
exhausting all retry attempts. Without a DLQ, a message that fails all retries is silently
dropped — there is no recovery path. With a DLQ, the message is preserved with its full
payload, error context, and timestamp. An operator can fix the root cause and replay the
DLQ once the system is healthy. DLQ is a pattern from "Enterprise Integration Patterns"
(Hohpe & Woolf) and is now a first-class concept in Confluent Platform and Kafka Connect.

**Q: What is a circuit breaker and how does it apply to Kafka producers?**

A circuit breaker is a fault-tolerance pattern (from Nygard's "Release It!") that prevents
a caller from repeatedly calling a service that is known to be failing. In Kafka, a circuit
breaker sits in front of the producer's send path. When consecutive failures exceed a
threshold, the circuit opens and all subsequent send attempts fail immediately without
touching the network. This protects the recovering broker from being flooded with retries
and prevents thread pool exhaustion in the application. After a configurable timeout, the
circuit enters HALF_OPEN to probe whether the broker has recovered.

**Q: What is the thundering herd problem and how does exponential backoff solve it?**

The thundering herd problem occurs when many clients (producers, consumers, or services)
all retry at the same fixed interval after a failure. If 500 service instances all retry
every 1 second, they simultaneously hammer the recovering broker in synchronized waves,
potentially re-triggering the failure. Exponential backoff doubles the retry delay on each
attempt (`delay_n = initial * 2^n`). This spreads retry traffic over time — early retries
are frequent (to catch fast recoveries), later retries are infrequent (to give the broker
space). Combined with jitter (random offset added to the delay), it breaks the synchronization
across instances.

**Q: What is the Bulkhead pattern and when would you use it in a Kafka producer?**

The Bulkhead pattern limits concurrency to isolate resource usage between components. In a
Kafka producer context, the bulkhead is a semaphore that caps the number of threads allowed
into the send path simultaneously. Without a bulkhead, a slow broker can cause send threads
to accumulate until the application's thread pool is exhausted — and then health check
endpoints, API handlers, and other operations also fail because there are no threads left.
The bulkhead ensures that Kafka slowness cannot exhaust more than `max_concurrent_sends`
threads, leaving the rest of the application responsive.

**Q: What is the difference between Kafka's built-in `retries` config and application-level retry?**

Kafka's `retries` config controls broker-level retries inside the `confluent_kafka.Producer`
client — these handle leader elections, network glitches, and broker-level transient errors.
Application-level retry (like in this pattern) wraps the entire `produce()` call and handles
errors that the Kafka client propagates up to application code. The two layers are
complementary: Kafka's internal retries handle low-level broker communication; application
retries handle higher-level failures including routing decisions and serialization errors.
The important constraint is that the total retry time budget of both layers must not exceed
the caller's SLA timeout.

**Q: Why does the DLQ send bypass the routing producer?**

Because the routing producer is the component that just exhausted all retries and failed.
Sending the DLQ envelope through the same routing producer risks an infinite retry loop:
routing fails → DLQ send through routing fails → DLQ send retried → routing fails again.
The DLQ send goes directly through the `underlying_producer` (the `SingletonProducer`)
which writes directly to the named DLQ topic without any routing rules or additional retry
logic. The DLQ send path must be independent of the normal send path.

**Q: How do you monitor a circuit breaker in production?**

Track the circuit state as a time-series metric. The key signals are:
- `circuit_state == OPEN`: broker is unavailable — alert on-call immediately.
- `circuit_state == HALF_OPEN`: broker is recovering — watch for transition back to CLOSED.
- Frequency of CLOSED→OPEN transitions per hour: a high rate indicates chronic broker instability.
- `window_error_rate > threshold`: leading indicator before the circuit opens.
- `bulkhead.active_count / max_concurrent_sends`: bulkhead saturation approaching 100% is a warning.

---

## 14. What Comes Next

The DLQ producer is the third pattern in this project. Here is the full learning sequence:

| Pattern | Location | What it adds | When to use |
|---|---|---|---|
| **Callback-Confirmed** | `producers/callback_confirmed/` | Structured produce with delivery proof | Starting point for all producers |
| **Singleton Producer** | `producers/singleton/` | Shared connection across threads | Multi-threaded services |
| **Topic Routing** | `producers/topic_routing/` | Priority-based topic selection | Multi-topic architectures |
| **Dead Letter Queue** | `producers/dead_letter_queue/` | ← **You are here** | SLA-critical pipelines |
| **Transactional** | *(next pattern)* | Exactly-once across topics | Consume-transform-produce |

A good learning sequence after this:

1. Add a consumer that reads from a DLQ topic and replays to the original topic.
2. Add metrics export (Prometheus/Datadog) from `health_status()` output.
3. Explore Kafka Streams' built-in DLQ support in `DeadLetterPublishingRecoverer`.
4. Study transactional producers for exactly-once semantics across multiple topics.
