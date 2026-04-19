# Chapter 5: Priority Architecture — Building Tiered Service Levels in Kafka

## Why Priority Cannot Be a Single Configuration Switch

Engineers new to Kafka often assume there must be a simple parameter — something like `message.priority = HIGH` — that tells the broker to process certain messages before others. There is not. This is an intentional design decision, not an oversight.

Kafka's core design is built around sequential, ordered log entries within a partition. If the broker tried to reorder messages by priority, it would break the ordering guarantees that make Kafka useful for event sourcing, state machines, and transaction processing. Priority processing in Kafka is therefore not a feature you enable — it is an architecture you design. The mechanism is separation: you create multiple topics with different configurations, run different consumers against them, and the *combination* of these choices creates different effective service levels.

The goal of this chapter is to move beyond understanding that this separation is possible, and into the specific, concrete decisions that make the separation actually meaningful in production. There is a significant difference between a high-priority topic and a low-priority topic that exist in name only (same configuration, same consumers) and a genuinely tiered architecture where the two tiers provide measurably different latency, reliability, and processing guarantees.

## The Five Configuration Dimensions of Priority

Priority in Kafka emerges from the coordinated difference in five configuration dimensions. Each dimension contributes to the overall effective service level. Changing just one or two dimensions gives you minor improvements. Changing all five in coordination gives you a genuinely tiered architecture.

### Dimension 1: Partition Count (Parallelism Ceiling)

As established in Chapter 2, the partition count determines the maximum number of consumer instances that can read from a topic simultaneously. This is the most direct lever for throughput. A topic with 6 partitions can be consumed by 6 consumers concurrently; a topic with 2 partitions can never benefit from more than 2 consumers, no matter how many you deploy.

```
Throughput Ceiling by Partition Count:

High Priority Topic (6 partitions):
  Consumer capacity: up to 6 instances
  Each instance processes 100 msg/sec
  Maximum throughput: 600 msg/sec

Low Priority Topic (2 partitions):
  Consumer capacity: up to 2 instances
  Each instance processes 100 msg/sec
  Maximum throughput: 200 msg/sec
```

The partition count must be planned ahead of time with peak load in mind, not current load. If your high-priority topic currently receives 500 messages per second and each consumer handles 200 messages per second, you need 3 consumers and therefore at least 3 partitions. But if you expect traffic to triple over the next year, plan for 9 partitions now — it is much harder to increase partitions safely after data is already flowing.

### Dimension 2: Replication Factor (Durability and Availability)

The replication factor determines how many copies of each partition exist across brokers. This directly affects how many broker failures the system can survive without data loss or availability interruption.

```
Replication Factor and Fault Tolerance:

Factor 1: [Leader] only — no copies
  Broker dies -> Partition is offline until broker recovers
  Risk of data loss if disk also fails
  Appropriate for: test data, regeneratable analytics events

Factor 2: [Leader] + [1 Follower]
  Broker dies -> Failover to follower (~30 seconds)
  No data loss if follower was in sync
  Appropriate for: moderate-importance events

Factor 3: [Leader] + [2 Followers]
  Two brokers can die -> System remains fully operational
  No data loss
  Appropriate for: payment data, user events, critical business events

Production rule: never use replication factor 1 in production
for data you care about. The cost (2-3x storage) is almost always
worth the protection.
```

For high-priority topics, combining replication factor 3 with `min.insync.replicas=2` creates the strongest available guarantee. When `min.insync.replicas=2`, Kafka refuses to acknowledge a write unless at least 2 replicas have confirmed receiving it. This means even if the leader crashes immediately after acknowledging your write, at least one follower has the data and can become the new leader without data loss.

### Dimension 3: Retention Policy (Time-to-Live and Resource Management)

Retention determines how long messages stay in Kafka before being deleted. This might seem unrelated to priority, but it has two important consequences: it controls disk space consumption, and it determines how far back a new consumer can read.

High-priority topics typically contain time-sensitive events — a payment alert is actionable for hours, not weeks. Keeping these messages for only 24 hours means your disk usage for the high-priority topic stays small and predictable, ensuring there is always available disk capacity for new critical messages. Running out of disk space on your Kafka broker is a catastrophic event that stops all writes to that broker.

Low-priority topics often contain batch-processed or analytically valuable events where longer retention is genuinely useful. An analytics system processing daily reports might read events that are 3 days old. A new fraud detection service might need to read 2 weeks of payment history when it first deploys.

```
Retention Configuration Interaction with Consumer Lag:

High Priority Topic (retention = 1 day):
  If your consumer falls 24 hours behind -> messages start getting deleted
  This is a CRITICAL alert condition — you are losing data
  Design for this: alert when lag > 1 hour (well before the cliff)

Low Priority Topic (retention = 7 days):
  Consumer can fall 7 days behind before losing data
  Much more forgiving of slow consumers or temporary outages
  A new service can "catch up" by reading a week of history
```

### Dimension 4: Segment Configuration (Operational Efficiency)

A Kafka partition is stored as a series of **segment files** on disk. Each segment file contains a range of messages. When a segment file is "full" (either by time elapsed or by size), Kafka closes it and opens a new one. Only a closed segment file can be deleted (for retention) or compacted.

This matters for priority because smaller, more frequent segments mean recent data is always in a fresh segment. When the retention cleanup process runs, it can delete older segments immediately without waiting for a large segment to fill up. For high-priority topics, this means disk space is freed quickly and the cluster stays lean. For low-priority topics, larger and less frequent segments reduce the operational overhead of segment rolling and cleanup.

```
Segment Rolling and Data Accessibility:

High Priority Topic (segment.ms = 5 minutes):
  Every 5 minutes, current segment closes and a new one opens
  Closed segments can be immediately eligible for cleanup
  Recent messages are in a small, efficiently accessible segment

  Time 0:00 -> Segment A opens
  Time 0:05 -> Segment A closes (may have 1000 messages)
               Segment B opens
  Time 0:10 -> Segment B closes (may have 800 messages)
               Segment C opens (current, being written to)
  
  Cleanup can delete Segment A as soon as it's older than retention.ms

Low Priority Topic (segment.ms = 1 hour):
  Segment closes only once per hour
  Less overhead, but cleanup can only happen on whole-segment boundaries
```

### Dimension 5: Consumer Configuration (Processing Commitment)

The consumer configuration is arguably the most impactful dimension because it controls the actual processing resources dedicated to each tier. A high-priority topic with 6 partitions is wasted potential if the consumer group has only one instance with one thread.

The full set of consumer differences for each tier includes the number of consumer instances (matching or approaching the partition count), the number of worker threads per instance for concurrent processing, the poll interval and processing timeout configurations, and the SLA targets that trigger alerts.

```
Complete Consumer Tier Configuration:

HIGH PRIORITY CONSUMER GROUP:
  Instances:         6 (one per partition, maximum parallelism)
  Threads per inst:  10 (concurrent I/O-bound processing)
  Total workers:     60
  Poll timeout:      100ms (respond quickly to new messages)
  Processing SLA:    < 500ms per message
  Lag alert:         > 100 messages (tight, because retention is 1 day)
  
LOW PRIORITY CONSUMER GROUP:
  Instances:         2 (limited, batch-friendly)
  Threads per inst:  1 (sequential, simple, predictable)
  Total workers:     2
  Poll timeout:      5000ms (comfortable, no rush)
  Processing SLA:    < 60 seconds per message
  Lag alert:         > 10,000 messages (generous, retention is 7 days)
```

## The Complete Priority Architecture in Practice

Bringing all five dimensions together, here is what a complete two-tier priority architecture looks like for a payment service:

```
PAYMENT SERVICE PRIORITY ARCHITECTURE
======================================

TIER 1: High Priority
---------------------
Topic:              payment-service-high-priority-v1
Partitions:         6
Replication factor: 3 (with min.insync.replicas=2)
Retention:          1 day (86,400,000 ms)
Segment rolling:    5 minutes (300,000 ms)

Consumer Group:     payment-high-processors
Instances:          6 (auto-scaled, one per partition)
Threads/instance:   10
Strategy:           Concurrent (external payment gateway calls)
SLA target:         500ms end-to-end
Lag alert:          > 1,000 messages

Use for: VIP transactions, fraud alerts, real-time balance checks


TIER 2: Low Priority
---------------------
Topic:              payment-service-low-priority-v1
Partitions:         2
Replication factor: 1 (lower cost, acceptable risk for batch)
Retention:          7 days (604,800,000 ms)
Segment rolling:    1 hour (3,600,000 ms)

Consumer Group:     payment-low-processors
Instances:          1 (single, batch-friendly)
Threads/instance:   1
Strategy:           Sequential (simple batch inserts to data warehouse)
SLA target:         60 seconds per message
Lag alert:          > 50,000 messages

Use for: daily report generation, analytics export, audit archival


PRODUCER ROUTING LOGIC:
if transaction.amount > 10000 or customer.tier == "VIP":
    route to high-priority topic
else:
    route to low-priority topic
```

## The End-to-End Lifecycle of a High-Priority Message

Following a single message through the entire system makes the architecture concrete. Consider a VIP customer placing a $50,000 order.

```
Step 1: Producer identifies priority

[Order Service]
  order = {order_id: "VIP-001", amount: 50000, customer_tier: "VIP"}
  router.send("payment-service", order, priority=HIGH)
  
  Internal routing: amount > 10000 -> high-priority topic
  Key = customer_id (ensures all events for this customer are in same partition)


Step 2: Message lands in Kafka

  Topic: payment-service-high-priority-v1
  Partition 3 (determined by hash of customer_id)
  
  Broker 1 (Leader for Partition 3):  writes message, offset=10,047
  Broker 2 (Follower):                replicates, offset=10,047
  Broker 3 (Follower):                replicates, offset=10,047
  
  Producer receives ack only after BOTH followers confirm (min.insync.replicas=2)
  Total time from produce() call to ack: ~15ms


Step 3: Consumer receives message

  Consumer instance C3 is assigned to Partition 3
  C3's poll() returns the message at offset 10,047
  C3 submits it to thread pool (Worker thread 7 picks it up)
  
  Worker 7: validates order -> calls payment gateway API -> receives approval
  Total processing time: 180ms (200ms API call, some parallelism savings)


Step 4: Offset committed

  C3 commits offset 10,047 for Partition 3
  SLA: message arrived in Kafka at T=0ms, fully processed at T=195ms
  SLA target was 500ms — PASSED with 305ms to spare


Step 5: Message lifecycle ends

  Message persists in topic for 1 day
  After 1 day, Kafka's cleanup thread deletes the segment containing offset 10,047
  Disk space is freed
```

## Routing Logic: Where Business Logic Meets Infrastructure

The routing producer is where business rules translate into infrastructure decisions. This is also where the most common mistakes happen. Two failure modes to watch for in production are **routing too many messages to high priority** and **routing too few**.

If your routing logic is too aggressive (everything goes to high priority), you defeat the purpose of the architecture. Your high-priority consumers become overloaded, consumer lag grows, SLAs are missed, and your low-priority infrastructure sits idle. The high-priority tier needs headroom — it should operate at well below capacity during normal conditions so it has reserve capacity to absorb spikes.

If your routing logic is too conservative (almost nothing qualifies as high priority), the high-priority infrastructure is wasteful and your users' most critical actions queue up behind batch work in the low-priority tier.

The right calibration comes from measuring your actual traffic. In most payment systems, VIP transactions and fraud alerts represent 5–15% of total volume but require 80% of processing resources. The routing threshold should be set to match this reality, and the partition count and consumer thread count should be sized accordingly.

## When to Add More Than Two Tiers

Two tiers (high and low) are sufficient for most systems. A third tier makes sense when you have three genuinely distinct service level requirements with no practical way to handle all three with two tiers. For example: real-time fraud detection (< 100ms), standard payment processing (< 2 seconds), and batch reconciliation (< 24 hours). These have different enough requirements that a single "high" tier cannot serve both the 100ms and the 2-second use case well without over-provisioning.

The cost of each additional tier is operational complexity: more topics to monitor, more consumer groups to track, more routing rules to maintain, and more alerting configurations to manage. Practical experience in the industry suggests that most production systems need at most three tiers, and many work well with two. If you find yourself designing five or more priority tiers, that is a signal that the routing logic has become the system's main complexity, and you should consider whether a different architectural approach (like a dedicated job scheduler for batch work) would serve you better.

---

**What comes next:** Chapter 6 covers the enterprise resilience patterns that protect your Kafka consumers from the inevitable failures of distributed systems: Rate Limiting, Backpressure, Bulkhead Isolation, Circuit Breaking, Message Filtering, and Dead Letter Queues. Each pattern addresses a specific failure mode, and understanding which failure mode each addresses is the key to knowing when to apply it.
