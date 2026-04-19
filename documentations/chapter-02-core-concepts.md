# Chapter 2: Core Concepts — Topics, Partitions, Offsets, Brokers, and Consumer Groups

## Starting with a Topic

A **topic** in Kafka is the simplest concept to grasp: it is a named stream of related messages. Think of it as a category or a channel. You might have a topic called `payments`, a topic called `user-signups`, and a topic called `inventory-updates`. When your code wants to write an event about a payment, it writes to the `payments` topic. When another piece of code wants to react to payment events, it reads from the `payments` topic.

If you have used a pub/sub messaging system before, a topic is similar to a "channel" in Slack or a "subject" in NATS. If you have not, think of it like a specific folder in a shared filing cabinet — producers put documents in the folder, consumers take documents out to read them, except in Kafka the documents stay in the folder even after being read (more on that shortly).

The key thing that makes Kafka topics different from a simple folder is what happens inside: every topic is broken up into one or more **partitions**, and this is where Kafka's real power (and complexity) lives.

## Partitions: The Unit of Parallelism

Imagine you run a warehouse and you receive 10,000 packages a day to sort and send out. If you have one sorting conveyor belt, it can only process packages sequentially — one at a time. Now imagine you have ten conveyor belts running in parallel. You can process ten times the volume, because ten workers can each handle one belt simultaneously.

A Kafka topic partition is exactly like a conveyor belt. Each partition is an independent, ordered, append-only log of messages. When a topic has multiple partitions, Kafka can spread those partitions across multiple machines, and multiple consumers can each read from a different partition simultaneously.

```
Topic: "payments"  (with 3 partitions)

Partition 0:  [msg_1] [msg_4] [msg_7] [msg_10] ...  ---> Consumer Instance A
Partition 1:  [msg_2] [msg_5] [msg_8] [msg_11] ...  ---> Consumer Instance B
Partition 2:  [msg_3] [msg_6] [msg_9] [msg_12] ...  ---> Consumer Instance C
```

In this example, three consumers are processing payment messages simultaneously. If you had only one partition, you could only have one consumer reading from it at a time — you would be limited to one conveyor belt no matter how many workers you hired. This is why **the number of partitions in a topic is the maximum degree of parallelism you can ever achieve for that topic**. You can never have more active consumers reading from a topic simultaneously than there are partitions. This is one of the most important production trade-offs in all of Kafka.

If you create a topic with 3 partitions and you deploy 10 consumer instances, only 3 of those consumers will be actively reading. The other 7 will sit idle, waiting. You will have paid for 10 machines and used 3. On the other hand, if you create a topic with 3 partitions and later need to scale to handle 10 times the load, you cannot simply spin up 10 consumer instances — you are fundamentally bottlenecked at 3 until you increase the partition count. And increasing partition count on an existing topic, while possible, disrupts message ordering guarantees in ways that require careful planning.

The practical recommendation from experienced Kafka engineers is: **create more partitions than you currently need** when first setting up a topic, because adding partitions later is harder than having idle capacity. A common heuristic is to set partitions to the peak number of concurrent consumer instances you expect within the next year, multiplied by two.

## How Messages Are Assigned to Partitions

When your producer code writes a message to a topic, Kafka needs to decide which partition to put it in. There are three ways this happens, and the choice has significant consequences for ordering.

If your message has a **key** (a string you provide that identifies what the message is about — for example, the user ID or the order ID), Kafka will hash that key and always send messages with the same key to the same partition. This guarantees that all events about user 42 will always land in Partition 1 (or whichever partition the hash resolves to), and they will always be in the order they were written. This is critical for use cases where you need to process a user's events in sequence.

```
Messages with keys:

producer.send("payments", data, key="user_42") ---> always Partition 1
producer.send("payments", data, key="user_99") ---> always Partition 0
producer.send("payments", data, key="user_42") ---> always Partition 1 (same key!)

Partition 0: [user_99_event_1] [user_99_event_2] ...
Partition 1: [user_42_event_1] [user_42_event_2] [user_42_event_3] ...
Partition 2: [user_17_event_1] ...
```

If your message has **no key**, Kafka distributes messages across partitions using a round-robin or sticky assignment strategy. This maximizes throughput and even distribution, but you lose any ordering guarantees across different messages.

If you supply a **custom partitioner** function, you can implement your own routing logic — for example, sending all VIP customer messages to the first two partitions and all regular customer messages to the remaining partitions.

The production decision here is this: if your downstream processing requires that messages about the same entity (same user, same order, same device) be processed in order, **you must use keys**. If you do not care about ordering and simply need maximum throughput, keyless round-robin is simpler and more balanced.

## Offsets: The Position Counter

Every message in a Kafka partition is assigned a sequential integer called an **offset**. The first message written to a partition gets offset 0, the second gets offset 1, the third gets offset 2, and so on. Offsets only ever increase — there is no concept of removing a message and reusing its offset number.

```
Partition 0 of "payments" topic:

Offset:  [ 0 ]  [ 1 ]  [ 2 ]  [ 3 ]  [ 4 ]  [ 5 ]  ...
Data:    [pay1] [pay2] [pay3] [pay4] [pay5] [pay6]  ...
         ^                                    ^
         First message ever written           Most recent message
```

The offset is how Kafka solves one of the hardest problems in distributed systems: **tracking what a consumer has already processed**. Each consumer tells Kafka "I have processed everything up to offset 47 in Partition 0." The next time that consumer asks for messages, Kafka knows to send it offset 48 onward.

This position is stored in a special internal Kafka topic called `__consumer_offsets`. The act of recording your progress is called **committing an offset**, and the strategic decision of *when* to commit is one of the most consequential decisions you make when writing a consumer.

If you commit offsets too eagerly — committing that you have processed a message immediately after receiving it, before actually processing it — and then your consumer crashes during processing, Kafka thinks you have already handled that message and will not resend it. **The message is effectively lost**. This is called *at-most-once* delivery.

If you commit offsets only after successfully processing a message, and your consumer crashes after processing but before committing, Kafka will resend the message. Your processing code will see it again and must be prepared to handle a duplicate. This is called *at-least-once* delivery, and it is the default behavior in well-written Kafka consumers.

```
At-Most-Once (DANGEROUS default in naive implementations):

  Receive msg (offset 5)
  Commit offset 5              <-- "I'm done with offset 5"
  [crash happens here]
  Restart: begins from offset 6
  msg at offset 5 is LOST FOREVER

---

At-Least-Once (recommended for most production systems):

  Receive msg (offset 5)
  Process msg
  [crash happens here]
  Restart: begins from offset 5 again
  msg at offset 5 is processed a SECOND TIME (duplicate)
  Your code must handle this (idempotency)

---

Exactly-Once (ideal, but has real performance cost):

  Kafka + your application work together with transactions
  msg is processed exactly once, even across failures
  Requires careful configuration; not free
```

The concept of handling duplicates — designing your code so that processing the same message twice produces the same result as processing it once — is called **idempotency**. For example, if you are updating a user's account balance, you should not do `balance += $50` (because doing it twice would incorrectly add $100). Instead, you should use a transaction ID to detect and skip already-processed messages, or use an operation that is naturally idempotent like `balance = $150` (setting the value explicitly based on the event's data).

## Brokers: The Machines Running Kafka

A **broker** is simply a Kafka server — one machine in the cluster that is running the Kafka process. In production, you almost always run multiple brokers (typically 3 to 9 for most organizations) to achieve fault tolerance. Together, these brokers form the **Kafka cluster**.

When you create a topic with multiple partitions, those partitions are distributed across brokers. If your cluster has 3 brokers and your topic has 6 partitions, each broker typically holds 2 partitions. This spreads the read and write load evenly across your hardware.

```
Kafka Cluster (3 brokers, topic "payments" with 6 partitions):

Broker 1 (machine-01)         Broker 2 (machine-02)         Broker 3 (machine-03)
+--------------------+        +--------------------+        +--------------------+
| Partition 0        |        | Partition 2        |        | Partition 4        |
| Partition 1        |        | Partition 3        |        | Partition 5        |
+--------------------+        +--------------------+        +--------------------+
```

For each partition, one broker acts as the **leader** for that partition — it handles all reads and writes. The other brokers that also hold copies of that partition are called **followers** or **replicas** — they passively copy data from the leader and serve as hot standbys.

This brings us to one of the most important configuration parameters you will encounter: the **replication factor**. The replication factor tells Kafka how many total copies of each partition to maintain (leader + followers). A replication factor of 3 means every partition has one leader and two followers, stored on three different brokers.

```
Partition 0 of "payments" (replication factor = 3):

Broker 1: Partition 0 [LEADER]   <-- handles all reads/writes
Broker 2: Partition 0 [FOLLOWER] <-- silently copies from leader
Broker 3: Partition 0 [FOLLOWER] <-- silently copies from leader

If Broker 1 dies:
  Kafka automatically elects Broker 2 or 3 as the new leader
  Zero data loss if followers were fully caught up
  Takes seconds, not minutes
```

The production trade-off with replication factor is straightforward: higher replication factor means more storage (3x storage for factor of 3), more network traffic between brokers (leaders push data to followers), and slightly higher write latency (because Kafka can wait for followers to confirm receipt before acknowledging to the producer). But you get proportionally more fault tolerance. Most production systems use a replication factor of 3, which can survive 2 simultaneous broker failures while remaining operational.

The related parameter is **min.insync.replicas** (minimum in-sync replicas). When this is set to 2 alongside a replication factor of 3, it means a write is only acknowledged to the producer after at least 2 replicas have confirmed receiving it. If 2 replicas are down, the cluster refuses writes rather than risk data loss. This is the configuration pattern LinkedIn, Confluent, and most enterprise Kafka users recommend for critical data.

## Consumer Groups: Coordinated Reading at Scale

A **consumer group** is a named set of consumer processes that cooperate to read from a topic. When multiple consumers join the same group, Kafka automatically assigns partitions to them so that each partition is read by exactly one consumer in the group at any given time. No two consumers in the same group ever read the same partition simultaneously.

This is the mechanism that enables horizontal scaling of consumers. If you have 6 partitions and you start with 2 consumer instances in the same group, each consumer handles 3 partitions. If your load increases and you add 4 more instances (for 6 total), each consumer now handles exactly 1 partition. Kafka handles this reassignment automatically in a process called a **rebalance**.

```
Topic "payments": 6 partitions
Consumer Group: "payment-processor"

SCENARIO 1: 2 consumer instances
Consumer-A: handles Partitions [0, 1, 2]
Consumer-B: handles Partitions [3, 4, 5]

SCENARIO 2: 6 consumer instances (scaled up)
Consumer-A: handles Partition [0]
Consumer-B: handles Partition [1]
Consumer-C: handles Partition [2]
Consumer-D: handles Partition [3]
Consumer-E: handles Partition [4]
Consumer-F: handles Partition [5]

SCENARIO 3: 8 consumer instances (over-scaled, wasteful)
Consumer-A: handles Partition [0]
Consumer-B: handles Partition [1]
Consumer-C: handles Partition [2]
Consumer-D: handles Partition [3]
Consumer-E: handles Partition [4]
Consumer-F: handles Partition [5]
Consumer-G: IDLE (no partition to assign)
Consumer-H: IDLE (no partition to assign)
```

The crucial insight about consumer groups is that **different consumer groups are completely independent**. They each maintain their own offset position and can read the same topic at entirely different speeds. This is what allows multiple downstream services to all read from the same event stream without interfering with each other. Your email service and your analytics service and your billing service all belong to different consumer groups, each reading the `user-signups` topic independently.

```
Topic: "user-signups"

Group "email-service":       offset at 5,000  (fast, keeps up)
Group "analytics-service":   offset at 4,500  (slightly behind)
Group "billing-service":     offset at 500    (slow batch processor)
Group "new-fraud-service":   offset at 0      (just deployed, reading history)

All four groups read the SAME messages in Kafka.
None of them interfere with each other.
Kafka retains the messages as long as any group might need them.
```

## The Concept of Consumer Lag: Your Most Important Operational Metric

**Consumer lag** is the difference between the offset of the most recently written message in a partition and the offset up to which a consumer group has committed. In plain terms, it is the count of messages that exist in Kafka but have not yet been processed by a particular consumer group.

```
Partition 0 state:

Most recent message offset: 10,000
Consumer group "billing" committed offset: 9,500

Consumer lag = 10,000 - 9,500 = 500 messages behind
```

A lag of zero means the consumer is keeping up in real time. A lag of 500 messages is generally fine. A lag that is growing over time — from 500 to 5,000 to 50,000 — means your consumer cannot keep up with the rate of production, and you have a scaling problem. If your retention period expires (messages are deleted from Kafka after their configured retention time), and your consumer lag has grown so large that unconsumed messages get deleted, you have permanently lost data. This is the most catastrophic consumer failure scenario in Kafka.

Monitoring consumer lag is not optional in production — it is mandatory. It is the single metric that tells you whether your entire event-driven pipeline is healthy.

## Retention: How Long Kafka Keeps Messages

Every Kafka topic is configured with a **retention policy** that determines when messages are deleted. There are two kinds of retention: time-based and size-based. Time-based retention means messages are deleted after a configured number of hours or days (the default is 7 days). Size-based retention means messages are deleted once the total log for a partition exceeds a configured size limit.

Understanding retention is critical because it directly affects two things: how far back a new consumer can read (you cannot read events older than what retention allows), and how much disk space your Kafka cluster requires. High-volume topics with long retention periods can consume enormous amounts of disk.

The strategic tension in production is between business needs (auditability, replay capability, new services catching up on history) and operational costs (disk, backup complexity, recovery time). Organizations that need long-term event history often pair Kafka with a separate long-term storage system — like Apache Iceberg, Amazon S3, or a data warehouse — that archives events before Kafka's retention deletes them.

## Putting It All Together: A Complete Mental Model

Before moving on, it is worth drawing the full picture of how all these concepts connect in a running production system.

```
KAFKA CLUSTER

 +------------------------------------------------------------------+
 |                                                                  |
 |  BROKER 1                BROKER 2                BROKER 3        |
 |  +-----------------+    +-----------------+    +-------------+   |
 |  | payments-P0 [L] |    | payments-P1 [L] |    | pay-P2 [L]  |   |
 |  | payments-P1 [F] |    | payments-P2 [F] |    | pay-P0 [F]  |   |
 |  | payments-P2 [F] |    | payments-P0 [F] |    | pay-P1 [F]  |   |
 |  +-----------------+    +-----------------+    +-------------+   |
 |     [L]=Leader [F]=Follower/Replica                              |
 +------------------------------------------------------------------+
         ^                         ^
         |    Producers write       |     Consumers read
         |                         |
 +---------------+         +---------------------------------------+
 | Payment App   |         | Consumer Group: "email-service"       |
 | (producer)    |         |   Consumer-1: reads payments-P0       |
 |               |         |   Consumer-2: reads payments-P1       |
 | key=user_42   |         |   Consumer-3: reads payments-P2       |
 | --> P1        |         +---------------------------------------+
 | key=user_99   |
 | --> P0        |         +---------------------------------------+
 +---------------+         | Consumer Group: "analytics-service"   |
                           |   Consumer-A: reads payments-P0       |
                           |   Consumer-B: reads payments-P1,P2    |
                           +---------------------------------------+
```

Producers write events to topic partitions on leader brokers. Followers silently replicate. Multiple independent consumer groups each read the full stream of events at their own pace, with Kafka tracking each group's offset position independently. Messages persist on disk until retention expires, regardless of whether anyone has consumed them.

---

**What comes next:** With this mental model of Kafka's internals established, we turn to the producer side — the code that writes events — and explore the patterns your codebase implements, the trade-offs each one represents, and when to choose each one in production.
