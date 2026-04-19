# Chapter 6: Resilience Patterns - Keeping Kafka Consumers Stable in the Real World

## Why Resilience Patterns Exist

A Kafka pipeline may look healthy in development and still break badly in production.

Why?

Because production introduces all the ugly realities at once:

- external APIs slow down
- databases deadlock
- one message is malformed
- one tenant floods the system
- a dependency goes down
- a consumer keeps taking work faster than it can finish it
- retries multiply the original problem

Kafka itself is only one part of the system.

Most failures happen not because Kafka stopped storing records, but because the application around Kafka could not absorb real-world instability.

That is why resilience patterns matter.

They exist to answer questions like:

- how do I stop a slow dependency from taking down the consumer?
- how do I stop one bad record from blocking everything behind it?
- how do I stop myself from consuming faster than I can process?
- how do I fail fast instead of timing out forever?
- how do I protect downstream systems from being overwhelmed?

The key principle is:

**each resilience pattern exists to solve a specific failure mode**

If you do not know the failure mode, you should not apply the pattern blindly.

## The Wrong Way to Think About Resilience

The wrong mindset is:

```text
"Production sounds scary, so let me enable every resilience pattern everywhere."
```

That creates:

- complexity
- operational confusion
- harder debugging
- duplicated logic
- new failure paths

The right mindset is:

```text
"What specific failure am I trying to survive, and what is the smallest pattern that addresses it?"
```

That is how good resilience design works.

## The Six Core Failure Modes This Chapter Covers

This chapter focuses on six common failure modes:

1. downstream service is overwhelmed
2. consumer itself is overwhelmed
3. one bad integration consumes all resources
4. a dependency is clearly down but calls keep piling up
5. the consumer is processing data it should not even touch
6. one poison record blocks all later records

Those map to six resilience patterns:

1. rate limiting
2. backpressure
3. bulkheads
4. circuit breakers
5. message filtering
6. dead letter queues

## The First Question Before Applying Any Pattern

Ask this first:

**What exactly is failing, and what is the blast radius if I do nothing?**

Examples:

- one API is returning 429 rate limit responses
- thread pool is filling up
- consumer lag is rising because DB writes slowed down
- a malformed record causes infinite retry on one partition
- a multi-tenant topic is exposing irrelevant or sensitive data to the wrong consumer

Once that is clear, the right pattern usually becomes much more obvious.

## Pattern 1: Rate Limiting

## What Failure Does It Solve?

Rate limiting solves this failure:

**your consumer can call a downstream dependency faster than that dependency can safely handle**

Kafka may be fast.
Your consumer may be fast.
The dependency you call might not be fast enough.

Example:

- consumer reads 10,000 records per second
- fraud API can handle 500 calls per second

Without protection:

- the consumer floods the API
- the API returns errors or timeouts
- retries make the API even more overloaded
- the whole pipeline degrades

## What the Pattern Does

Rate limiting puts a speed governor in front of downstream work.

The consumer still reads from Kafka eventually, but it does not call the downstream service faster than the downstream can sustain.

The natural result is:

- consumer lag grows
- downstream stays alive
- Kafka acts as the buffer

That is an important mindset shift:

**sometimes growing lag is healthier than crushing a dependency**

## The Intuition

Rate limiting answers:

**"How do I slow myself down before I break the thing I depend on?"**

## When to Use It

Use rate limiting when:

- an external API has a known capacity or contractual rate limit
- a downstream service degrades badly under burst load
- your consumer can outpace the dependency by a large margin

## When Not to Use It

Do not add it automatically when:

- processing is entirely local/in-memory
- downstream capacity is not actually a bottleneck
- the true problem is unbounded in-flight work inside your own consumer

That last case is a backpressure problem, not a rate-limiter problem.

## Common Rate-Limiting Styles

### Token Bucket

Good when:

- short bursts are okay
- sustained overload is not

Example:

```text
rate = 500/sec
burst = 1000
```

This lets the system absorb short spikes while still enforcing an average ceiling.

### Sliding Window

Good when:

- you need stricter period-based control
- a provider says "you may make at most X calls per second"

## The Production Decision

Use a rate limiter when the dependency's safe throughput is lower than your consumer's potential throughput.

Do not set the rate exactly at the dependency's theoretical maximum.

Leave headroom.

A good mental rule:

- aim below hard capacity, not at it

## Pattern 2: Backpressure

## What Failure Does It Solve?

Backpressure solves this failure:

**your consumer is taking in work faster than it can finish work**

This is often a concurrent-consumer problem.

The consumer polls aggressively, dispatches work to threads or async tasks, and then:

- in-flight work grows
- memory usage grows
- queues grow
- processing falls behind
- eventually the consumer crashes

That is not a downstream rate-limit problem.
That is an internal capacity-control problem.

## What the Pattern Does

Backpressure slows or pauses intake when the consumer is already too busy.

This can mean:

- pausing new dispatch
- pausing partitions
- polling less aggressively
- waiting for in-flight work to drain

In Kafka terms, backpressure says:

```text
"Leave the records in Kafka for a while. I cannot safely take more right now."
```

## The Intuition

Backpressure answers:

**"How do I stop consuming more work when I am already full?"**

## Why Kafka Works Well with Backpressure

Kafka is naturally good at this because:

- the data stays in Kafka until the consumer is ready
- lag is a visible signal
- the broker can act as a buffer

That is much safer than letting work explode inside application memory.

## When to Use It

Use backpressure when:

- you have concurrent processing
- in-flight work can grow faster than it drains
- memory pressure is a real risk
- one slow dependency can cause queue buildup

## When Not to Use It

You often need less explicit backpressure when:

- processing is strictly sequential
- the consumer is naturally self-limiting

## Common Backpressure Responses

### Blocking / Pause Intake

Safest for:

- systems where records must not be dropped

Trade-off:

- lag grows

### Throttling

Useful when:

- you want a gentler slowdown rather than a hard stop

Trade-off:

- more nuanced behavior, but slower recovery sometimes

### Dropping Lower-Value Work

Only acceptable when:

- low-priority work is explicitly disposable

This is dangerous and must never be used casually.

### Scaling Out

Useful when:

- the platform supports autoscaling
- the bottleneck is simply lack of workers

But scaling takes time, so it is not instant protection.

## The Production Decision

Any concurrent consumer should have an intentional answer to:

```text
"What happens when my worker pool is full?"
```

If the answer is "we keep polling anyway," that is a design gap.

## Pattern 3: Bulkhead

## What Failure Does It Solve?

Bulkheads solve this failure:

**one bad dependency consumes all shared processing resources and starves unrelated work**

Imagine one consumer pipeline calls:

- payment gateway
- email service
- database

If all three share the same thread pool, and the payment gateway becomes very slow:

- payment work fills the pool
- email work stops
- database work stops
- one failing dependency degrades everything

## What the Pattern Does

Bulkhead isolation gives different work types separate resource pools.

That means:

- one integration can become slow
- but it cannot consume all threads or slots needed by other integrations

This is exactly like watertight compartments in a ship:

- one compartment floods
- the whole ship does not sink

## The Intuition

Bulkheads answer:

**"How do I stop one bad dependency from taking all of my shared capacity?"**

## When to Use It

Use bulkheads when:

- one consumer talks to multiple external systems
- those systems have different reliability/performance profiles
- one slow integration should not starve all other work

## When Not to Use It

Do not introduce bulkheads for:

- simple in-memory logic
- a tiny consumer with one downstream call and no shared resource contention

If there is no meaningful shared-capacity conflict, the pattern may add unnecessary complexity.

## The Production Decision

Bulkheads are especially valuable when a consumer does multiple kinds of work in the same process and those kinds of work have different failure behaviors.

The sizing question is:

```text
"How much capacity can this dependency consume before I want new work to fail fast instead of crowding out everything else?"
```

## Pattern 4: Circuit Breaker

## What Failure Does It Solve?

Circuit breakers solve this failure:

**a dependency is clearly unhealthy, but the consumer keeps trying it anyway and burns timeouts repeatedly**

Without a circuit breaker:

- each message triggers a call
- every call times out slowly
- worker threads remain blocked
- lag rises
- timeouts pile up
- recovery is delayed because the failing dependency keeps getting hammered

## What the Pattern Does

The circuit breaker watches failure behavior.

When failure crosses a threshold:

- it opens
- future calls fail immediately
- threads are freed
- the dependency is given time to recover

Later it tries a small test request:

- if successful, it closes
- if not, it opens again

## The Intuition

Circuit breakers answer:

**"How do I stop spending 30 seconds proving, over and over, that something is still broken?"**

## When to Use It

Use circuit breakers when:

- downstream failures are slow and expensive
- timeouts would otherwise tie up threads or async slots
- a dependency is expected to go hard-down sometimes

## When Not to Use It

Do not use one reflexively when:

- the work is extremely fast
- failures are rare and cheap
- the complexity would not materially reduce blast radius

## Circuit Breaker vs Bulkhead

These patterns are related but not the same.

Bulkhead asks:

- how much capacity can this dependency consume?

Circuit breaker asks:

- should I even try calling this dependency right now?

You often use both together.

### Combined Intuition

- bulkhead protects capacity
- circuit breaker protects time

## The Production Decision

If a dependency can fail in a way that causes lots of slow timeouts, you should seriously consider a circuit breaker.

If a dependency can fail but quickly returns an error and does not consume much resource, a circuit breaker may be less important.

## Pattern 5: Message Filtering

## What Failure Does It Solve?

Message filtering solves this failure:

**the consumer is processing records it should never have processed in the first place**

This is less about runtime instability and more about:

- wasted work
- isolation failures
- compliance risk
- multi-tenant leakage

Example:

- a tenant-specific consumer receives a shared topic
- without filtering, it reads all tenants' data

That wastes resources and may create a serious data boundary violation.

## What the Pattern Does

Filtering discards irrelevant or disallowed records before deeper processing.

Examples:

- tenant filtering
- priority filtering
- compliance filtering
- session-based filtering

## The Intuition

Filtering answers:

**"How do I avoid spending resources on data that should not enter this pipeline at all?"**

## When to Use It

Use filtering when:

- one topic carries mixed data
- tenant isolation matters
- compliance boundaries matter
- only a subset of events is relevant to this consumer

## When Not to Use It

If a topic is always meant for exactly one type of consumer and one type of data:

- clearer topic design is usually better than heavy filtering

Filtering should not become a lazy replacement for good topic modeling.

## The Production Decision

Use filtering when shared topics are intentional and the boundary is meaningful.

Do not use filtering just because the topic taxonomy is sloppy and no one wants to clean it up.

## Pattern 6: Dead Letter Queue (DLQ)

## What Failure Does It Solve?

DLQs solve this failure:

**one poison record keeps failing and blocks all later records behind it**

This is especially dangerous in ordered partition processing.

If offset 102 keeps failing forever:

- offset 103 never gets processed
- offset 104 never gets processed
- one bad record effectively freezes the partition

## What the Pattern Does

A DLQ gives the system a place to move permanently unprocessable records after policy says:

```text
"We have tried enough. This record needs separate handling."
```

Then the consumer can:

- record the failure
- move the poison message aside
- commit past it
- continue processing later healthy records

## The Intuition

DLQ answers:

**"How do I stop one impossible record from blocking all the possible ones behind it?"**

## Retriable vs Non-Retriable Failures

This distinction is critical.

### Retriable Failures

Examples:

- timeout
- temporary DB lock
- transient network issue

Retry may help.

### Non-Retriable Failures

Examples:

- malformed payload
- schema mismatch
- validation failure
- forbidden business rule state

Retry will not help.

That is why naive retry loops are dangerous.

They waste time, increase lag, and still do not make progress.

## When to Use a DLQ

Use a DLQ when:

- upstream data is not fully trusted
- malformed or invalid records are possible
- partition blockage would be costly
- there is an operational process for reviewing failed records

## When Not to Use a DLQ

In a tightly controlled internal system with:

- fully trusted producers
- strict schemas
- low chance of malformed events

the need may be lower.

But even then, many teams still prefer DLQ capability because real systems eventually drift.

## The Production Decision

A DLQ is not just a topic.

It is a workflow:

- how many retries?
- which errors go straight to DLQ?
- who monitors DLQ growth?
- how are records replayed after a fix?

If you do not have answers to those questions, you do not yet have a complete DLQ design.

## How These Patterns Interact in One Consumer

These patterns are not isolated islands.

They often work together.

Example:

- rate limiter protects external API capacity
- backpressure protects internal consumer memory
- bulkhead isolates one integration from another
- circuit breaker avoids repeated slow failures
- DLQ handles poison records

That combination can be reasonable in a complex consumer.

But not every consumer needs every pattern.

## Example: External Enrichment Consumer

Imagine a consumer that:

- reads user events from Kafka
- calls an external enrichment API
- writes enriched records to a database

Possible failures:

- enrichment API has hard rate limit
- enrichment API sometimes goes down
- DB becomes slow
- some records are malformed
- concurrent processing queue grows

Reasonable resilience choices:

- rate limiter for enrichment API
- backpressure for in-flight queue
- circuit breaker for enrichment API
- bulkhead if DB writes and API calls share process capacity
- DLQ for malformed records

That is a justified layered design.

## Example: Simple Internal State Consumer

Imagine a consumer that:

- reads internal order events
- updates one internal database table

Possible failures are narrower.

Reasonable design may be:

- manual commit
- idempotent DB writes
- maybe DLQ if schema drift is possible

Probably unnecessary:

- elaborate bulkheads
- aggressive rate limiting
- multiple layers of resilience around one stable internal DB

That is why resilience should be calibrated, not maximized blindly.

## The Pattern Selection Framework

Use this quick mapping.

### Use Rate Limiting when:

- you can overload a dependency faster than it can safely process calls

### Use Backpressure when:

- your consumer can intake work faster than it can finish work

### Use Bulkheads when:

- one dependency can starve unrelated work by consuming shared capacity

### Use Circuit Breakers when:

- a known-bad dependency would otherwise burn timeouts repeatedly

### Use Message Filtering when:

- some data should not enter this consumer pipeline at all

### Use DLQ when:

- one permanently bad record could block the partition or poison the pipeline

## The Common Mistake: Applying All Patterns Everywhere

This is one of the biggest reliability mistakes.

Why?

Because resilience patterns are not free.

They add:

- configuration
- state
- edge cases
- debugging complexity
- operational behavior that someone must understand at 3 AM

The goal is not:

```text
"maximum number of patterns"
```

The goal is:

```text
"minimum set of patterns needed to survive the real risks of this consumer"
```

## The Intuition to Carry Forward

If you remember only a few lines from this chapter, remember these:

- rate limiting protects downstream capacity
- backpressure protects your own consumer capacity
- bulkheads protect shared resources from one bad dependency
- circuit breakers protect time by failing fast when something is clearly broken
- filtering protects boundaries by rejecting irrelevant or unsafe records early
- DLQ protects throughput by removing poison records from the main path

The strongest resilience designs are not the ones with the most patterns.

They are the ones where every pattern has a clear reason to exist.

---

**What comes next:** Chapter 7 brings everything together into a production decision framework: how to size Kafka systems, what to monitor, what questions to ask during design, and how to debug the incidents you will eventually face in production.
