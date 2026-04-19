# Chapter 7: Production Decision Making — Designing, Operating, and Debugging Kafka Systems

## The Questions You Must Answer Before Writing Any Code

Every Kafka production system that fails does so for predictable reasons. The failure was usually seeded in the design phase, when a critical question was left unanswered or answered incorrectly. Before you write a single line of producer or consumer code, you need definitive answers to the following seven questions.

**Question 1: What is the maximum acceptable message delay for this system?**

This answer determines your SLA target, which in turn determines your partition count, consumer thread count, and alerting thresholds. If the answer is "100 milliseconds," you need a very different architecture than if the answer is "15 minutes" or "overnight." Document this as a firm number from your product or business stakeholders, not a vague preference. Vague preferences lead to architectures that are almost-right until an edge case hits them.

**Question 2: What happens if a message is processed twice?**

The answer reveals whether your processing logic must be idempotent. If processing a payment event twice would charge a customer twice, your system is not idempotent and you must build deduplication logic before you can safely use at-least-once delivery. If inserting an analytics event twice simply creates a duplicate row that analysts can filter out, idempotency is less critical. The answer to this question determines how much defensive programming your consumer needs.

**Question 3: What happens if a message is never processed?**

If a single lost payment event is a compliance violation, you need `acks=all`, `min.insync.replicas=2`, at-least-once consumer commit semantics, and a dead letter queue to capture failures. If a single lost click-tracking event is irrelevant noise, you can tolerate weaker guarantees. The data loss tolerance directly drives your producer and consumer configuration choices.

**Question 4: What is the maximum rate of message production at peak?**

This number drives partition count and consumer scaling. If you expect 10,000 messages per second at peak, and each consumer can handle 1,000 messages per second, you need at least 10 partitions and 10 consumer instances. Add a safety factor of 2x for headroom, meaning 20 partitions designed into the topic from the start.

**Question 5: Who are all the consumers of this topic, and do they have different latency or throughput requirements?**

If ten different downstream services consume a single topic, and five need real-time processing while five are batch-oriented, you may need a priority architecture or topic split from day one. Discovering this after deployment means live topic reconfiguration, which is painful.

**Question 6: How long must data be available for replay?**

This determines retention policy. If a new consuming service might be deployed six months from now and needs to process historical data, you need six months of retention — or you need a separate archival system. Retention decisions affect disk costs directly and must be reviewed with infrastructure cost owners.

**Question 7: What external dependencies does your consumer call, and what is their reliability SLA?**

Each external dependency with a reliability SLA below your own must have a resilience pattern applied to it. A payment gateway with 99.9% uptime (8.7 hours of downtime per year) means your consumer will regularly encounter gateway failures. A circuit breaker and dead letter queue are mandatory. An internal in-memory cache with 99.999% uptime probably does not need a circuit breaker.

## Sizing Your Kafka System: The Capacity Planning Framework

Capacity planning for Kafka involves sizing four dimensions: partitions, consumer instances, consumer threads per instance, and broker disk. Getting any one of these wrong will cause production incidents.

**Partitions:** Start with your peak throughput requirement, divide by single-consumer throughput, and multiply by 2 for headroom. For a topic receiving 50,000 messages per second at peak, where each consumer handles 5,000 messages per second, you need at minimum 10 partitions and should create 20. Never create fewer than 3 partitions for any topic used in production — it limits your future scaling options unnecessarily.

**Consumer instances:** In practice, start with one consumer instance per partition and scale down if load is light. Over time, your consumer lag metrics will tell you if you need more or fewer instances. Auto-scaling based on consumer lag is a mature production practice — when lag exceeds a threshold for more than 5 minutes, trigger a new consumer instance. When lag is near zero for 30 minutes, allow scale-in.

```
Capacity Planning Worked Example:

Business requirement:
  Process 30,000 user events per second at peak
  Process each event in under 500ms
  Never lose a message
  Retain 7 days of data (for new consumer catch-up)

Event characteristics:
  Average message size: 1 KB
  Processing type: external API call (200ms average)

Partition calculation:
  Each consumer thread handles: 1 msg / 200ms = 5 msg/sec
  With 10 threads per consumer: 50 msg/sec per instance
  To handle 30,000/sec: 30,000 / 50 = 600 threads needed
  -> 60 consumer instances of 10 threads each, needing 60 partitions
  -> With 2x headroom: CREATE 120 PARTITIONS

Disk calculation:
  30,000 msg/sec * 1KB = ~30 MB/sec ingest rate
  7 days retention = 7 * 24 * 3600 * 30MB = ~18 TB per replica
  With replication factor 3: ~54 TB total cluster storage needed

SLA check:
  At 60 instances * 10 threads * 5msg/sec/thread = 3,000 msg processed/sec
  That means at 30,000 msg/sec input, you are at 10% utilization per consumer
  Each message will wait in queue briefly but well under 500ms SLA
  Peak traffic (3x normal, 90,000 msg/sec) would require dynamic scaling
```

## The Monitoring Stack You Cannot Operate Without

Kafka systems that are not instrumented will fail silently and be difficult to debug when they do. There are five metric categories that must be monitored, without exception, in every production deployment.

**Consumer Lag per Partition.** This is the most important operational metric in Kafka. Not aggregate lag — per-partition lag. If partition 3 of your high-priority topic has lag of 50,000 messages while the other 5 partitions have lag near zero, you have a problem isolated to the consumer assigned to partition 3. Aggregate lag hides this. You want an alert that fires when any single partition's lag exceeds your defined threshold, and the threshold should be set based on retention time. If retention is 24 hours and you are ingesting 1,000 messages per second, a lag of 1,000,000 messages means you are 1,000 seconds (17 minutes) behind — perfectly fine. If your retention is 2 hours, that same lag means you are dangerously close to data loss.

**Producer Error Rate and Delivery Latency.** Track the percentage of produce calls that fail and the time between produce and delivery acknowledgment. A rising error rate without a rising latency suggests broker connectivity problems. A rising latency without rising errors suggests brokers are overloaded. These two metrics together diagnose most producer-side issues.

**Consumer Processing Time Distribution.** Not just average processing time, but the full distribution: P50, P95, P99, and P99.9. The average is almost always misleadingly low. If your P99 processing time is 45 seconds and your `max.poll.interval.ms` is 60 seconds, you are one unlucky request away from a consumer being kicked out of its group and triggering a rebalance. The distribution tells you this; the average hides it.

**Rebalance Frequency.** Count how many consumer group rebalances happen per hour. A healthy, stable system should have rebalances only when consumers are deliberately added or removed (deployments). Rebalances happening every few minutes indicate consumer health problems — consumers crashing and restarting, processing timeouts causing group expulsion, or networking issues causing intermittent group membership failures.

**Dead Letter Queue Depth.** Track how many messages are accumulating in each DLQ topic. A DLQ that is empty is healthy. A DLQ that receives a few messages per day suggests occasional edge cases that need review. A DLQ that is rapidly growing suggests a systemic failure — a bug in your processing code, a change in upstream message format, or an external dependency that is returning unexpected errors.

```
Monitoring Dashboard Layout:

+----------------------------------+----------------------------------+
| Consumer Lag (per partition)      | Producer Success Rate            |
|                                   |                                  |
|  P0: ████░ 1,200  [OK]           |  Current: 99.97%  [OK]           |
|  P1: ████░ 800    [OK]           |  5-min ago: 99.98%               |
|  P2: ████░ 1,100  [OK]           |  Alert threshold: < 99.9%        |
|  P3: ████████ 48,000 [ALERT!]    |                                  |
+----------------------------------+----------------------------------+
| Consumer Processing Time P99      | DLQ Message Count                |
|                                   |                                  |
|  payment-high: 380ms  [OK]       |  payment-high-DLQ:  2   [OK]    |
|  payment-low:  12sec  [OK]       |  payment-low-DLQ:   847 [WARN]  |
|                                   |  user-events-DLQ:   0   [OK]    |
+----------------------------------+----------------------------------+
| Group Rebalances (last hour)      | Broker Disk Usage                |
|                                   |                                  |
|  payment-processors:  2  [OK]    |  Broker 1:  68%  [OK]           |
|  analytics-consumers: 14 [WARN]  |  Broker 2:  71%  [OK]           |
|                                   |  Broker 3:  69%  [OK]           |
|                                   |  Alert threshold: > 80%          |
+----------------------------------+----------------------------------+
```

## The Production Failure Scenarios You Must Plan For

Every Kafka system will experience each of the following failure scenarios at least once in production. The difference between an engineer who panics and an engineer who resolves the incident calmly is having thought through each scenario before it happens.

**Scenario 1: A Kafka Broker Goes Down**

What happens: Kafka automatically elects new partition leaders from the follower replicas. If your replication factor is 3, this happens with no data loss and typically completes within 30 seconds. Your producer may receive temporary errors during the election and should retry. Your consumers will see a brief pause and then reconnect.

What you should check: Are all partitions that were on the failed broker successfully re-led by followers? Is your `min.insync.replicas` setting satisfied (if you required 2 in-sync replicas and now only have 2 brokers total, you still have 2 — fine. But if `min.insync.replicas=2` and you now have only 1 in-sync replica, produces will fail). How long until you can bring the failed broker back to restore your replication factor?

What goes wrong if you are unprepared: If your replication factor is 1 (no replicas), the partition is completely offline until the broker restarts. You are in a data loss and availability crisis rather than a brief hiccup.

**Scenario 2: Consumer Lag Suddenly Grows**

What happens: Something caused your consumers to slow down. The possible causes are: a downstream service (database, API) became slow; a code deployment introduced a performance regression; a traffic spike produced more messages than your consumer capacity can handle; a schema change in messages is causing parsing failures and retry loops; or one consumer instance crashed and partitions were rebalanced among fewer instances.

Your first diagnostic step is to look at per-partition lag. If one partition's lag is far higher than others, the consumer handling that partition has a problem specific to it. If all partitions' lag is growing uniformly, it is a throughput or downstream dependency problem.

Your second diagnostic step is to check consumer processing time metrics. If P99 processing time jumped from 200ms to 5 seconds at the same moment lag started growing, you have a downstream dependency slowdown. If processing time is unchanged but lag is growing, you simply need more consumer instances.

**Scenario 3: Dead Letter Queue Suddenly Filling Up**

A growing DLQ is almost always caused by one of three things: a code deployment that introduced a bug in the processing logic, a change in the upstream message format that your consumer does not handle, or a downstream dependency returning unexpected errors that cause all messages to fail.

The first action is to examine the DLQ messages themselves. What is the error type? What does the `original_message` look like? Is there a common pattern (all failures are for messages with a specific field value, or all failures started at a specific timestamp corresponding to a deployment)?

After identifying the cause, fix it, redeploy the consumer, and then replay the DLQ messages back to the original topic. Your dead letter handler should retain enough metadata (`original_topic`, `partition`, `offset`, `timestamp`) to make this replay straightforward.

**Scenario 4: Consumer Group Rebalancing Continuously**

Continuous rebalancing is one of the most disruptive operational problems in Kafka because processing pauses during every rebalance. The consumer group coordinator logs will show you which consumer is joining or leaving the group with each rebalance.

If a specific consumer instance is repeatedly joining and leaving, investigate that instance: is it crashing and restarting? Is it experiencing OOM (out of memory) kills? Is its processing time consistently exceeding `max.poll.interval.ms`, causing it to be expelled from the group by the coordinator?

If all instances are triggering rebalances simultaneously, it may be a coordination problem with the group coordinator broker itself, or a network partition between your consumers and the broker.

**Scenario 5: Producer Delivery Failures Spike**

A spike in producer delivery failures points to one of: broker leader elections (transient, should resolve within 60 seconds), broker disk full (critical, requires immediate disk cleanup or expansion), broker memory pressure, network connectivity issues, or `min.insync.replicas` not being satisfied (a broker replica is significantly behind the leader).

Check your broker disk usage immediately — a full disk is the most common cause of sudden producer failures and is straightforward to diagnose but requires fast action, because a broker that cannot write to disk stops accepting produce requests entirely.

## The Schema Evolution Problem: Planning for Change

One of the most underestimated production challenges in Kafka is **schema evolution** — how to change the format of your messages over time without breaking existing consumers.

Unlike a database where you can run an ALTER TABLE statement and all code immediately sees the new schema, Kafka messages are stored as bytes. Old messages in the broker still have the old format. New consumers might read old messages. Old consumers might read new messages. The following rules apply.

Adding a new optional field to your messages is generally safe: old consumers that do not know about this field will ignore it; new consumers can use it. Removing a field is dangerous: old consumers expecting that field will fail to parse the message. Renaming a field is equivalent to removing the old field and adding a new one — equally dangerous. Changing the type of a field (from string to integer, for example) is the most dangerous change of all.

```
Safe Schema Evolution Patterns:

Version 1 of payment event:
  {
    "payment_id": "pay_001",
    "amount": 50.00,
    "currency": "USD"
  }

Version 2 (SAFE - adding optional field):
  {
    "payment_id": "pay_001",
    "amount": 50.00,
    "currency": "USD",
    "customer_tier": "VIP"    <- new, optional; old consumers ignore it
  }

Version 3 (DANGEROUS - removing field):
  {
    "payment_id": "pay_001",
    "amount": 50.00"
    // "currency" removed! Old consumers expecting this field will fail.
  }

SAFE approach to field removal:
  Step 1: Deploy new consumers that handle currency being absent
  Step 2: Deploy new producers that stop sending currency
  Step 3: Wait until all old messages (with currency) have expired from retention
  Step 4: Remove currency handling code from consumers
```

Using a schema registry (like Confluent Schema Registry with Avro or Protobuf) enforces schema compatibility rules at the infrastructure level, rejecting producer deployments that would introduce breaking changes. This is strongly recommended for any organization with multiple teams publishing to shared topics.

## The Interview Lens: How to Present These Concepts

When discussing Kafka in an interview, the quality of your answers is determined not by whether you can name the concepts but by whether you can articulate the trade-offs. Every Kafka concept involves a trade-off, and demonstrating awareness of those trade-offs signals production experience.

When asked about partition count, do not just say "more partitions = more parallelism." Say: "More partitions give you more parallelism, but they also mean more open file descriptors on brokers, longer rebalance times when consumers join or leave, more leader election work, and more complexity in ordering guarantees. The right number is based on peak throughput requirements with 2x headroom, but there is a real operational cost to very high partition counts."

When asked about delivery guarantees, do not just list at-most-once, at-least-once, and exactly-once. Say: "At-least-once is the right default for most systems, because exactly-once adds significant producer and consumer complexity and has non-trivial performance overhead. The key engineering investment for at-least-once is making your consumer processing idempotent, which is a design discipline rather than a configuration change."

When asked about the circuit breaker, do not just describe the state machine. Say: "The circuit breaker solves a specific problem that is easy to overlook: without it, a failed downstream dependency does not just cause failures — it causes threads to block for full timeout durations, which exhausts thread pools and eventually makes your entire service unresponsive. The circuit breaker's value is that it turns a slow failure (service gradually degrades over minutes) into a fast failure (requests fail immediately when the dependency is known to be down), which is dramatically easier to handle and recover from."

The underlying theme in all of these answers is the same: you understand why the pattern exists, what specific failure mode it prevents, and what it costs to use it. That is the thinking of an engineer who has operated these systems under pressure, and it is exactly what production interviews are testing for.

---

This concludes the seven-chapter Kafka production documentation series. The chapters build on each other: Chapter 1 gives you context for why Kafka exists, Chapter 2 gives you the conceptual vocabulary, Chapters 3 and 4 cover your codebase's producer and consumer patterns with production trade-offs, Chapter 5 covers the priority architecture, Chapter 6 covers the resilience patterns, and this chapter synthesizes everything into the decision-making frameworks you need to design, operate, and debug production systems.
