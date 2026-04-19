# Chapter 3: Producer Patterns - How Data Enters Kafka in Production

## Why Producer Decisions Matter

A Kafka producer looks simple from the application side:

- create an event
- send it to a topic

That simplicity is misleading.

The producer is where you decide:

- how much data loss you can tolerate
- how much latency you can tolerate
- how much throughput you need
- whether retries can create duplicates
- whether your service should block during Kafka trouble or fail fast

This is why "just use the default producer" is not enough for production systems.

Producer configuration is really a set of business decisions translated into infrastructure behavior.

This chapter is written to make those decisions intuitive.

## What a Producer Actually Does

Let us walk through what really happens when your code sends a Kafka **record**. In this chapter, "message" may still appear informally, but **record** is the more precise term.

### Step 1: Build the Event

Your application creates a record:

- topic
- key
- value
- maybe headers

Example:

```json
{
  "topic": "orders",
  "key": "customer_42",
  "headers": {
    "event_type": "order_created",
    "schema_version": "v1"
  },
  "value": {
    "order_id": "ord_1001",
    "customer_id": "customer_42",
    "amount": 499.99
  }
}
```

### Step 2: Serialize It

Kafka does not send Python dicts or Java objects directly.

The producer serializes the record into bytes:

- JSON
- Avro
- Protobuf
- MessagePack
- custom binary format

This matters because schema decisions and serialization costs affect:

- payload size
- interoperability
- schema evolution
- consumer compatibility

### Step 3: Put It into the Producer Buffer

This is the first thing many beginners miss:

**calling `produce()` usually does not mean the record was immediately written to Kafka**

The producer typically places the record into an in-memory buffer first.

Why?

Because sending one network request per event is inefficient.

The producer wants to batch records together.

### Step 4: Batch and Send

The producer groups buffered records and sends them to the partition leader broker.

This is where settings like:

- `linger.ms`
- `batch.size`
- `compression.type`

start to matter.

### Step 5: Wait for Acknowledgment

Depending on configuration, the producer may wait for:

- no acknowledgment
- only the leader broker
- all required in-sync replicas

This is where `acks` matters.

### Step 6: Retry or Report Success/Failure

If the send fails:

- the producer may retry
- the retry may be safe or unsafe depending on idempotence
- your callback/future gets success or failure information

That is where:

- `enable.idempotence`
- `retries`
- `retry.backoff.ms`
- `delivery.timeout.ms`

become important.

## The Producer Lifecycle in One Picture

```text
Application code
    |
    v
create event
    |
    v
serialize event
    |
    v
place into producer buffer
    |
    v
batch with other events
    |
    v
send to Kafka leader
    |
    v
wait for ack / retry on transient failure
    |
    v
success callback or failure callback
```

The practical implication is:

**a producer is not just "a socket that writes data." It is a stateful component making buffering, batching, retry, and durability decisions on your behalf.**

## The Producer Questions You Must Answer First

Before choosing config values, answer these five questions.

### 1. If this event is lost, what happens?

If losing one event is harmless:

- you can optimize harder for speed

If losing one event is unacceptable:

- you need strong durability settings

### 2. If this event is published twice, what happens?

If duplicates are harmless:

- retries are easy to accept

If duplicates create real business damage:

- idempotence becomes essential
- downstream systems may also need deduplication

### 3. Is this a low-latency path or a high-throughput path?

If you need sub-10ms publish latency:

- aggressive batching may hurt you

If you need huge throughput:

- batching and compression help a lot

### 4. Is this a one-off script or a long-running service?

This determines whether a simple producer is acceptable or a long-lived producer is mandatory.

### 5. What should happen if Kafka is temporarily unhealthy?

Your service might:

- block and wait
- retry for a while
- fail fast
- write to a local outbox

This is an architectural choice, not just a config choice.

## The Must-Know Producer Configurations

This section is the core of production producer knowledge.

Do not memorize these as isolated knobs. Each one exists to answer a specific production question.

## 1. `acks`: How Safe Does a Successful Send Need to Be?

`acks` controls how many broker acknowledgments the producer waits for before calling the send successful.

### `acks=0`

Meaning:

- do not wait for acknowledgment

What it optimizes for:

- maximum speed

What can go wrong:

- the record may be lost and the producer will not know

Use only when:

- the data is expendable
- you truly prefer speed over reliability

Example use cases:

- best-effort telemetry
- debug events
- high-volume metrics you can afford to drop

### `acks=1`

Meaning:

- wait only for the partition leader to acknowledge

What it optimizes for:

- decent speed with some durability

What can go wrong:

- leader acknowledges
- leader crashes before followers replicate
- acknowledged data can still be lost

Use when:

- you want moderate durability
- losing a small amount of acknowledged data is acceptable

### `acks=all`

Meaning:

- wait for all required in-sync replicas

What it optimizes for:

- strongest durability

What it costs:

- higher latency
- lower throughput than weaker ack settings

Use when:

- the event matters
- money, user actions, audit trails, workflows, or critical business state are involved

### Intuition

Think of `acks` as asking:

**"How much evidence do I want before I believe this write is safe?"**

## 2. `enable.idempotence`: Can Retries Create Duplicate Records?

This is one of the most important Kafka producer settings.

Set:

```text
enable.idempotence=true
```

Why it exists:

Sometimes the producer sends a record successfully, but the acknowledgment is lost or delayed. The producer retries because it thinks the first attempt failed.

Without idempotence:

- Kafka may store the same record twice

With idempotence:

- Kafka can detect and suppress duplicate retry writes from that producer session

### Intuition

This setting exists to answer:

**"If I retry because I am unsure what happened, can Kafka protect me from accidental duplicate writes?"**

### Practical Recommendation

For almost all serious production producers:

```text
enable.idempotence=true
```

should be the default unless you have a very specific reason otherwise.

## 3. `retries` and `retry.backoff.ms`: What Should Happen on Transient Failure?

Transient failures happen all the time in distributed systems:

- leader elections
- brief network issues
- overloaded broker
- temporary ISR shrink

Retries exist because many failures are temporary.

### `retries`

This controls how many times the producer will retry failed sends.

### `retry.backoff.ms`

This controls how long the producer waits between retries.

### Why They Matter Together

Retrying immediately and aggressively can create a retry storm.

Retrying too little can cause unnecessary failures.

The right model is:

- transient failure -> retry a few times
- repeated failure -> surface the problem

### Intuition

This config answers:

**"How patient should I be with temporary Kafka problems?"**

### Practical Guidance

For most production services:

- retries should be enabled
- backoff should not be zero
- idempotence should be enabled alongside retries

## 4. `delivery.timeout.ms`: When Should the Producer Give Up Completely?

This controls the maximum total time a record is allowed to spend trying to get delivered, including retries.

Why it exists:

Without a total deadline, a producer can keep retrying for too long and cause request paths to hang or pile up.

### Intuition

This setting answers:

**"How long am I willing to keep trying before I treat this send as failed?"**

Shorter timeout:

- faster failure
- better for user-facing low-latency services

Longer timeout:

- more resilience to temporary Kafka trouble
- more waiting before failure is visible

## 5. `linger.ms`: Should I Send Immediately or Wait Briefly to Batch?

This is another critical setting.

Kafka producers get much better throughput when they batch records.

`linger.ms` tells the producer:

- send immediately, or
- wait a little to accumulate more records into a batch

### `linger.ms=0`

Meaning:

- send as soon as possible

Best for:

- lowest latency

Trade-off:

- smaller batches
- lower throughput efficiency

### `linger.ms=5` or `10`

Meaning:

- wait a few milliseconds for more records to join the batch

Best for:

- higher throughput systems

Trade-off:

- a little more latency
- much better batching efficiency

### Intuition

This config answers:

**"Would I rather leave immediately, or wait briefly for the bus to fill up?"**

## 6. `batch.size`: How Big Can Each Batch Be?

This controls the maximum batch size per partition before the producer sends.

Why it exists:

- larger batches improve throughput
- but they also use more memory and may increase latency if traffic is low

### Intuition

This setting answers:

**"How large am I willing to let my outgoing packet groups become?"**

In many systems, `linger.ms` has the clearer intuitive effect and is tuned first. `batch.size` becomes more relevant in high-throughput systems.

## 7. `compression.type`: Should I Spend CPU to Save Network and Disk?

Common choices:

- `none`
- `snappy`
- `lz4`
- `gzip`
- `zstd`

Compression reduces:

- network traffic
- broker disk usage

But costs:

- CPU on producers and consumers

### Intuition

This config answers:

**"Would I rather spend CPU, or spend bandwidth and storage?"**

### Practical Guidance

- `snappy`, `lz4`, or `zstd` are common production choices
- compression is especially valuable for high-volume topics
- very latency-sensitive tiny messages may see less benefit

## 8. `max.in.flight.requests.per.connection`: How Much Concurrency Should I Allow Per Connection?

This controls how many produce requests can be in flight on one connection before earlier ones are acknowledged.

Why it matters:

- more in-flight requests can improve throughput
- but under retries and failures, too much concurrency can complicate ordering guarantees

With idempotence enabled, Kafka clients handle this more safely, but it is still a meaningful knob for advanced tuning.

### Intuition

This setting answers:

**"How many unconfirmed sends should I allow at once on one connection?"**

For most teams:

- do not tune this first
- understand that it exists because concurrency and ordering safety can pull in opposite directions

## 9. Serialization and Schema Choice: What Exactly Am I Sending?

This is often missed in Kafka tutorials, but it is absolutely a production concern.

Your producer must decide:

- JSON
- Avro
- Protobuf
- other formats

This choice affects:

- payload size
- consumer compatibility
- schema evolution
- validation discipline

### Intuition

This answers:

**"What contract am I making with every consumer of this topic?"**

For multi-team systems, schema discipline matters as much as transport reliability.

## How These Configs Are Usually Set

A producer is usually configured in application code or framework config.

Python-style example:

```python
producer_config = {
    "bootstrap.servers": "broker1:9092,broker2:9092,broker3:9092",
    "acks": "all",
    "enable.idempotence": True,
    "retries": 10,
    "retry.backoff.ms": 200,
    "delivery.timeout.ms": 30000,
    "linger.ms": 5,
    "batch.size": 131072,
    "compression.type": "lz4"
}
```

Java-style properties example:

```properties
bootstrap.servers=broker1:9092,broker2:9092,broker3:9092
acks=all
enable.idempotence=true
retries=10
retry.backoff.ms=200
delivery.timeout.ms=30000
linger.ms=5
batch.size=131072
compression.type=lz4
```

The exact syntax differs by client library, but the semantics are the same.

## Recommended Producer Config Combinations by Scenario

This is where intuition becomes usable.

## Scenario 1: Critical Business Events

Examples:

- orders
- payments
- audit events
- user account lifecycle changes

Recommended mindset:

- do not lose events
- avoid duplicate writes during retries
- modest extra latency is acceptable

Typical config direction:

```text
acks=all
enable.idempotence=true
retries=on
retry.backoff.ms=100-500
delivery.timeout.ms=30s or more
linger.ms=5-20
compression.type=lz4 or zstd
```

This is the default serious-production profile.

## Scenario 2: User-Facing Low-Latency Events

Examples:

- interactive app events where publish latency affects request latency directly

Recommended mindset:

- still durable, but keep the path fast

Typical config direction:

```text
acks=all
enable.idempotence=true
retries=on
linger.ms=0-5
smaller delivery timeout than batch pipelines
compression tuned conservatively
```

You still want safety, but you avoid overly aggressive batching.

## Scenario 3: High-Volume Analytics / Telemetry

Examples:

- clickstream
- observability streams
- non-critical usage metrics

Recommended mindset:

- throughput matters most
- some loss may be acceptable

Typical config direction:

```text
acks=1 or sometimes 0
enable.idempotence=depends on duplicate sensitivity
linger.ms=10-50
larger batch.size
compression.type=lz4/zstd
retries=moderate
```

This is where performance optimization starts to outweigh strict durability.

## Scenario 4: One-Off Scripts and Batch Jobs

Examples:

- migration scripts
- backfills
- administrative replay jobs

Recommended mindset:

- simplicity is okay
- but do not forget flush and error handling

Typical direction:

- a simple short-lived producer is fine
- stronger durability settings still matter if data matters
- always flush before exit

## The Four Producer Patterns That Actually Matter

The original way of describing producers as named patterns is still useful, but now we can ground them better.

## Pattern 1: The Simple Producer

This is:

- create producer
- send a small amount of data
- flush
- exit

Use it for:

- scripts
- CLI tools
- migrations
- tests

Do not use it for:

- hot request paths
- long-running services

Why not?

Because creating a producer repeatedly is expensive:

- connection setup
- authentication
- metadata fetch
- buffer warmup

### Intuition

A simple producer is like renting a truck for one delivery.

Perfect for occasional use.
Terrible if you ship packages all day.

## Pattern 2: The Long-Lived Producer

This is the default for real services.

The service starts one producer instance and reuses it.

Use it for:

- APIs
- web services
- daemons
- long-running workers

Why it exists:

- avoids repeated connection setup
- allows efficient batching
- makes monitoring easier
- gives stable, predictable behavior

### Intuition

If your application publishes continuously, the producer should be treated as infrastructure, not a temporary helper object.

## Pattern 3: The Routing Producer

This is a producer wrapper that hides topic-routing business logic from callers.

Use it when:

- many callers publish to multiple topics
- routing rules must stay consistent
- topic names should be centralized

It is especially useful when routing depends on:

- priority
- tenant
- region
- event class

Trade-off:

- clearer central logic
- but one more abstraction layer to debug

## Pattern 4: The Resilient Producer

This is for services where Kafka trouble should not take down the whole app.

It typically combines:

- retries with backoff
- timeouts
- sometimes circuit breakers
- sometimes bulkheads
- sometimes a local outbox

Use it when:

- Kafka outages must not cascade into full service outages
- request threads must fail fast or degrade gracefully
- delivery guarantees are important enough to need fallback strategy

### The Key Idea

Producer resilience is not only about "can I retry?"

It is also about:

- how long will I block?
- what happens to the caller?
- what happens if Kafka is unavailable for minutes?

## The Delivery Callback / Future: The Only Honest Source of Truth

A producer send call often returns before the record is durably acknowledged.

So the send call itself is not the final truth.

The real result comes from:

- callback
- future
- promise

depending on the client library.

Example logic:

```python
def delivery_callback(err, msg):
    if err is not None:
        logger.error(f"delivery failed: {err}")
    else:
        logger.info(
            "delivered to topic=%s partition=%s offset=%s",
            msg.topic(),
            msg.partition(),
            msg.offset()
        )
```

This is where you decide what failure means:

- log and move on
- return error to caller
- retry
- write to outbox
- trigger alerting

## The Outbox Pattern: When "Just Retry" Is Not Enough

For critical systems, producer retries alone may not be enough.

Why?

Because if Kafka is unavailable for long enough:

- request threads may time out
- in-memory buffers may fill
- events may be lost before they ever reach Kafka

The outbox pattern solves this by first writing the event to a local durable store tied to the business transaction, then asynchronously publishing it to Kafka.

Typical flow:

```text
application writes business row + outbox row in one DB transaction
        |
        v
background publisher reads outbox rows
        |
        v
publishes them to Kafka
        |
        v
marks outbox rows as sent
```

This is a higher-complexity pattern, but it is often the right answer for truly important event publication.

## The Producer Decision Framework

If you are unsure how to choose producer settings, walk through this checklist.

### Choose strong safety settings when:

- data loss is unacceptable
- duplicates must be minimized
- business events matter
- replay alone is not enough to recover

### Choose throughput-oriented settings when:

- events are high volume and lower value
- small losses are acceptable
- latency is less important than cost efficiency

### Choose low-latency settings when:

- producer latency directly affects user requests
- batching delay matters visibly

### Choose resilience patterns when:

- Kafka trouble must not take down the app
- you need graceful degradation
- you need outbox-based durability

## The Intuition to Carry Forward

If you remember only a few lines from this chapter, remember these:

- `acks` is about how much proof you want before calling a send successful
- idempotence is about making retries safer
- retries and timeouts are about how patient you are with transient failure
- `linger.ms`, `batch.size`, and compression are about trading latency for throughput efficiency
- producer patterns are really about application shape: script, service, router, or resilient publisher

Once this is clear, the consumer side becomes the natural next question:

- producer decides how safely data enters Kafka
- consumer decides how safely and efficiently data is processed after that

---

**What comes next:** Chapter 4 covers consumer patterns and the consumer-side configuration decisions that matter most in production: commit timing, auto-commit vs manual commit, sequential vs concurrent processing, and how to choose safety, throughput, and resilience trade-offs on the read path. Chapters 3 and 4 together form the main config foundation, and Chapter 7 ties both sides together into one production decision framework.
