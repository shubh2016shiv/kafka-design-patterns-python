# Chapter 7: Production Decisions - Designing, Operating, and Debugging Kafka Systems

## Why This Chapter Exists

The earlier chapters explained:

- why Kafka exists
- how topics, partitions, offsets, consumer groups, and retention work
- how producers and consumers behave
- how to think about priority lanes
- how resilience patterns protect the system

This chapter answers the final question:

**how do you actually make good production decisions with all of that?**

That means three things:

1. designing a Kafka system before it is built
2. operating it once it is live
3. debugging it when something goes wrong

That is the lens of this chapter.

## The Wrong Way to Build Kafka Systems

The wrong production mindset looks like this:

```text
"Let's create a topic, add some partitions, copy a producer config from the internet,
copy a consumer config from some blog, and tune later if needed."
```

That usually leads to:

- under-partitioned topics
- broken ordering assumptions
- accidental data loss
- high lag under load
- weak monitoring
- retention cliffs
- confusing incidents

Kafka is not hard because the APIs are hard.

Kafka is hard because every choice has consequences:

- safety
- latency
- throughput
- cost
- recovery behavior
- operational complexity

Good production design starts by answering the right questions before code exists.

## Part 1: Design Decisions Before You Build Anything

## The Seven Questions You Must Answer First

These seven questions are the minimum design brief for a Kafka-backed production system.

## 1. What is the maximum acceptable delay?

Ask:

```text
"How late can this event be before the business considers it a failure?"
```

Examples:

- fraud alert: under 200ms
- payment authorization update: under 2 seconds
- analytics export: within 10 minutes
- nightly batch: within 6 hours

This one answer influences:

- topic partition count
- consumer concurrency
- whether you need priority lanes
- alert thresholds
- whether batching is acceptable

If the team answers with vague language like:

- "pretty fast"
- "near real time"

you do not yet have a usable production requirement.

## 2. What happens if the event is processed twice?

This is your idempotency question.

Ask:

```text
"If this record is replayed or redelivered, what breaks?"
```

Examples:

- charging a customer twice -> severe problem
- writing duplicate analytics rows -> maybe acceptable
- updating a status row to the same value twice -> often harmless

This answer drives:

- commit strategy
- producer idempotence importance
- consumer deduplication logic
- whether at-least-once semantics are acceptable

## 3. What happens if the event is never processed?

This is your loss-tolerance question.

Ask:

```text
"If one record disappears forever, what is the business consequence?"
```

Possible answers:

- compliance issue
- customer-visible breakage
- minor analytics loss
- no meaningful impact

This answer shapes:

- producer `acks`
- producer idempotence
- topic replication factor
- `min.insync.replicas`
- DLQ design
- whether outbox patterns are needed

## 4. What is peak event rate, not average event rate?

Kafka systems fail at peaks, not averages.

Ask:

```text
"At maximum load, how many records per second could this topic receive?"
```

Then ask:

```text
"How many records per second can one consumer instance safely process?"
```

That is the starting point for:

- partition count
- consumer instance count
- worker pool sizing
- autoscaling policy

Designing from average load is one of the most common mistakes.

## 5. Who will consume this topic?

This question is often underestimated.

Ask:

```text
"Which applications, teams, and workflows will read this data?"
```

Because topic design depends heavily on:

- how many independent consumer groups exist
- whether some consumers need real-time behavior
- whether some are batch-oriented
- whether different service levels require topic separation

This is where you discover whether:

- one topic is enough
- you need multiple consumer groups
- you need a priority architecture
- you need different retention profiles

## 6. How long must this data remain replayable?

Ask:

```text
"How much historical data do I need available in Kafka itself?"
```

Examples:

- 24 hours
- 7 days
- 30 days
- Kafka only for short buffer, long-term archive elsewhere

This drives:

- retention settings
- disk planning
- recovery expectations
- whether Kafka alone is enough or archival storage is required

This is also where many teams get retention dangerously wrong.

If replay matters, retention is not just a storage knob.
It is a recovery contract.

## 7. What external dependencies are in the processing path?

Ask:

```text
"What does the consumer call, and how reliable are those things really?"
```

Examples:

- payment gateway
- enrichment API
- vector database
- relational database
- email provider

This determines whether you likely need:

- rate limiting
- circuit breakers
- bulkheads
- backpressure
- DLQ handling

If a consumer depends on a less reliable system than Kafka itself, that is where most of your operational pain will come from.

## The Most Important Design Habit

Before building, write a short one-page decision record with:

- event type
- latency target
- loss tolerance
- duplicate tolerance
- expected peak throughput
- consumers of the topic
- retention target
- external dependencies

That one artifact will prevent a surprising number of bad production defaults.

## Part 2: Capacity Planning

Capacity planning is really four questions:

1. how many partitions?
2. how many consumer instances?
3. how much consumer concurrency?
4. how much broker disk?

## How to Think About Partition Count

Partition count is the concurrency ceiling for a consumer group.

Start with:

```text
peak records/sec
divided by
records/sec one consumer instance can safely process
```

Then add headroom.

Do not design for "just enough."

You are planning for:

- spikes
- replays
- consumer restarts
- uneven load across partitions

### Important Reality

More partitions help scaling, but they are not free.

They also increase:

- metadata overhead
- rebalance cost
- broker file usage
- operational complexity

So the right question is not:

```text
"How many partitions can I get away with?"
```

It is:

```text
"How many partitions give me enough scale headroom without unnecessary operational cost?"
```

## How to Think About Consumer Concurrency

Consumer concurrency can come from:

- more consumer instances
- more partitions
- more worker threads inside each instance

Use more consumer instances when:

- you need more parallel partition ownership

Use more worker-level concurrency when:

- processing is I/O-bound
- one partition's work can be handled concurrently safely

Be careful:

- more concurrency does not automatically mean more safety
- commit logic and backpressure complexity rise quickly

## How to Think About Disk

Disk planning depends on:

- ingest rate
- average message size
- retention duration
- replication factor

Very rough mental model:

```text
storage needed
= records/sec * avg bytes/record * seconds retained * replication factor
```

The exact math varies, but the key lesson is:

retention and replication multiply cost much faster than people expect

This is why long replay windows often push teams toward:

- Kafka for short-to-medium-term buffer
- object storage / warehouse / lakehouse for long-term history

## A Worked Decision Example

Imagine:

- 20,000 events/sec peak
- each event around 1 KB
- 7 day replay window
- consumer work is I/O-bound
- each worker handles about 10 events/sec safely
- strong durability required

Production implications:

- producer should use strong durability settings
- topic likely needs substantial partitions for future scaling
- consumer likely needs concurrency plus backpressure
- disk footprint will be large because:
  - high ingest
  - long retention
  - replication

This is exactly why Kafka architecture should be designed from workload shape, not from copy-pasted defaults.

## Part 3: What You Must Monitor

A Kafka system without good monitoring is not a production system.

It is a future incident with poor visibility.

## 1. Lag

This is the single most important consumer-side metric.

Track:

- lag by consumer group
- lag by partition
- lag by priority lane if you use tiered architecture

Why it matters:

- lag tells you whether consumers are keeping up
- per-partition lag reveals hot partitions and skew
- lag relative to retention tells you how close you are to data loss

### Intuition

Lag is Kafka's way of answering:

```text
"How much unfinished work is piling up behind me?"
```

## 2. Producer Success Rate and Delivery Latency

Track:

- send success/failure rate
- delivery acknowledgment latency

Why it matters:

- rising failures suggest connectivity, ISR, broker, or disk problems
- rising latency suggests broker stress or replication delays

This is the producer-side equivalent of lag visibility.

## 3. Consumer Processing Time Distribution

Do not rely only on averages.

Track:

- P50
- P95
- P99

Why it matters:

- long-tail processing drives rebalances and lag much more than the average does
- if P99 approaches `max.poll.interval.ms`, you are living dangerously

## 4. Rebalance Frequency

Track how often consumer groups rebalance.

Healthy systems:

- rebalance during deployments or scaling events

Unhealthy systems:

- rebalance repeatedly for unclear reasons

Frequent rebalances often point to:

- crashing consumers
- overly aggressive timeouts
- processing taking too long
- network instability

## 5. DLQ Growth

DLQ depth and rate are critical when DLQ exists.

Why it matters:

- a few messages may be normal
- a rising stream means systemic failure

DLQ growth often reveals:

- schema drift
- code regressions
- unexpected dependency behavior
- bad upstream data

## 6. Broker Health

At minimum, watch:

- disk usage
- under-replicated partitions
- offline partitions
- ISR shrink

Why it matters:

- broker disk pressure can stop writes
- ISR problems can break `acks=all` behavior
- replication health directly affects durability and availability

## 7. Lane-Specific Metrics if You Use Priority Architecture

If you built priority lanes, monitor them separately.

Track:

- high-priority lag
- high-priority processing latency
- routing percentage into high-priority lane
- saturation of high-priority consumers

Otherwise you cannot tell whether the fast lane is really protected.

## Part 4: The Production Failure Scenarios You Should Expect

Do not think in terms of "if something goes wrong."

Think in terms of:

```text
"When this known class of failure happens, what will I check first?"
```

That is what calm production response looks like.

## Scenario 1: Lag Suddenly Grows

This is one of the most common Kafka incidents.

### First Questions

- Is lag growing on all partitions or just one?
- Did processing time increase?
- Did a deployment just happen?
- Did a downstream dependency slow down?
- Did a consumer instance disappear?

### If One Partition Is Much Worse

Think:

- hot partition
- hot key
- stuck consumer instance
- skewed workload

### If All Partitions Are Worse

Think:

- downstream slowdown
- underprovisioned consumers
- sudden traffic spike
- global regression in processing code

## Scenario 2: Producer Failures Spike

### First Questions

- Are brokers healthy?
- Is disk full anywhere?
- Are partitions under-replicated?
- Is `min.insync.replicas` being violated?
- Did a leader election just happen?

This is where strong producer and broker metrics matter.

## Scenario 3: Rebalances Happen Repeatedly

### First Questions

- Are consumers crashing?
- Is `max.poll.interval.ms` too low for actual processing time?
- Is session timeout too aggressive?
- Did a rollout destabilize membership?
- Is there network instability between consumers and brokers?

This is where Chapter 4's timing settings become very operational very quickly.

## Scenario 4: DLQ Starts Filling Quickly

### First Questions

- What error category dominates?
- Are failures retriable or non-retriable?
- Did upstream schema or payload change?
- Did consumer code change recently?
- Did one dependency begin returning bad responses?

DLQ growth is often not "a few bad records."
It is often an early warning of systemic breakage.

## Scenario 5: A Broker Goes Down

### First Questions

- Are leaders re-electing successfully?
- Are producers still able to satisfy safe-write requirements?
- Are any partitions offline?
- How much durability headroom is left?

This is where topic replication and producer `acks` settings become directly relevant.

## Part 5: Schema Evolution Is a Production Problem, Not Just a Serialization Problem

Many Kafka failures are really schema failures in disguise.

Why?

Because Kafka stores records for time.

That means:

- old records still exist
- new consumers may read old data
- old consumers may still see newer data if compatibility is broken

## Safe Evolution Habits

Safer changes:

- add optional fields
- make consumers tolerant before producers rely on new fields

Dangerous changes:

- remove fields immediately
- rename fields casually
- change types without compatibility planning

The core rule is:

**consumer compatibility must be designed across time, not just across services**

That is why schema discipline matters so much in event systems.

## Part 6: The Production Decision Framework

If you need a short framework to apply before building or changing a Kafka system, use this checklist.

## Step 1: Define the Workload

- event type
- peak throughput
- message size
- ordering requirements
- latency target
- replay window

## Step 2: Define the Failure Tolerance

- can records be lost?
- can records be duplicated?
- what happens if processing is delayed?

## Step 3: Define the Consumers

- how many independent services read the topic?
- do they need different service levels?
- do any require priority isolation?

## Step 4: Define the Processing Shape

- CPU-bound or I/O-bound?
- sequential or concurrent?
- which external dependencies exist?

## Step 5: Define Resilience Needs

- rate limiting?
- backpressure?
- circuit breaker?
- bulkhead?
- DLQ?

## Step 6: Define Observability

- lag alerts
- producer success/failure
- processing latency
- rebalance frequency
- broker health
- DLQ growth

## Step 7: Define Recovery

- replay approach
- retention sufficiency
- schema compatibility plan
- incident response expectations

If you cannot answer those seven steps clearly, the system is not fully designed yet.

## Part 7: How to Talk About Kafka in Interviews

A strong Kafka interview answer is rarely just a definition.

The difference between a beginner answer and a production answer is trade-off awareness.

### Weak Answer

```text
"Partitions increase parallelism."
```

### Stronger Answer

```text
"Partitions increase parallelism, but they also increase broker overhead,
rebalance complexity, and ordering complexity. The right count depends on
peak throughput, expected scale, and whether the key design risks hot partitions."
```

### Weak Answer

```text
"At-least-once means duplicates can happen."
```

### Stronger Answer

```text
"At-least-once is usually the practical default because it avoids silent loss,
but it pushes responsibility into consumer idempotency and careful commit timing."
```

### Weak Answer

```text
"Use a circuit breaker for failures."
```

### Stronger Answer

```text
"Use a circuit breaker when failures are slow and repeated, because the real damage
is not just failed calls - it is timeout accumulation, resource blockage, and eventual service collapse."
```

That is what production understanding sounds like.

## Final Intuition for the Whole Series

If you remember only a handful of ideas from all seven chapters, remember these:

- Kafka is a durable event log, not just a queue
- partitions create parallelism but constrain ordering
- offsets are consumer progress, not just record IDs
- retention is a replay window, not a promise to wait for slow consumers
- producer settings define how safely and efficiently data enters Kafka
- consumer settings define how safely and efficiently work is completed
- priority is created through workload separation, not broker magic
- resilience patterns should be chosen by failure mode, not by fashion
- lag is the main truth signal for whether the system is keeping up

## The Production Mindset

The deepest Kafka skill is not memorizing config names.

It is learning to think in questions like:

- what failure am I protecting against?
- what trade-off am I making?
- what signal will tell me this choice is breaking down?

That is the mindset that makes Kafka manageable in production.

---

This completes the seven-chapter Kafka documentation series.

The chapters now build intentionally:

- Chapter 1: why Kafka exists
- Chapter 2: what its core building blocks are
- Chapter 3: how producers work and how to configure them
- Chapter 4: how consumers work and how to configure them
- Chapter 5: how to build real priority architectures
- Chapter 6: how to apply resilience patterns selectively
- Chapter 7: how to design, operate, and debug Kafka systems in production

For fast recall after reading the series, use the companion reference docs in this folder:

- `appendix-kafka-production-configurations.md` for decision-oriented config lookup
- `kafka-quick-reference-cheatsheet.md` for a compact visual refresher
