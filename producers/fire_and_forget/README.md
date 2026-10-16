# Fire-and-Forget Producer

**Send the message, and never find out whether it arrived.**

This package is a teaching-quality implementation of one Kafka delivery mode. It assumes no prior Kafka knowledge: every term is defined the first time it appears, and every configuration value explains why it has that default and when you would change it.

```bash
# Run the tutorial (needs a broker; see Prerequisites below)
python -m producers.fire_and_forget
```

---

## Contents

1. [The idea in one picture](#1-the-idea-in-one-picture)
2. [What makes it fire-and-forget](#2-what-makes-it-fire-and-forget)
3. [The three calls: produce, poll, flush](#3-the-three-calls-produce-poll-flush)
4. [Quick start](#4-quick-start)
5. [Module map: what to read, in order](#5-module-map-what-to-read-in-order)
6. [Reading a SendResult correctly](#6-reading-a-sendresult-correctly)
7. [When to use this, and when not to](#7-when-to-use-this-and-when-not-to)
8. [Production checklist](#8-production-checklist)
9. [Prerequisites and running](#9-prerequisites-and-running)
10. [Testing](#10-testing)
11. [Related packages](#11-related-packages)

---

## 1. The idea in one picture

Think of dropping a postcard into a mailbox. You let go and walk away. It almost certainly arrives. You get **no tracking number**, and if it is lost, nobody tells you.

```text
  YOUR THREAD                    BACKGROUND THREAD (in the C library)      BROKER
  ───────────                    ────────────────────────────────────      ──────
  send()
    │  validate + serialize
    │
    ├──> produce()  ──────────>  [ message waits in a local queue ]
    │    returns instantly;                │
    │    touches no network                │  started automatically;
    │                                      │  you never call it yourself
    │                                      v
    └──> poll(0)            send batch over network ───────────────────>  broker stores it
         handle results                                      <──────────  broker answers
         already back                      │
                                           v
  caller resumes                 [ answer waits in a report queue ]
  immediately                              │
                                           │  NOBODY IS LISTENING: no delivery
                                           x  callback was registered, so this
                                              answer is discarded — success or not

  close()  →  flush()  -  before shutdown: give queued messages a bounded
                          chance to finish instead of abandoning them.
```

**Jargon, defined once:**

| Term | Plain meaning |
|---|---|
| **Broker** | One running Kafka server. |
| **Topic** | A named stream of messages, like a folder name. |
| **Partition** | One of the parallel ordered logs a topic is split into. |
| **Asynchronous** | The call returns *before* the work is done; the work finishes later. |
| **Delivery callback** | A function Kafka would call to report "delivered" or "failed". This pattern deliberately does not register one. |
| **Back-pressure** | Kafka telling your code "you are producing faster than I can deliver; slow down." |

---

## 2. What makes it fire-and-forget

**There is no configuration setting for this.** There is no `fire_and_forget=True` flag anywhere in Kafka. The pattern is established by exactly one thing:

```python
producer.produce(topic, value=payload)  # fire-and-forget
producer.produce(topic, value=payload, on_delivery=callback)  # NOT fire-and-forget
```

It is the **absence of the `on_delivery=` argument**. Nothing else. Same class, same broker, same network trip, same `acks` setting. Adding that one argument turns this into the [callback-confirmed](../callback_confirmed/) pattern.

Two things people wrongly believe control it:

| Common belief | Why it is wrong |
|---|---|
| `acks=0` makes it fire-and-forget | `acks` decides how careful the **broker** is before answering. Whether **your code** listens to that answer is a separate decision. This package uses `acks="all"` and is still fire-and-forget, because it never listens. |
| There is an async/sync mode flag | No. **Every** `produce()` call in this library is asynchronous. It returns without waiting for a delivery outcome. |

---

## 3. The three calls: produce, poll, flush

The library runs **two threads**, though you only write code for one:

- **Your thread** runs `produce()`, `poll()`, `flush()`, and your callbacks.
- **A hidden background thread**, started automatically when the producer is created, does all the actual networking. You never call it.

| Call | What it does |
|---|---|
| `produce()` | Queue the message locally and return. **Never touches the network.** That is why it is instant even when the broker is slow or dead. |
| *(background thread)* | Actually sends the batch and receives the broker's answer — on its own schedule, with no code from you. |
| `poll(timeout)` | Process answers that have **already arrived**, running your callback for each. It does **not** send anything and does not ask the broker for anything. `poll(0)` = "handle what is ready now, do not wait." |
| `flush(timeout)` | Keep polling until nothing is outstanding, or the timeout expires. |

**Two consequences that trip people up:**

1. **`poll()` serves callbacks; the background client performs delivery.** This package does not register a delivery callback, so it does not poll after every successful send. It does poll briefly after `BufferError`, following the client guidance before retrying a full local queue. Call `poll()` yourself only if your custom configuration registers event, statistics, error, or logging callbacks.

2. **Forgetting `flush()` can lose locally queued messages.** Always `close()` (or use the `with` form) before exiting. A successful flush means messages reached a final outcome, not necessarily that every message was delivered; this pattern intentionally receives no per-message report.

> Note: `Producer.poll()` and `Consumer.poll()` are **different methods that share a name**. The consumer's `poll()` fetches new messages from a topic. The producer's `poll()`, described here, only processes results for messages you already sent.

---

## 4. Quick start

```python
from producers.fire_and_forget import FireAndForgetProducer

# Recommended form: close() is called for you, even if an exception is raised.
with FireAndForgetProducer() as producer:
    producer.send(
        topic="page-views",
        data={"user_id": "u-1", "page": "/home"},
        key="u-1",  # optional: same key -> same partition -> ordered
    )
```

Equivalent explicit form:

```python
producer = FireAndForgetProducer()
try:
    producer.send("page-views", {"user_id": "u-1", "page": "/home"})
finally:
    producer.close()  # ALWAYS: flushes what is still queued
```

**Create one producer per application and share it.** A producer holds TCP connections to the brokers and accumulates messages into batches. Creating one per message destroys both benefits and leaks connections. It is safe to share across threads: the underlying client synchronises internally, so no locking is needed around `send()`.

**About the `key` argument:** messages sharing a key always land in the same partition, which is how Kafka preserves ordering per entity (per customer, per device). Pass `None` when ordering does not matter — the client then spreads messages across partitions, which batches better.

---

## 5. Module map: what to read, in order

| File | What it holds |
|---|---|
| [`constants.py`](constants.py) | Tunable defaults and the reason behind each one. |
| [`types.py`](types.py) | The producer protocol, result object, and stable failure categories. |
| [`config.py`](config.py) | Kafka settings, with the production tradeoff beside each decision. |
| [`clients.py`](clients.py) | The single place the concrete Kafka library is touched. |
| [`core.py`](core.py) | **The pattern itself.** Start with its flow diagram. |
| [`demo.py`](demo.py) | Runnable tutorial scenes. |

Flow-critical code uses `Stage n.n` markers. Imports and executable steps have nearby plain-language comments; unfamiliar Kafka and Python terms are defined where they first matter.

---

## 6. Reading a SendResult correctly

```python
result = producer.send("events", {"a": 1})
result.queued  # True = the LOCAL QUEUE accepted it
result.failure_reason  # stable enum for a local rejection, or None
result.retryable  # True only when local back-pressure may clear
result.error_message  # why queueing failed, or None
```

**`queued=True` does not mean the message was delivered.** It means the local in-memory queue accepted it. The broker may not have it, and it may never arrive.

That is not a limitation of the class — it is the definition of the pattern. No delivery callback was registered, so nothing in this process will ever learn the delivery outcome. A result object cannot report information the pattern deliberately declines to collect.

`error_message` is populated only for **local** failures:

| Failure | Reported? |
|---|---|
| Local queue full even after waiting | Yes — `queued=False` |
| Message rejected by the client (e.g. too large) | Yes — `queued=False` |
| Producer already closed | Yes — `queued=False` |
| Invalid arguments (blank topic, non-serializable data) | Raises `ValueError` — a caller bug, not a runtime condition |
| **Broker rejected it / broker unreachable / delivery timed out** | **No. Never. By design.** |

Callers should branch on `failure_reason`, not text:

```python
from producers.fire_and_forget import SendFailureReason

result = producer.send("telemetry", event)
if result.failure_reason is SendFailureReason.QUEUE_FULL:
    shed_or_buffer_elsewhere(event)
```

---

## 7. When to use this, and when not to

| Use fire-and-forget when… | Do **not** use it when… |
|---|---|
| High volume, each message cheap | Each message is valuable |
| Losing a few is acceptable | Loss must be detected or prevented |
| Throughput matters above all | You need to react to a failure |
| *Examples:* metrics, logs, clickstream, telemetry, IoT sensor pings | *Examples:* payments, orders, sign-ups, inventory changes, audit logs |

**Decision rule:** *"If one of these messages vanished and nobody noticed, is that acceptable?"*
Yes → fire-and-forget. No → [callback-confirmed](../callback_confirmed/).

**Can you make it safer without leaving the pattern?** Partly. This package already sets `acks="all"` and a bounded `delivery.timeout.ms`, so the broker side is durable and retried. But you still will not be *notified* of a final failure — and that notification is exactly what the next pattern adds.

---

## 8. Production checklist

| Item | Why |
|---|---|
| List 2–3 brokers in `bootstrap.servers` | If the one address you listed is the broker that is down, the producer cannot start at all, even though the cluster is healthy. |
| Set a meaningful `client.id` | During an incident, "which application is flooding this topic?" becomes answerable instead of guesswork. |
| Topic `replication.factor=3`, `min.insync.replicas=2` | Without these, `acks="all"` can mean "one replica" — a far weaker guarantee than it sounds. |
| Keep `delivery.timeout.ms` bounded | This, not `retries`, is what actually bounds retrying. `retries` defaults to effectively unlimited. |
| Align `SHUTDOWN_FLUSH_TIMEOUT_SECONDS` with your platform's kill timeout | The 25-second default fits inside Kubernetes' common 30-second grace period, but your deployment may differ. |
| Turn off `auto.create.topics.enable` on the broker | Otherwise a typo in a topic name silently creates a new topic instead of failing loudly. |
| Always `close()` on shutdown | Queued messages are lost otherwise. Use the `with` form so it happens even on an exception. |
| Decide consciously that silent loss is acceptable | If it is not, this is the wrong pattern. |

---

## 9. Prerequisites and running

A reachable Kafka broker. For the local lab in [`infrastructure/`](../../infrastructure/):

```bash
cd infrastructure && docker compose up -d
```

Then:

```bash
# Default broker: localhost:9092
python -m producers.fire_and_forget

# Or point it elsewhere
KAFKA_BOOTSTRAP_SERVERS=broker:9092 python -m producers.fire_and_forget
```

**The four tutorial scenes:**

| Scene | Shows |
|---|---|
| 1 | `send()` returns without waiting to learn whether the broker stored the message |
| 2 | The `produce` → `poll` → `flush` lifecycle, with real timings: queueing is nearly free, `flush()` is where the waiting happens |
| 3 | The payoff: throughput from a single thread (~100k messages/second on a laptop) |
| 4 | The honest weakness: a guaranteed delivery failure that produces no per-message error in the application result |

---

## 10. Testing

```bash
python -m unittest test.producer.test_fire_and_forget_producer
```

51 tests, **no broker required**. Every test injects a `FakeProducer` through the `producer_factory` argument, so the whole send path runs with no network — which is why [`types.py`](types.py) defines `KafkaProducerLike` as a `Protocol`: anything with the right three methods will do.

The most important test is `TestFireAndForgetMechanism.test_never_registers_a_delivery_callback`. If a future change adds an `on_delivery` callback, the class stops being fire-and-forget, and that test fails to say so.

---

## 11. Related packages

| Package | What it adds |
|---|---|
| [`producers.callback_confirmed`](../callback_confirmed/) | The same send, plus a delivery callback — so failures become visible instead of silent. The minimal next step from here. |
| [`producers.singleton`](../singleton/) | A process-wide shared producer, with confirmation mode, local retry, and health metrics. **These four concerns used to live in this package** and made it hard to learn from; they are about producer lifecycle and operations, not about a delivery mode. |
| [`producers.dead_letter_queue`](../dead_letter_queue/) | What to do when delivery fails: retry, circuit-break, and preserve the message for replay. |
| [`producers.content_based_router`](../content_based_router/) | Choosing the destination topic from the message's own content. |

For the step-by-step notebook version of this material, see [`notebook/producer_design_pattern/01_fire_and_forget.ipynb`](../../notebook/producer_design_pattern/01_fire_and_forget.ipynb).
