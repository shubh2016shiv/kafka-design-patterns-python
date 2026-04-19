# Chapter 2: Core Concepts - Topics, Partitions, Offsets, Brokers, Consumer Groups, and Retention

## Why This Chapter Matters So Much

If Kafka feels confusing, it is usually because these five ideas have not clicked yet:

- topic
- partition
- offset
- consumer group
- retention

Once these become intuitive, Kafka stops feeling mysterious and starts feeling mechanical.

This chapter is intentionally practical. The goal is not just to define the terms, but to answer the real questions engineers ask:

- what does this thing actually look like?
- why does it exist?
- what problem is it solving?
- what production decision depends on it?

## Start with a Topic

A **topic** is the named category under which related events are written.

Examples:

- `orders`
- `payments`
- `user-signups`
- `document-uploads`

If your service publishes order events, it writes them to the `orders` topic. If another service wants to react to orders, it reads from the `orders` topic.

The simplest mental model is:

**a topic is a named stream of related facts**

If Chapter 1 described Kafka as a company-wide event journal, then a topic is one named section of that journal.

```text
Topic examples:

orders            -> all order-related events
payments          -> all payment-related events
document-uploads  -> all upload events
```

### Why Topics Exist

Topics exist because systems produce many different kinds of events, and you need a way to organize them.

Without topics, everything would land in one giant stream:

- orders
- payments
- login events
- fraud alerts
- document uploads

That would be chaos.

Topics let you separate event categories so consumers can subscribe only to what they care about.

### What a Topic Is Not

A topic is not:

- one file
- one queue worker
- one database row set

A topic is made of **partitions**, and that is where Kafka's real behavior starts.

## Partitions: Why Kafka Splits a Topic

This is the concept most people struggle with first, so let us make it visual.

Suppose you have a topic called `orders`.

If `orders` had only one partition, then all events would go into one ordered lane:

```text
orders topic
└── partition 0
    [order_1] [order_2] [order_3] [order_4] ...
```

That is simple, but it has a hard limit:

- only one consumer in a group can actively read that partition at a time
- all writes for that partition go through one leader
- all ordering is forced through one lane

Now imagine `orders` has three partitions:

```text
orders topic
├── partition 0   [msg] [msg] [msg] ...
├── partition 1   [msg] [msg] [msg] ...
└── partition 2   [msg] [msg] [msg] ...
```

Now Kafka can spread the workload.

### The Most Important Partition Insight

**A partition is the unit of parallelism in Kafka.**

That means:

- more partitions = more maximum parallelism
- fewer partitions = simpler ordering, but lower scaling ceiling

If a topic has 3 partitions, then at most 3 consumers in the same consumer group can actively consume it at the same time.

```text
orders topic with 3 partitions

partition 0  ---> consumer A
partition 1  ---> consumer B
partition 2  ---> consumer C
```

If you start 6 consumers in that group:

```text
partition 0  ---> consumer A
partition 1  ---> consumer B
partition 2  ---> consumer C

consumer D   ---> idle
consumer E   ---> idle
consumer F   ---> idle
```

That is why partition count is not an academic detail. It directly controls how far you can scale a consumer group.

### Why Partitions Exist

Partitions exist because Kafka is trying to achieve two things at once:

1. preserve ordering
2. scale horizontally

A single global ordered log is great for simplicity, but terrible for parallel throughput.

Partitions are Kafka's compromise:

- ordering is preserved within each partition
- throughput is increased by having many partitions in parallel

So when someone asks, "Why does Kafka even have partitions?", the answer is:

**because Kafka needs to scale without giving up ordered logs entirely**

### What Decision You Make Because of Partitions

This is the first major design decision in Kafka:

**How many partitions should this topic have?**

You decide partition count based on:

- expected peak throughput
- number of consumer instances you may need
- how much future scaling headroom you want
- how important ordering is for your workload

More partitions help with:

- higher throughput
- more consumer parallelism
- better horizontal scaling

More partitions also cost you:

- more broker metadata
- more open files on brokers
- more rebalancing overhead
- more operational complexity
- more chances of uneven partition load if keys are skewed

So the right mindset is not "more partitions is always better."

It is:

**use enough partitions to meet throughput and scaling needs, but do not inflate them casually**

## How Messages Get Assigned to Partitions

Once a topic has multiple partitions, Kafka must decide where each message goes.

There are three common cases.

### Case 1: Keyed Messages

If you provide a **key**, Kafka hashes that key and routes all messages with the same key to the same partition.

Example:

```text
topic: orders

key=customer_42 -> partition 1
key=customer_99 -> partition 0
key=customer_42 -> partition 1
key=customer_42 -> partition 1
```

That means all events for `customer_42` stay together in one ordered lane.

```text
partition 1
[customer_42 order_created]
[customer_42 payment_authorized]
[customer_42 order_shipped]
```

This is extremely important when event order matters for the same entity.

### Case 2: No Key

If you do not provide a key, Kafka distributes messages across partitions for balancing and throughput.

That gives good load distribution, but it means related messages may land in different partitions.

So you get:

- better balancing
- less ordering guarantee for related entities

### Case 3: Custom Partitioner

Sometimes teams write custom partition logic.

Examples:

- send VIP customers to a specific topic or partitioning scheme
- route by geography
- route by tenant

This is advanced and should only be done when there is a real reason. Most production systems should prefer normal keyed partitioning unless custom routing is clearly needed.

### The Real Decision About Keys

This is where production intuition matters.

Ask:

**Do events for the same entity need to stay in order?**

If yes:

- use a key such as `order_id`, `customer_id`, `account_id`, or `document_id`

If no:

- an unkeyed or throughput-oriented strategy may be fine

Typical choices:

- `order_id` if all order lifecycle events must stay ordered
- `customer_id` if all actions for one customer must stay ordered
- no key if the events are independent and ordering is irrelevant

### The Edge Case That Bites Teams

If one key is much hotter than others, one partition can become overloaded.

Example:

- one tenant produces 80% of the traffic
- all those records hash to one partition

Now you have a **hot partition**:

- one partition lags badly
- others are mostly idle
- consumer scaling does not help much because that one partition is the bottleneck

This is why key design matters. The key determines both ordering and distribution.

## Offsets: The Position Inside a Partition

An **offset** is the position number of a record inside one partition.

Example:

```text
orders topic
partition 1

offset 100 -> order_created
offset 101 -> payment_authorized
offset 102 -> order_shipped
```

Offsets are not global across the whole topic.

They are per-partition.

That means:

- partition 0 has its own offsets: 0, 1, 2, 3...
- partition 1 has its own offsets: 0, 1, 2, 3...
- partition 2 has its own offsets: 0, 1, 2, 3...

### Why Offsets Exist

Offsets solve the question:

**How does a consumer remember where it left off?**

Kafka does not track "message consumed" like a queue deleting items one by one.

Instead, the consumer tracks its position in each partition.

That is much simpler and much more scalable.

```text
partition 1

offset 100 -> processed
offset 101 -> processed
offset 102 -> next to read
```

That means the consumer's position is "start from offset 102."

### What Offset Tracking Looks Like in Practice

Imagine a consumer group called `billing-service`.

It might have:

```text
orders-0 -> committed offset 500
orders-1 -> committed offset 920
orders-2 -> committed offset 301
```

That means:

- for partition 0, it will next resume after 500
- for partition 1, it will next resume after 920
- for partition 2, it will next resume after 301

This is why offsets are such a core Kafka idea: they turn consumption into "progress through a log."

### Position vs Committed Offset

This distinction is important in production.

- **position** = where the consumer has currently read up to in memory
- **committed offset** = the last offset safely stored so the consumer can recover after restart

That means a consumer may have:

- read up to offset 105
- processed up to 104
- committed only 102

If it crashes now, it will restart from 103, not 105.

That is how Kafka trades off between safety and duplicates.

## Why Offset Commit Timing Matters

The key consumer decision is:

**When should I commit the offset?**

### Commit Too Early

```text
read offset 10
commit offset 10
process offset 10
crash during processing
```

On restart, Kafka thinks offset 10 is already done.

Result:

- offset 10 is skipped
- you lost that event

This is the dangerous path.

### Commit After Processing

```text
read offset 10
process offset 10
commit offset 10
```

If the app crashes before the commit:

- Kafka will send offset 10 again on restart

Result:

- no silent loss
- possible duplicate processing

This is why most production consumers are built for **at-least-once** delivery and make processing idempotent.

### Why Offsets Exist Instead of Deleting Messages After Read

Because Kafka is a shared log, not a one-consumer queue.

Multiple consumer groups may all be reading the same topic:

- billing service
- analytics service
- fraud service

Each needs its own progress.

Offsets make that possible cleanly.

## Consumer Groups: How Kafka Scales Readers

A **consumer group** is a set of consumers cooperating as one logical application.

This is another place where beginners often get confused.

Think of it like this:

- one consumer group = one application/service reading a topic
- many consumer instances in the group = horizontal scale for that application

Example:

```text
topic: orders
partitions: 3

consumer group: billing-service

consumer A -> partition 0
consumer B -> partition 1
consumer C -> partition 2
```

Together, those three consumers are one logical billing application.

### Why Consumer Groups Exist

Without consumer groups, you would have two bad choices:

1. every consumer instance reads everything
2. you manually split work across readers

Kafka solves this for you.

Inside one group:

- each partition is assigned to exactly one consumer at a time
- no double-processing within the group
- scaling is automatic through partition assignment

### The Golden Rule of Consumer Groups

**Inside one consumer group, one partition is consumed by only one consumer at a time.**

That is what prevents duplicate work inside the group.

### The Confusion Most Beginners Have

A very common misunderstanding is this:

```text
"If I have one partition and three consumers, won't all three eventually read the event?"
```

The answer depends entirely on whether those three consumers are in the **same consumer group** or in **different consumer groups**.

#### Case 1: Same Consumer Group

If you have:

- one topic partition
- three consumers
- all three in the same consumer group

then only **one** of those consumers will read from that partition.

Example:

```text
topic: orders
partitions: 1
group: billing-service

orders-0 -> consumer A
consumer B -> idle
consumer C -> idle
```

Why?

Because Kafka uses the consumer group to divide work, and one partition can be assigned to only one consumer in that group at a time.

So in this case:

- all three consumers do **not** eventually read the same event
- only the assigned consumer reads it for that group

#### Case 2: Different Consumer Groups

If you have:

- one topic partition
- three consumers
- each consumer in a different consumer group

then all three can read the same partition independently.

Example:

```text
topic: orders
partitions: 1

group billing-service   -> reads orders-0
group analytics-service -> reads orders-0
group fraud-service     -> reads orders-0
```

Now the same event can be read by all three groups, because each group keeps its own offsets and its own progress.

This is the correct mental model:

- partitions control parallelism **within a group**
- consumer groups control independent consumption **across applications**

### Different Consumer Groups Are Independent

Now imagine three different services:

- `billing-service`
- `analytics-service`
- `fraud-service`

All can read the same `orders` topic.

```text
orders topic

billing-service group   -> reads orders for billing
analytics-service group -> reads orders for analytics
fraud-service group     -> reads orders for fraud checks
```

They do not compete with each other.

Each group has:

- its own partition assignments
- its own offsets
- its own lag
- its own failure behavior

This is one of Kafka's biggest strengths.

### Why Consumer Groups Exist Instead of Simple Shared Readers

Because production systems need both:

- independent services reading the same events
- horizontal scale within each service

Kafka consumer groups provide both.

## What an Event Actually Looks Like

Another beginner pain point is that Kafka explanations often say "message" without showing what that message actually contains.

A Kafka event is usually called a **record**, and in practice it often has these pieces:

- topic
- partition
- offset
- key
- value
- timestamp
- optional headers

Here is a realistic example:

```json
{
  "topic": "orders",
  "partition": 1,
  "offset": 102,
  "key": "customer_42",
  "timestamp": "2026-04-19T10:30:00Z",
  "headers": {
    "event_type": "order_created",
    "schema_version": "v1",
    "source": "order-service"
  },
  "value": {
    "order_id": "ord_1001",
    "customer_id": "customer_42",
    "amount": 499.99,
    "currency": "USD",
    "status": "CREATED"
  }
}
```

### What Each Part Means

#### `key`

The key is usually used for partitioning and ordering.

Example:

```text
key = customer_42
```

Meaning:

- Kafka will likely send all records with that key to the same partition
- order for that key is preserved within that partition

#### `value`

The value is the actual business data the application cares about.

Example:

```json
{
  "order_id": "ord_1001",
  "customer_id": "customer_42",
  "amount": 499.99
}
```

#### `headers`

Headers carry metadata that is useful but usually not the main business payload.

Teams often use headers for:

- event type
- schema version
- tenant id
- trace id
- correlation id
- source service

#### `offset`

The offset is the Kafka-assigned position of the record within its partition.

#### `partition`

The partition tells you which ordered lane the record belongs to.

### A GenAI-Flavored Example

Here is what an event might look like in a GenAI pipeline:

```json
{
  "topic": "document-uploads",
  "partition": 2,
  "offset": 88,
  "key": "doc_789",
  "timestamp": "2026-04-19T11:00:00Z",
  "headers": {
    "event_type": "document_uploaded",
    "schema_version": "v2"
  },
  "value": {
    "document_id": "doc_789",
    "user_id": "user_55",
    "file_name": "policy.pdf",
    "storage_url": "s3://bucket/policy.pdf",
    "content_type": "application/pdf"
  }
}
```

Different consumer groups can now react to the same event independently:

- `chunking-service`
- `embedding-service`
- `audit-service`
- `analytics-service`

That is why saying only "message" is too vague. In real systems, an event is a structured record with routing information, metadata, and business payload.

## Rebalancing: What Happens When Group Membership Changes

A **rebalance** happens when Kafka needs to reassign partitions among consumers in a group.

This happens when:

- a consumer starts
- a consumer stops
- a consumer crashes
- a deployment rolls pods in and out
- a consumer is considered dead because it stopped polling

Example:

```text
Before rebalance:

consumer A -> partitions 0, 1
consumer B -> partition 2

After consumer C joins:

consumer A -> partition 0
consumer B -> partition 1
consumer C -> partition 2
```

Rebalancing is necessary, but it is not free.

It can cause:

- temporary pause in consumption
- partition movement between instances
- more operational instability if consumers join/leave too often

That is why stable consumer deployments matter in production.

## Consumer Lag: The Health Signal You Must Watch

**Lag** is how far behind a consumer group is compared to the latest data in Kafka.

Example:

```text
partition latest offset: 1000
consumer group committed offset: 920

lag = 80
```

Lag means:

- 80 records exist that this group has not fully caught up on yet

Lag by itself is not always bad.

It becomes important when:

- it is growing steadily
- it spikes unexpectedly
- one partition lags far more than others
- it approaches the retention window

### Why Lag Exists

Lag exists because producers and consumers work at different speeds.

That is normal.

Kafka is designed to absorb that difference.

### What Lag Tells You

Lag helps diagnose several classes of problems:

- consumer is too slow
- downstream dependency is slow
- one partition is hot
- not enough consumers
- not enough partitions
- processing code regressed after deployment

### Per-Partition Lag Matters More Than Aggregate Lag

If total lag is high, that tells you something is wrong.

If one partition has huge lag and others are fine, that usually tells you something much more specific:

- hot key
- stuck consumer instance
- skewed workload

That is why serious Kafka monitoring looks at lag per partition, not just a grand total.

## Retention: How Long Kafka Keeps Events

Retention is the answer to this question:

**How long will Kafka keep this data before deleting it?**

This is where many beginners get the wrong mental model, so let us be precise.

Kafka does **not** keep data because a consumer group still needs it.

Kafka keeps data according to the topic's retention policy.

That policy is usually based on:

- time
- size
- or compaction rules

### Time-Based Retention

Example:

```text
retention.ms = 604800000
```

That means:

- keep data for 7 days

### Size-Based Retention

Example:

```text
retention.bytes = 10737418240
```

That means:

- keep up to around 10 GB per partition before older segments become eligible for deletion

### Why Retention Exists

Because Kafka is a durable log, but not infinite storage.

If Kafka kept all data forever by default:

- disks would fill
- brokers would stop accepting writes
- costs would explode

Retention is the mechanism that balances:

- replay capability
- recovery safety
- storage cost

### The Production Meaning of Retention

Retention is really a replay window.

If retention is 7 days, then a new consumer or recovering consumer can usually read up to 7 days of history, assuming the data has not been deleted by size-based limits first.

If a consumer falls behind longer than retention:

- old data may be gone
- replay becomes impossible from Kafka
- you may have permanent data loss from the consumer's perspective

That is why retention is not just an infrastructure knob. It is a business risk knob.

### The Key Retention Correction

This statement is wrong and should never guide your mental model:

```text
Kafka keeps data as long as any group needs it
```

Kafka does not watch your consumer groups and preserve messages for them.

Kafka deletes based on configured policy.

So if one slow consumer group is 10 days behind and retention is 7 days:

- Kafka will not wait for that group
- old data can be deleted anyway

This is one of the most important production truths in Kafka.

## Retention and Log Compaction Are Not the Same Thing

Some topics use regular delete-based retention.

That means:

- old data is removed after time or size limits

Other topics use **log compaction**.

That means Kafka keeps the latest known value for each key over time, rather than preserving every historical record forever.

This is useful for topics that behave like a changelog.

Example:

```text
key=user_42 value=email=a@example.com
key=user_42 value=email=b@example.com
key=user_42 value=email=c@example.com
```

With compaction, Kafka may eventually keep only the latest relevant record for `user_42`, plus some recent history depending on timing.

Why this matters:

- normal retention topics are good for event history
- compacted topics are good for restoring latest state by key

You do not need to master compaction yet, but you do need to know it exists because it changes what "retention" means.

## Brokers: The Machines Running Kafka

A **broker** is a Kafka server.

A Kafka cluster is made of multiple brokers.

Example:

```text
broker-1
broker-2
broker-3
```

Partitions are distributed across brokers.

Why?

Because Kafka needs:

- more storage than one machine has
- more throughput than one machine can handle
- resilience if one machine fails

### Leaders and Followers

For each partition:

- one broker is the **leader**
- other brokers may hold follower replicas

All reads and writes for that partition go through the leader. Followers replicate from it.

Example:

```text
orders partition 0

broker 1 -> leader
broker 2 -> follower
broker 3 -> follower
```

If the leader dies and a follower is in sync, one of the followers can become the new leader.

### Why Replication Exists

Replication exists so a broker failure does not automatically mean data loss.

This leads to two production concepts you must know:

- `replication.factor`
- `min.insync.replicas`

### Replication Factor

`replication.factor=3` means each partition has three total copies:

- one leader
- two followers

This improves:

- durability
- failover safety
- availability after broker failure

But it costs:

- more disk
- more network traffic
- somewhat slower writes

### min.insync.replicas

This controls how many replicas must be in sync before Kafka safely acknowledges a write when the producer uses strong acknowledgments.

Typical critical-data setup:

```text
replication.factor = 3
min.insync.replicas = 2
acks = all
```

This means:

- the producer wants strong durability
- Kafka requires at least 2 in-sync replicas before acknowledging

If too many replicas are unavailable, Kafka will reject writes rather than pretend the write is safe.

That is the right failure mode for critical data.

### Important Accuracy Note

It is not enough to say "replication factor 3 survives two broker failures" as a blanket statement.

The reality depends on:

- which replicas failed
- whether followers were in sync
- whether you still meet `min.insync.replicas`
- whether you care about reads only or safe writes

The right mental model is:

**replication improves fault tolerance, but safe write availability depends on the full combination of producer acknowledgments, ISR health, and replica availability**

## Putting Topic, Partition, Offset, and Group Together

Let us walk through one concrete example.

### Example Setup

Topic:

```text
orders
```

Partitions:

```text
orders-0
orders-1
orders-2
```

Producer sends:

```text
key=customer_42  value=order_created
key=customer_99  value=order_created
key=customer_42  value=payment_authorized
key=customer_42  value=order_shipped
```

Kafka might place them like this:

```text
orders-0
offset 0 -> key=customer_99  order_created

orders-1
offset 0 -> key=customer_42  order_created
offset 1 -> key=customer_42  payment_authorized
offset 2 -> key=customer_42  order_shipped

orders-2
(nothing yet)
```

Now a consumer group called `billing-service` reads the topic:

```text
billing-service group

consumer A -> orders-0
consumer B -> orders-1
consumer C -> orders-2
```

Billing processes and commits:

```text
orders-0 -> committed offset 0
orders-1 -> committed offset 2
orders-2 -> no data yet
```

A separate consumer group called `analytics-service` can read the same topic independently:

```text
analytics-service group

consumer X -> orders-0, orders-1
consumer Y -> orders-2
```

Its progress may be different:

```text
orders-0 -> committed offset 0
orders-1 -> committed offset 1
orders-2 -> no data yet
```

That one example contains almost all the core Kafka ideas:

- topic = `orders`
- partitions = `orders-0`, `orders-1`, `orders-2`
- key decides routing
- offsets mark record position within each partition
- consumer groups track their own progress independently
- retention decides how long the records stay replayable

## The Production Questions These Concepts Control

These concepts are not just vocabulary. They drive real design choices.

### Partition-related decisions

- How much concurrency do I need?
- How much future scale do I want?
- How much ordering do I need?
- What key should I use?

### Offset-related decisions

- When do I commit?
- Can I tolerate duplicates?
- Is my processing idempotent?
- How do I recover after a crash?

### Consumer-group-related decisions

- How many instances should I run?
- Why are some consumers idle?
- Why is rebalancing happening often?
- How many independent services need to read this topic?

### Retention-related decisions

- How long do I need replay?
- How much lag can I safely tolerate?
- How much disk will this require?
- Do I need Kafka to be the short-term buffer and another system to be long-term storage?

## The Intuition to Carry Forward

If you remember only five lines from this chapter, remember these:

- a topic is a named stream of related events
- a partition is one ordered lane of that stream
- an offset is the position of a record within a partition
- a consumer group is one logical application reading the topic cooperatively
- retention is the replay window, not a promise to wait for slow consumers

Once these are solid, the next step becomes much easier:

- producer settings decide how safely and efficiently data enters Kafka
- consumer settings decide how safely and efficiently data leaves Kafka

That is where production configuration starts to matter.

---

**What comes next:** Chapter 3 moves to the producer side and explains what a producer is really doing under the hood, which configurations matter most in production, and how to choose the right producer pattern instead of copying random defaults.
