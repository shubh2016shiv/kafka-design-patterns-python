# Chapter 4: Consumer Patterns - How Data Leaves Kafka in Production

## Why Consumer Decisions Matter

If producers decide how safely data enters Kafka, consumers decide how safely and efficiently that data turns into real work.

A consumer is where you decide:

- when an event counts as "done"
- whether duplicates are acceptable
- whether ordering must be preserved
- how much parallelism you want
- what should happen when downstream systems are slow
- how much lag you can tolerate

This is why consumer design is often harder than producer design.

On the producer side, the core question is usually:

- "Did Kafka store the event safely?"

On the consumer side, the core question becomes:

- "Did my application finish the work safely, and can I recover correctly if it crashes halfway?"

That is a much messier problem.

## What a Consumer Actually Does

At a high level, a Kafka consumer repeatedly does four things:

1. join a consumer group
2. poll for records
3. process records
4. commit progress

The critical thing to understand is that Kafka does not push records into your application like a webhook.

The consumer must keep asking for more data.

## The Consumer Lifecycle in One Picture

```text
start consumer
    |
    v
join consumer group
    |
    v
receive partition assignment
    |
    v
poll for records
    |
    v
process records
    |
    v
commit offsets when appropriate
    |
    v
poll again
```

This loop continues for the life of the consumer.

## What `poll()` Really Means

When a consumer calls `poll()`, it is effectively saying:

```text
"Give me records from my assigned partitions, starting from where I should continue."
```

Kafka then returns records from those partitions.

Those records are not automatically considered processed.

This is another common beginner confusion.

Receiving a record is not the same as finishing the work for that record.

That is why commit strategy matters so much.

## A Minimal Mental Model of Consumer Processing

```python
while True:
    records = consumer.poll(timeout=1.0)

    for record in records:
        process(record)

    consumer.commit()
```

This simple loop hides almost all the important production questions:

- what if `process(record)` fails?
- what if processing takes too long?
- what if the consumer crashes before commit?
- what if commits happen too early?
- what if processing is slow and lag grows?
- what if one partition is much hotter than others?

The rest of this chapter exists to answer those questions.

## The Consumer Questions You Must Answer First

Before choosing consumer settings, answer these five questions.

### 1. What happens if the same record is processed twice?

If duplicates are harmless:

- your design is much easier

If duplicates are dangerous:

- commit timing matters more
- idempotency matters more
- retry design matters more

### 2. What happens if processing stops halfway through?

If work can be resumed safely:

- at-least-once semantics are manageable

If partial processing causes external side effects:

- you need stronger idempotency and recovery discipline

### 3. Does order matter?

If order matters for the same entity:

- sequential per-partition processing is often required

If order does not matter:

- you can pursue more concurrency

### 4. Is the work CPU-bound or I/O-bound?

If it is CPU-bound:

- thread-based concurrency may help less
- scaling through more partitions and more consumer instances may help more

If it is I/O-bound:

- concurrent processing can dramatically improve throughput

### 5. What should happen when downstream dependencies are slow?

Your consumer might:

- slow down naturally
- build lag
- apply backpressure
- pause consumption
- route failed records to a DLQ

This is an architectural decision, not just a coding detail.

## The Must-Know Consumer Configurations

These are the core consumer settings every production engineer should understand intuitively.

## 1. `group.id`: Which Logical Application Is This Consumer Part Of?

This setting identifies the consumer group.

Example:

```text
group.id=billing-service
```

Why it exists:

Kafka needs to know which consumers are cooperating as one logical application.

Consumers with the same `group.id`:

- share work
- divide partitions
- share progress state conceptually as one application

Consumers with different `group.id` values:

- consume independently
- keep separate offsets

### Intuition

This setting answers:

**"Which team am I reading with?"**

If two consumers should collaborate on the same workload, they belong in the same group.

If they are different applications, they need different groups.

## 2. `enable.auto.commit`: Should Kafka Commit Progress Automatically?

This is one of the most important and most misunderstood settings.

### `enable.auto.commit=true`

Meaning:

- the client periodically commits offsets automatically

Why it is attractive:

- very simple

Why it is dangerous:

- Kafka may commit progress even though your application has not finished processing safely

This can cause:

- skipped records after failure
- hard-to-debug inconsistency between business work and committed offsets

### `enable.auto.commit=false`

Meaning:

- your application controls when offsets are committed

Why it is preferred in serious systems:

- you commit only after processing is truly complete

### Intuition

This setting answers:

**"Should Kafka guess when I am done, or should my application decide explicitly?"**

For serious production consumers, explicit control is usually the safer choice.

## 3. `auto.offset.reset`: What Should Happen If There Is No Starting Offset?

This setting matters when a consumer group has no committed offset yet, or when its committed offset points to data that no longer exists in Kafka.

Common values:

- `earliest`
- `latest`

### `earliest`

Meaning:

- start from the oldest available retained data

Use when:

- you want to replay history
- a new service should process existing retained records

### `latest`

Meaning:

- start only from new incoming records

Use when:

- historical replay is not needed
- you only care about future data

### Intuition

This setting answers:

**"If I have no usable saved position, should I begin from the start of the retained log, or only from what arrives next?"**

### Important Note

This does not override normal committed offsets.

It is only used when there is no usable committed position.

## 4. `max.poll.interval.ms`: How Long Can I Go Between Polls Before Kafka Thinks I Am Dead?

This is a crucial production setting.

Kafka expects consumers to keep polling.

If too much time passes between polls, Kafka assumes the consumer is unhealthy and triggers a rebalance.

That can happen even if the consumer process is still alive but stuck processing a long-running record.

### Why It Exists

Kafka needs a way to detect consumers that are no longer making progress.

### Why It Hurts

If your processing takes longer than `max.poll.interval.ms`:

- consumer is kicked out of the group
- partitions are reassigned
- work may be retried
- rebalances increase
- latency spikes

### Intuition

This setting answers:

**"How long am I allowed to be busy before Kafka stops trusting me as an active consumer?"**

### Practical Guidance

If your message processing can take minutes:

- raise this setting, or
- redesign processing so polling remains frequent

## 5. `session.timeout.ms`: How Quickly Should the Group Notice a Dead Consumer?

This controls how long the group coordinator waits without heartbeats before marking a consumer dead.

Why it exists:

- failed consumers should not keep partitions forever

Shorter session timeout:

- faster failure detection
- faster partition reassignment
- more sensitivity to network blips or temporary pauses

Longer session timeout:

- more tolerant of transient issues
- slower failover when a consumer really dies

### Intuition

This setting answers:

**"How quickly should the group give up on me if I disappear?"**

## 6. `heartbeat.interval.ms`: How Often Should I Prove I Am Still Alive?

Consumers send heartbeats to remain members of the group.

This works together with `session.timeout.ms`.

Why it exists:

- the coordinator needs ongoing proof that a consumer is still alive

### Intuition

This setting answers:

**"How often should I raise my hand and say I am still here?"**

You usually do not tune this first, but you should understand that heartbeat frequency and session timeout work together.

## 7. `max.poll.records`: How Much Work Should I Take from Kafka at Once?

This controls how many records the consumer returns in one poll.

Large value:

- fewer polls
- larger processing batches
- potentially higher throughput
- more in-flight work per loop
- more risk if processing is slow

Small value:

- tighter control
- lower per-batch latency
- less memory pressure
- more frequent polls

### Intuition

This setting answers:

**"How big a bite should I take each time I ask Kafka for work?"**

## 8. Fetch Settings: How Much Data Should Kafka Send Back Per Request?

Different clients expose slightly different fetch settings, but the important idea is the same:

- how much data can one fetch return?
- how long can the broker wait to accumulate data before responding?

These settings affect:

- throughput
- latency
- memory usage

You usually tune them later, but they matter in very high-throughput systems.

### Intuition

These settings answer:

**"How large and how efficient should each trip to Kafka be?"**

## 9. Partition Assignment Strategy: How Should Partitions Be Divided Among Consumers?

Consumer groups need an assignment strategy.

Common styles include:

- range
- round-robin
- cooperative sticky

You do not need to master every algorithm on day one, but you should know this:

- some strategies rebalance more disruptively
- some preserve assignments better
- cooperative sticky strategies are often preferred in modern systems because they reduce full-stop rebalances

### Intuition

This answers:

**"When group membership changes, should Kafka reshuffle everything, or disturb as little as possible?"**

## How Consumer Config Is Usually Set

Python-style example:

```python
consumer_config = {
    "bootstrap.servers": "broker1:9092,broker2:9092,broker3:9092",
    "group.id": "billing-service",
    "enable.auto.commit": False,
    "auto.offset.reset": "earliest",
    "max.poll.interval.ms": 300000,
    "session.timeout.ms": 45000,
    "heartbeat.interval.ms": 15000,
    "max.poll.records": 100
}
```

Java-style properties example:

```properties
bootstrap.servers=broker1:9092,broker2:9092,broker3:9092
group.id=billing-service
enable.auto.commit=false
auto.offset.reset=earliest
max.poll.interval.ms=300000
session.timeout.ms=45000
heartbeat.interval.ms=15000
max.poll.records=100
```

The exact client syntax differs, but the operational meaning is the same.

## The Most Important Consumer Design Decision: When to Commit Offsets

This is the heart of consumer correctness.

## Commit Too Early

Example:

```text
poll record at offset 10
commit offset 10
start processing
crash before processing finishes
```

What Kafka thinks:

- offset 10 is done

What really happened:

- processing did not finish

Result:

- record can be skipped on restart
- data loss from the application's perspective

## Commit After Processing

Example:

```text
poll record at offset 10
process offset 10
commit offset 10
```

If the app crashes after processing but before commit:

- Kafka will redeliver offset 10

Result:

- possible duplicate processing
- but much safer than silent loss

This is why most production consumers prefer **at-least-once** semantics and make processing idempotent.

## The Three Common Commit Styles

## 1. Auto-Commit

Simple, but risky.

Good for:

- very low-stakes data
- simple internal pipelines where occasional inconsistency is acceptable

Risk:

- application-level work and committed progress can drift apart

## 2. Manual Sync Commit

Application explicitly commits and waits for acknowledgment.

Good for:

- correctness-focused systems
- most serious business workflows

Trade-off:

- safer
- a bit slower

## 3. Manual Async Commit

Application explicitly commits without waiting for acknowledgment.

Good for:

- very high-throughput consumers with strong failure handling

Trade-off:

- faster
- more complex to reason about

### Intuition

Commit strategy answers:

**"At what exact moment am I comfortable telling Kafka that this work is safely behind me?"**

## Sequential vs Concurrent Consumption

This is the consumer equivalent of the producer pattern decision.

## Pattern 1: Sequential Consumer

Sequential consumption means:

- process one record at a time in partition order
- only move forward after current work is complete

Use it when:

- order matters
- work is relatively simple
- correctness is more important than throughput

Examples:

- order lifecycle state transitions
- financial state changes
- audit reconstruction
- workflow engines with ordered state transitions

### Why It Is Attractive

- simplest mental model
- easiest offset management
- easiest debugging
- strongest natural ordering behavior

### Why It Can Be Slow

If processing waits on external systems:

- one slow request delays everything behind it

## Pattern 2: Concurrent Consumer

Concurrent consumption means:

- poll records
- dispatch processing to a worker pool
- manage completion and commits carefully

Use it when:

- work is I/O-bound
- ordering requirements are weaker or managed carefully
- throughput needs exceed sequential processing

Examples:

- calling external APIs
- sending emails or notifications
- making inference calls
- enrichment workloads

### Why It Is Attractive

- much higher throughput for I/O-bound workloads
- better hardware utilization

### Why It Is Harder

You must manage:

- in-flight work
- completion tracking
- safe commit boundaries
- backpressure
- thread safety

## The Offset Problem in Concurrent Consumers

This is the core complexity.

Suppose a partition returns:

```text
offset 100
offset 101
offset 102
```

You hand them to worker threads.

If offset 102 finishes before offset 100:

- you still cannot safely commit 102

Why?

Because if you crash, offsets below it may be lost from the application's perspective.

So concurrent consumers need logic like:

- track which offsets are finished
- commit only the highest continuous completed offset range

This is one of the main reasons sequential consumers remain attractive when ordering matters.

## Backpressure: What If Processing Is Slower Than Intake?

Kafka makes it very easy for consumers to take in data faster than they can finish work.

That sounds good until:

- work queues grow without bound
- memory rises
- processing falls behind
- the consumer crashes

Backpressure is the discipline of slowing intake when the system is already busy.

This might mean:

- pause polling
- pause specific partitions
- stop dispatching more work to worker threads
- apply bounded queues

### Intuition

Backpressure answers:

**"How do I stop eating when I am already too full?"**

Any concurrent consumer should have an intentional backpressure strategy.

## Rebalancing: Why Consumers Sometimes Pause Even When Nothing Seems Wrong

Rebalancing is one of the most important operational behaviors to understand.

It happens when:

- a consumer joins
- a consumer leaves
- a consumer crashes
- a rollout restarts pods
- Kafka decides a consumer is unhealthy

During rebalance:

- partitions are reassigned
- processing may pause
- lag can spike temporarily

If rebalances happen too often, the whole system becomes unstable.

### Common Causes of Excessive Rebalances

- consumers crashing
- `max.poll.interval.ms` too low for actual processing time
- session timeout too aggressive
- unstable deployments
- networking instability

### Intuition

A rebalance is Kafka saying:

**"I need to redraw the map of who owns which partitions."**

That is necessary sometimes, but expensive when it happens constantly.

## Lag: The Operational Signal That Tells You If the Consumer Is Keeping Up

Lag is the difference between:

- latest available offset
- committed offset for the group

Lag means:

- Kafka has more work available than the group has fully completed

Lag is not automatically bad.

Lag becomes concerning when:

- it grows steadily
- it exceeds what your SLA can tolerate
- it approaches the retention boundary
- one partition is much worse than the others

### What Lag Can Mean

Growing lag may indicate:

- not enough consumer instances
- not enough partitions
- downstream API slowdown
- database slowdown
- hot partition
- bad deployment
- commit bottleneck

### Why Per-Partition Lag Matters

If total lag is high, you know you have a problem.

If one partition has far more lag than the others, you often have a more specific diagnosis:

- hot key
- stuck consumer instance
- skewed load

This is why serious Kafka monitoring always looks at partition-level lag.

## Recommended Consumer Config Combinations by Scenario

Now we can turn all of this into practical decisions.

## Scenario 1: Critical Ordered Workflows

Examples:

- payment state transitions
- order lifecycle state machine
- compliance-sensitive processing

Recommended mindset:

- preserve order
- avoid silent skips
- tolerate duplicates better than loss

Typical direction:

```text
enable.auto.commit=false
manual commit after processing
sequential processing
group.id per service
auto.offset.reset=earliest for recoverability if appropriate
max.poll.records kept moderate
max.poll.interval.ms sized for worst-case processing time
```

## Scenario 2: High-Volume I/O-Bound Enrichment

Examples:

- API enrichment
- ML inference calls
- notification fanout

Recommended mindset:

- throughput matters
- ordering may matter less or only per key
- backpressure is essential

Typical direction:

```text
enable.auto.commit=false
manual commit after confirmed processing
concurrent worker pool
bounded in-flight queue
max.poll.records tuned to avoid overloading worker pool
max.poll.interval.ms increased if batches can take longer
```

## Scenario 3: Low-Stakes Analytics Consumption

Examples:

- internal metrics aggregation
- exploratory analytics

Recommended mindset:

- simplicity may matter more than perfect correctness

Typical direction:

```text
enable.auto.commit=true or simpler manual approach
auto.offset.reset=latest or earliest depending replay need
larger batches acceptable
ordering often less critical
```

## Scenario 4: New Service Bootstrapping from History

Examples:

- new analytics pipeline
- new GenAI enrichment service
- replaying retained events into a new sink

Recommended mindset:

- start from retained history
- expect lag initially
- tune for controlled catch-up

Typical direction:

```text
auto.offset.reset=earliest
enable.auto.commit=false
batch processing
careful lag monitoring
retention window must be long enough
```

## The Consumer Decision Framework

If you are unsure how to design a consumer, walk through this checklist.

### Choose sequential processing when:

- order matters strongly
- debugging simplicity matters
- throughput needs are moderate

### Choose concurrent processing when:

- work is I/O-bound
- one-by-one processing is too slow
- you are ready to manage commit complexity and backpressure

### Choose manual commit when:

- correctness matters
- you want processing to determine when progress is recorded

### Choose simpler commit behavior when:

- data is low stakes
- occasional inconsistency is acceptable
- operational simplicity matters more than strict correctness

### Revisit group and poll settings when:

- rebalances are frequent
- processing time is highly variable
- consumers appear healthy but still get kicked from the group

## The Intuition to Carry Forward

If you remember only a few lines from this chapter, remember these:

- a consumer is a polling loop, not a passive receiver
- receiving a record is not the same as finishing the work
- commit timing defines your correctness model
- `group.id` defines who collaborates on the workload
- `max.poll.interval.ms`, session timeout, and heartbeats define how Kafka decides whether to trust you as a live consumer
- concurrency improves throughput, but makes offset safety harder
- lag is the main signal telling you whether your consumer design is actually working

Once this is clear, the next architectural step is easier to reason about:

- if different classes of work need different treatment, you may need separate topics, separate groups, or a priority architecture

---

**What comes next:** Chapter 5 covers priority architecture and shows how to translate different business service levels into Kafka topic design, routing strategy, and consumer allocation decisions.
