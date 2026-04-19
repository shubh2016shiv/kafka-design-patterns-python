# Chapter 5: Priority Architecture - Turning Business Urgency into Kafka Design

## Why "Priority" in Kafka Is Often Misunderstood

When people first hear "high-priority events" and "low-priority events," they often imagine a simple broker-level switch:

```text
priority=high
```

and Kafka magically processes those records first.

Kafka does not work that way.

Kafka preserves order within partitions. It does not look inside a partition and reshuffle records based on urgency.

That means:

- Kafka does not have a native "skip the line" feature inside a partition
- if urgent and non-urgent records are mixed in the same partition, they still flow in order

So in Kafka, priority is not something you attach to a record and expect the broker to honor automatically.

**Priority in Kafka is an architecture, not a flag.**

You create priority by separating workloads and giving those workloads different:

- topics
- partitions
- producer routing rules
- consumer resources
- durability settings
- retention settings
- alert thresholds

That is what this chapter is about.

## The First Question: Do You Really Need Priority Tiers?

Before designing a high-priority and low-priority setup, ask:

**Do these events actually need meaningfully different service levels?**

Sometimes the answer is yes.

Examples:

- fraud alerts must be acted on in under 200ms
- analytics export can wait minutes
- user-facing search index update should be fast
- nightly reporting can be slow

Sometimes the answer is no.

Examples:

- all events have similar urgency
- all consumers already keep up comfortably
- the "priority" idea is just a guess rather than a measured need

### When Priority Tiers Are Worth It

Priority architecture is justified when different workloads have meaningfully different needs for:

- latency
- throughput
- durability
- recovery window
- consumer resource allocation

### When Priority Tiers Are Overengineering

You probably do not need tiers when:

- event volume is low
- all events have similar urgency
- one well-sized topic and one consumer design is enough
- the operational overhead of multiple topics would outweigh the benefit

### The Core Decision

Priority tiers are worth adding only when:

**letting all records share the same topic and consumer capacity would cause important work to suffer behind less important work**

That is the real test.

## What Priority Actually Means in Kafka

In Kafka, priority means:

**important work gets separate resources, separate expectations, and separate failure boundaries**

Not just separate labels.

If both high-priority and low-priority events:

- go to the same topic
- share the same partitions
- share the same consumers
- share the same lag and failure modes

then you do not actually have a priority architecture.

You just have a naming convention.

## The Simplest Useful Mental Model

Think of a Kafka priority architecture as separate roads.

```text
Without priority separation:

all vehicles use one road
ambulances, buses, trucks, and bicycles all queue together


With priority separation:

critical traffic gets a fast lane
background traffic gets a normal lane
```

In Kafka terms:

- high-priority lane = dedicated topic(s) + faster consumers + stronger guarantees
- low-priority lane = separate topic(s) + lower-cost processing + more relaxed SLAs

## The Five Levers That Create Real Priority

Priority becomes real only when you change actual system behavior.

The most important levers are:

1. routing
2. partition count
3. consumer allocation
4. durability configuration
5. retention and replay window

We will walk through each one.

## 1. Routing: Which Events Go to Which Lane?

This is the most important decision.

If routing is wrong, the whole architecture is wrong.

Ask:

**What business rule decides whether an event belongs in the fast lane or the normal lane?**

Example routing rules:

- customer tier is VIP
- payment amount is above threshold
- fraud score is above threshold
- user action is interactive rather than batch
- event type is operationally urgent

### Good Routing Rules

Good routing rules are:

- easy to explain
- easy to measure
- stable over time
- tied to actual business urgency

Example:

```text
if event_type in {"fraud_alert", "payment_authorization"}:
    route to high-priority topic
else:
    route to standard topic
```

### Bad Routing Rules

Bad routing rules are:

- vague
- inconsistent
- constantly changing
- impossible to validate with metrics

Example:

```text
if message_feels_important:
    route to high-priority
```

That kind of routing rule creates operational chaos.

### The Biggest Routing Failure Modes

#### Failure Mode 1: Too Many Records Go High Priority

If almost everything ends up in the high-priority lane:

- the fast lane becomes the normal lane
- high-priority consumers overload
- lag rises
- urgent work loses its protection

#### Failure Mode 2: Too Few Records Go High Priority

If almost nothing qualifies:

- expensive high-priority capacity sits mostly idle
- urgent records may still get routed too late or inconsistently

### Intuition

Routing answers:

**"What deserves special treatment badly enough that I am willing to isolate capacity for it?"**

## 2. Partition Count: How Much Scale Does Each Lane Need?

Partition count is the throughput and concurrency ceiling for each topic.

From Chapter 2:

- more partitions = more parallelism
- fewer partitions = lower parallelism, simpler behavior

This matters a lot in a priority architecture.

### High-Priority Topics

High-priority lanes often need:

- enough partitions to scale out consumers aggressively
- enough headroom for traffic spikes
- low queueing delay

### Low-Priority Topics

Low-priority lanes often need:

- fewer partitions
- simpler operations
- lower infrastructure cost
- enough throughput to eventually clear work, not necessarily instantly

### Example

```text
high-priority topic:
12 partitions
can actively use up to 12 consumers in one group

low-priority topic:
3 partitions
can actively use up to 3 consumers in one group
```

This is a real priority distinction because the high-priority lane can absorb more parallel consumer capacity.

### Intuition

Partition count answers:

**"How much parallel work capacity am I reserving for this lane?"**

## 3. Consumer Allocation: Who Actually Gets the Compute?

This is where many teams accidentally fake priority.

They create two topics:

- `payments-high`
- `payments-low`

but both are still read by the same underpowered consumer design.

That is not real priority isolation.

Priority becomes real when the consumer side is intentionally different.

### High-Priority Consumers Often Have

- more instances
- more worker threads if work is I/O-bound
- tighter alerting
- stricter latency SLOs
- stronger on-call visibility

### Low-Priority Consumers Often Have

- fewer instances
- lower concurrency
- more relaxed lag tolerance
- more batch-oriented behavior

### Example

```text
high-priority lane:
6 consumer instances
8 worker threads each
aggressive autoscaling
strict lag alerts

low-priority lane:
2 consumer instances
1 worker thread each
batch-oriented processing
relaxed lag alerts
```

Now the system actually behaves differently under load.

### Intuition

Consumer allocation answers:

**"When the system is busy, who gets the workers?"**

## 4. Durability Configuration: Which Lane Gets Stronger Safety Guarantees?

Sometimes high-priority also means high-importance.

That may require stronger producer and topic durability.

Examples:

- `acks=all`
- `enable.idempotence=true`
- `replication.factor=3`
- `min.insync.replicas=2`

Low-priority topics may tolerate:

- weaker replication
- cheaper storage posture
- more relaxed failure semantics

But this must be decided carefully.

### Important Distinction

High-priority does not always mean high-durability.

Some workloads are:

- urgent but disposable

Example:

- real-time live dashboard updates

Other workloads are:

- urgent and critical

Example:

- fraud alerts
- payment authorization events

So do not assume "priority" always maps directly to "maximum durability."

You must ask two separate questions:

- how fast must this be?
- how safe must this be?

### Intuition

Durability settings answer:

**"If this lane fails, how much loss am I willing to accept?"**

## 5. Retention and Replay Window: How Long Must Each Lane Stay Recoverable?

Priority lanes often differ in how much replay history matters.

### High-Priority Topics

High-priority topics often contain records that are operationally urgent right now.

Sometimes that means:

- shorter retention is acceptable
- replay need is limited
- low disk footprint matters

### Low-Priority Topics

Low-priority topics often feed:

- analytics
- batch processing
- delayed consumers
- backfills
- new service bootstrap

That often means:

- longer retention is useful

### Example

```text
high-priority topic:
retention.ms = 1 day

low-priority topic:
retention.ms = 7 days
```

This gives:

- tighter operational footprint for urgent records
- larger replay window for slower consumers and backfills

### Important Caveat

Short retention on a high-priority topic means lag becomes dangerous faster.

If the high-priority consumer is down for longer than the retention window:

- the urgent data may be gone before recovery

So shorter retention is only safe if:

- monitoring is strong
- lag alerts are tight
- recovery expectations are realistic

### Intuition

Retention answers:

**"How long should this lane remain recoverable from Kafka alone?"**

## What a Real Two-Tier Architecture Looks Like

Here is a practical example for a payment system.

## Tier 1: High-Priority

Use for:

- fraud alerts
- payment authorization
- VIP transactions
- real-time balance-affecting events

Typical design:

```text
topic: payments-high
partitions: 12
replication.factor: 3
min.insync.replicas: 2
retention: 1 day

producer:
acks=all
enable.idempotence=true

consumer:
many instances
concurrent processing if I/O-bound
tight lag alerting
tight processing SLOs
```

## Tier 2: Standard / Low-Priority

Use for:

- analytics export
- reporting
- non-urgent notification pipelines
- delayed enrichment jobs

Typical design:

```text
topic: payments-standard
partitions: 3
replication.factor: 2 or 3 depending value
retention: 7 days

producer:
safe or throughput-oriented depending business value

consumer:
fewer instances
possibly sequential or lower-concurrency
more relaxed lag thresholds
```

The point is not that every system must use exactly these numbers.

The point is that the high-priority lane must have meaningfully different resources and expectations.

## What Priority Architecture Looks Like End to End

```text
incoming business event
        |
        v
producer routing logic decides lane
        |
        +--> high-priority topic
        |       |
        |       +--> stronger producer guarantees
        |       +--> more partitions
        |       +--> more consumer capacity
        |       +--> tighter lag/SLA alerts
        |
        +--> standard topic
                |
                +--> lower-cost processing path
                +--> fewer partitions
                +--> fewer consumers
                +--> longer acceptable lag
```

That is how Kafka priority becomes real.

## The Questions You Must Answer Before Creating Priority Tiers

Before implementing a tiered architecture, answer these clearly.

### 1. What exactly qualifies for the fast lane?

If this is fuzzy, stop and define it before building anything.

### 2. How much traffic will the fast lane actually carry?

If high-priority is 80% of all traffic, it is not really a fast lane anymore.

### 3. What latency or SLO does each lane need?

You need actual numbers:

- under 200ms
- under 2 seconds
- under 1 minute

Not vague statements like "pretty fast."

### 4. What consumer capacity does each lane need at peak?

This decides partitions and worker allocation.

### 5. What durability does each lane require?

Fast and durable are separate dimensions.

### 6. How much replay window does each lane need?

This decides retention strategy.

### 7. How will you know if the fast lane is being polluted by low-value traffic?

You need observable routing metrics.

## How to Know If the Priority Architecture Is Actually Working

A priority architecture should be observable.

Otherwise you cannot tell whether the fast lane is real or imaginary.

You should monitor at least:

- volume per lane
- lag per lane
- lag per partition within the high-priority lane
- processing latency per lane
- routing percentages
- high-priority consumer saturation
- DLQ growth by lane

### Healthy Signals

Signs the architecture is working:

- high-priority lag stays low under normal load
- high-priority SLA is consistently met
- low-priority lag can grow without harming urgent work
- high-priority traffic percentage stays in the expected range

### Broken Signals

Signs the architecture is not working:

- high-priority lag spikes whenever normal traffic grows
- most traffic ends up in the high-priority lane
- consumers for both lanes are equally overloaded
- routing logic is hard to explain
- on-call cannot tell which lane is failing

## The Most Common Priority Architecture Mistakes

## Mistake 1: Same Topic, Same Consumers, Different Labels

This is fake priority.

If the records still share the same partitions and consumers, urgent work is not isolated.

## Mistake 2: Everything Becomes High Priority

This collapses the architecture.

The fast lane must remain scarce enough to stay fast.

## Mistake 3: Durability and Latency Get Mixed Up

Urgent does not always mean critical.
Critical does not always mean ultra-low-latency.

Keep those dimensions separate.

## Mistake 4: Too Many Tiers

If you have:

- ultra-high
- high
- medium-high
- medium
- medium-low
- low

you probably built a taxonomy problem, not a Kafka solution.

Most systems need:

- one lane, or
- two lanes, or
- at most three meaningful lanes

## Mistake 5: No Validation of Routing Logic

If you cannot answer:

- what percentage of traffic is routed high priority?
- which event types dominate the high-priority lane?
- when did that distribution change?

then the architecture is under-observed.

## When Two Tiers Are Enough

Two tiers are enough for most systems:

- urgent
- not urgent

This is especially true when the business distinction is clear:

- real-time operational events
- everything else

## When Three Tiers Make Sense

A third tier can make sense when you truly have three distinct service levels.

Example:

- real-time fraud detection: under 100ms
- normal payment processing: under 2 seconds
- batch reconciliation: under 24 hours

Those are genuinely different lanes.

But add a third tier only when the operational difference is real and measurable.

## When Kafka Priority Is the Wrong Tool

Sometimes the right answer is not more Kafka tiers.

If the real problem is:

- scheduled batch orchestration
- workflow dependencies
- long-running jobs
- cron-like timing

then a job scheduler or workflow system may be a better fit than trying to encode everything as Kafka priority lanes.

Kafka is best when the core problem is:

**different event streams need different throughput, latency, and isolation characteristics**

## The Priority Design Framework

If you are unsure whether to build priority lanes, use this checklist.

### Create separate priority lanes when:

- urgent work must be protected from background work
- SLOs differ materially
- consumer resources must be isolated
- routing rules are explainable and measurable

### Keep one lane when:

- urgency is mostly the same across events
- one consumer architecture can satisfy all needs
- operational simplicity matters more than micro-optimization

### Add a third lane only when:

- the second lane is not enough to represent real SLO differences
- on-call can still reason about the system clearly

## The Intuition to Carry Forward

If you remember only a few lines from this chapter, remember these:

- Kafka does not do in-partition priority scheduling for you
- priority is created by separating workloads and giving them different resources
- routing is the heart of the architecture
- partitions and consumers decide who gets throughput
- durability and latency are separate decisions
- retention decides how recoverable each lane is
- if high-priority traffic is not isolated, it is not really high priority

With that foundation in place, the next step is to make each consumer lane resilient when the real world gets messy:

- downstream slowdowns
- retry storms
- poison messages
- rate limits
- overload

---

**What comes next:** Chapter 6 covers resilience patterns and explains when to apply rate limiting, backpressure, bulkheads, circuit breakers, filtering, and dead letter queues so your Kafka consumers stay stable under production failures.
