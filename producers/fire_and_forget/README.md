# Fire-and-Forget Producer

The simplest Kafka producer delivery mode. `produce()` enqueues the message locally
and returns immediately — the broker acknowledgement happens in the background on
librdkafka's internal thread. The caller never waits for broker I/O.

This document assumes you have already read the `callback_confirmed` README and
understand how `produce()`, `poll()`, and `flush()` work. Here we focus on *why*
fire-and-forget exists, when it is the right choice, and how to manage a shared
producer instance across the full application lifecycle.

---

## Table of Contents

1. [What Is Fire-and-Forget](#1-what-is-fire-and-forget)
2. [The Three Kafka Delivery Modes](#2-the-three-kafka-delivery-modes)
3. [Why a Shared (Singleton) Instance](#3-why-a-shared-singleton-instance)
4. [Architecture — Two Threads, One Producer](#4-architecture--two-threads-one-producer)
5. [Module Structure](#5-module-structure)
6. [The Send Flow — Stage by Stage](#6-the-send-flow--stage-by-stage)
7. [Optional Confirmation Mode](#7-optional-confirmation-mode)
8. [Retry Logic](#8-retry-logic)
9. [Health Monitoring](#9-health-monitoring)
10. [Configuration Reference](#10-configuration-reference)
11. [Running the Demo](#11-running-the-demo)
12. [Interview Talking Points](#12-interview-talking-points)
13. [What Comes Next](#13-what-comes-next)

---

## 1. What Is Fire-and-Forget

Fire-and-forget is a Kafka delivery mode where the producer enqueues a message
in its local buffer and immediately moves on. No blocking, no waiting for the
broker to reply.

```
Application Thread
──────────────────────────────────────────────────────────────────
  producer.send("page-views", {"user_id": 42})
                      │
                      │  produce() returns in microseconds
                      ▼
              [librdkafka internal buffer]
                      │
                      │  async network I/O (background thread)
                      ▼
              [Kafka Broker]   ← ack arrives later (or never)
                      │
                      │  delivery callback fires via poll()
                      ▼
              health metrics updated, offset logged
```

The application thread never blocks on broker I/O. The delivery callback
fires when poll() is called — either the `poll(0)` call immediately after
produce(), or the next scheduled poll cycle.

**Trade-off:** maximum throughput and minimum producer-side latency, but
message loss is possible if the broker is unreachable when the message
expires from the buffer.

---

## 2. The Three Kafka Delivery Modes

| Mode | Waits for broker ack? | Message loss risk | Throughput |
|---|---|---|---|
| **Fire-and-forget** | No | Yes — possible | Highest |
| **Callback-confirmed** | Yes (async callback) | Low (broker ack required) | High |
| **Synchronous send** | Yes (blocks caller) | Lowest | Lowest |

This module implements fire-and-forget as the default. Confirmation mode
(blocking until the ack arrives) is available via `require_delivery_confirmation=True`
but turns every call into a synchronous send — use sparingly.

---

## 3. Why a Shared (Singleton) Instance

Confluent's own documentation states:

> *"It is generally advisable to create the producer at application startup and
> keep it alive for the lifetime of the application."*

**What happens if you create a producer per request:**

```
Request 1 → new Producer → TCP connect → produce → disconnect
Request 2 → new Producer → TCP connect → produce → disconnect
Request 3 → new Producer → TCP connect → produce → disconnect
```

Each `Producer()` call:
- Opens TCP connections to every broker in the cluster
- Performs metadata negotiation
- Allocates internal buffers (32 MB by default)
- Starts background threads

For a service handling 1,000 requests/second this creates 1,000 broker
connections per second — most Kafka clusters have a connection limit.

**With a shared instance:**

```
Startup → one Producer → TCP connect (once, per broker)
Request 1 ──┐
Request 2 ──┤──→ shared producer.produce() → [internal batching] → one TCP write
Request 3 ──┘
```

One connection pool, one background thread, full batching across all callers.

---

## 4. Architecture — Two Threads, One Producer

```
┌─────────────────────────────────────────────────────────────────────┐
│  Application Process                                                │
│                                                                     │
│  ┌──────────────────┐    produce()     ┌─────────────────────────┐ │
│  │  Thread A        │ ────────────────▶│                         │ │
│  │  (any app code)  │                  │  FireAndForgetProducer  │ │
│  └──────────────────┘                  │  (shared instance)      │ │
│                                        │                         │ │
│  ┌──────────────────┐    produce()     │  KafkaProducerLike      │ │
│  │  Thread B        │ ────────────────▶│  (confluent_kafka or    │ │
│  │  (any app code)  │                  │   kafka-python adapter) │ │
│  └──────────────────┘                  │                         │ │
│                                        └──────────┬──────────────┘ │
│                                                   │                │
│                   ┌───────────────────────────────▼──────────────┐ │
│                   │  librdkafka internal queue + I/O thread      │ │
│                   │  (batches messages, manages TCP connections)  │ │
│                   └───────────────────────────────┬──────────────┘ │
└───────────────────────────────────────────────────┼────────────────┘
                                                    │  TCP
                                              ┌─────▼──────┐
                                              │ Kafka       │
                                              │ Broker      │
                                              └─────────────┘
```

**Thread safety:** `confluent_kafka.Producer` is thread-safe at the C-extension
level. Multiple application threads can call `produce()` concurrently on the
same instance without any application-level locking.

---

## 5. Module Structure

```
producers/fire_and_forget/
├── types.py       ← Protocols, dataclasses, type aliases (no Kafka dependency)
├── constants.py   ← Default values for timeouts, retries, health thresholds
├── clients.py     ← confluent_kafka → kafka-python fallback factory
├── core.py        ← FireAndForgetProducer class + convenience functions
├── demo.py        ← Runnable educational demo (6 scenes)
├── __init__.py    ← Public API surface
└── README.md      ← This file
```

**Dependency direction** (each layer only imports from layers above it):

```
types.py  ←  constants.py  ←  clients.py  ←  core.py  ←  demo.py
                                                ↑
                                          __init__.py
```

This means `types.py` never imports Kafka; you can unit-test all result
objects without any broker installed.

---

## 6. The Send Flow — Stage by Stage

```python
result = producer.send(topic="page-views", data={"user_id": 42})
```

### Stage 1.0 — Health guard

Before touching the Kafka client, `send()` checks `is_healthy`. If the
cumulative error rate has exceeded the threshold, the call returns a
`SendResult(success=False)` immediately.

Why: a degraded producer that silently accepts new work makes diagnosis
harder. Failing fast forces an operator decision.

### Stage 2.0 — Serialize payload and key

`data` is JSON-serialized to bytes. The key is UTF-8 encoded if present.
Kafka message headers (optional) are encoded to `[(str, bytes)]` pairs.

### Stage 3.0 — Wire delivery callback

A closure `_wired_callback` wraps the caller-supplied callback (or the
default health-updating callback). It captures a `threading.Event` and
a state dict so confirmation mode can inspect the result.

### Stage 4.0 — `producer.produce()`

Always non-blocking. Returns in microseconds regardless of broker state.

### Stage 4.0a — Fire-and-forget: `poll(0)`

`poll(0)` with a zero timeout fires only delivery callbacks that are
*already ready* — it does not wait for new ones to arrive. The caller
resumes immediately.

```
produce() → poll(0) → return SendResult(success=True)
            ↑
       fires callbacks already ready; skips ones still in flight
```

### Stage 4.0b — Confirmation mode: `_await_delivery_confirmation()`

Only reached when `require_delivery_confirmation=True`. Polls in 100 ms
intervals until the delivery event fires or the deadline passes.

---

## 7. Optional Confirmation Mode

```python
result = producer.send(
    topic="checkout-events",
    data={"order_id": "ord-5001"},
    require_delivery_confirmation=True,
    delivery_timeout_seconds=10.0,
)
```

**When to use:**
- The last record in a batch before you signal a downstream service.
- Critical messages where you want to know *right now* if delivery failed.

**When NOT to use:**
- High-throughput paths (100+ msg/s). Every call now has broker round-trip
  latency added. Use the callback-confirmed or dead-letter-queue pattern instead.

**Failure modes:**
- `TimeoutError` — broker took longer than `delivery_timeout_seconds`.
- `RuntimeError` — broker explicitly rejected the message (topic does not exist,
  authorization failure, etc.).

---

## 8. Retry Logic

```python
result = producer.send_with_retry(
    topic="page-views",
    data={"user_id": 42},
    max_retries=3,
    retry_delay_seconds=1.0,
)
```

**What is being retried:**
Application-level retry on *local* enqueue failures — e.g., producer buffer
full, producer marked unhealthy. The backoff schedule is:

```
attempt 1: immediate
attempt 2: wait 1 s
attempt 3: wait 2 s
attempt 4: wait 4 s
```

**What is NOT being retried here:**
Broker-level delivery failures. Those are handled internally by librdkafka
via the `retries` and `retry.backoff.ms` producer config keys.

**Return value:** `RetryResult` — includes `attempts_made` so you can alert
when messages consistently require retries (a sign of back-pressure).

---

## 9. Health Monitoring

```python
metrics = producer.get_health_metrics()
print(metrics.is_healthy)       # False when error rate > 10%
print(metrics.messages_sent)    # cumulative delivery successes
print(metrics.error_count)      # cumulative delivery failures
print(metrics.error_rate)       # error_count / max(messages_sent + error_count, 1)
print(metrics.sanitized_config) # config with passwords redacted
```

**Health degradation rule:**
After `HEALTH_MIN_MESSAGE_COUNT` (10) total completions, if
`error_count / (messages_sent + error_count) > HEALTH_ERROR_RATE_THRESHOLD` (10%),
`is_healthy` is set to False and `send()` refuses new messages.

**Why wait for 10 completions:**
A single early failure out of 1 attempt gives a 100% error rate which would
permanently kill the producer before it has had a chance to establish a baseline.

**Recovery:**
```python
FireAndForgetProducer.reset_instance()   # creates a fresh instance next call
```

---

## 10. Configuration Reference

| Key | Default | What it controls | When to tune |
|---|---|---|---|
| `DEFAULT_DELIVERY_TIMEOUT_SECONDS` | 10.0 | Max wait in confirmation mode | Increase for cross-DC deployments |
| `DEFAULT_FLUSH_TIMEOUT_SECONDS` | 30.0 | Max flush() wait | Increase for large in-flight backlogs |
| `SHUTDOWN_FLUSH_TIMEOUT_SECONDS` | 60.0 | atexit flush budget | Keep generous; message loss risk |
| `DEFAULT_MAX_RETRIES` | 3 | Application-level retry count | Increase for flaky environments |
| `DEFAULT_RETRY_DELAY_SECONDS` | 1.0 | Base retry backoff | Decrease for time-sensitive alerts |
| `HEALTH_ERROR_RATE_THRESHOLD` | 0.10 | Error rate that triggers unhealthy | Decrease to 0.01 for payments |
| `HEALTH_MIN_MESSAGE_COUNT` | 10 | Messages before health eval | Increase for very low-volume producers |

---

## 11. Running the Demo

```bash
# Start a local Kafka broker (Docker Compose example)
docker compose up -d kafka

# Run the demo (6 scenes)
python -m producers.fire_and_forget.demo

# Override broker address
KAFKA_BOOTSTRAP_SERVERS=localhost:9092 python -m producers.fire_and_forget.demo
```

**Scene 1:** Singleton property — same instance across all call sites.
**Scene 2:** Fire-and-forget send — returns in microseconds.
**Scene 3:** Confirmation mode — blocks until broker acks.
**Scene 4:** Retry — exponential backoff on enqueue failure.
**Scene 5:** Thread safety — 5 threads sharing one producer.
**Scene 6:** Health metrics — observability snapshot.

---

## 12. Interview Talking Points

**Q: What is fire-and-forget in Kafka?**

The producer calls `produce()`, which enqueues the message in librdkafka's
internal buffer and returns immediately. The broker acknowledgement arrives
asynchronously. The caller never blocks. Trade-off: highest throughput, but
messages can be lost if the broker is unreachable when the buffer expires.

**Q: When should you NOT use fire-and-forget?**

When every message must be accounted for: payments, order events, audit logs.
Use callback-confirmed (at-least-once) or idempotent/transactional producers
(exactly-once) for those cases.

**Q: Why not create a new Kafka producer per request?**

Each `Producer()` call opens TCP connections to every broker, allocates 32 MB
of internal buffers, and starts background threads. At high request rates this
creates thousands of broker connections. A shared instance eliminates this
overhead and enables librdkafka's internal batching.

**Q: Is the shared producer thread-safe?**

Yes. `confluent_kafka.Producer` is thread-safe at the C-extension level.
Multiple threads can call `produce()` concurrently on the same instance.
The singleton pattern here manages *lifecycle*, not thread safety.

**Q: What does `poll()` do?**

`poll()` gives librdkafka CPU time to fire delivery callbacks that are
waiting in its internal event queue. Without `poll()`, callbacks would
never execute in the application thread and the delivery event would never
be set. `poll(0)` is non-blocking — it fires only callbacks that are already
ready and returns immediately.

---

## 13. What Comes Next

| Pattern | Adds over fire-and-forget |
|---|---|
| `callback_confirmed` | Explicit delivery verification; structured result objects |
| `dead_letter_queue` | Circuit breaker + bulkhead + DLQ so nothing is silently dropped |
| `idempotent_producer` | Exactly-once delivery via broker-side deduplication |
| `transactional_producer` | Atomic writes across multiple partitions/topics |
