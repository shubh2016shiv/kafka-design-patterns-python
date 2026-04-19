# Kafka Quick Reference Cheatsheet

## Core Concepts

| Concept | One-line definition | Why it exists | One practical decision it affects |
|---|---|---|---|
| `topic` | Named stream of related records | Organizes events by category | How you split business domains and consumer ownership |
| `partition` | One ordered lane inside a topic | Gives Kafka parallelism without losing all ordering | How much concurrency one consumer group can achieve |
| `offset` | Position of a record inside one partition | Lets consumers track progress through the log | When to commit and how replay/recovery works |
| `consumer group` | One logical application reading a topic cooperatively | Lets consumers share partition work without duplicate processing inside the group | Whether consumers should collaborate or read independently |
| `retention` | Replay window for how long Kafka keeps records | Kafka is durable, but not infinite storage | How much history you can recover or replay from Kafka alone |

## Topic, Partition, Offset, Group In One View

```text
topic: orders

orders-0
  offset 0 -> key=customer_99  order_created

orders-1
  offset 0 -> key=customer_42  order_created
  offset 1 -> key=customer_42  payment_authorized
  offset 2 -> key=customer_42  order_shipped

orders-2
  (empty for now)
```

```text
consumer group: billing-service

consumer A -> orders-0
consumer B -> orders-1
consumer C -> orders-2
```

## Same Group vs Different Groups

```text
CASE 1: SAME GROUP

topic: orders
partitions: 1
group: billing-service

orders-0 -> consumer A
consumer B -> idle
consumer C -> idle

Rule:
within one consumer group, one partition is owned by only one consumer at a time
```

```text
CASE 2: DIFFERENT GROUPS

topic: orders
partitions: 1

group billing-service   -> reads orders-0
group analytics-service -> reads orders-0
group fraud-service     -> reads orders-0

Rule:
different consumer groups read independently and keep separate offsets
```

## What A Kafka Record Looks Like

```json
{
  "topic": "orders",
  "partition": 1,
  "offset": 102,
  "key": "customer_42",
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

## Producer Config Quick Table

| Config | What it really controls | Safe default mindset | Watch out for |
|---|---|---|---|
| `acks` | Proof threshold for a successful write | Use `acks=all` for important records | `acks=1` can still lose acknowledged data |
| `enable.idempotence` | Whether retries can duplicate writes | Usually `true` in serious production | Retries without idempotence can duplicate records |
| `retries` | Patience for transient failures | Keep enabled | Zero retries turns short issues into avoidable failures |
| `retry.backoff.ms` | Spacing between retries | Non-zero | Too low can create retry storms |
| `delivery.timeout.ms` | Total time before giving up on a record | Bound it to app latency tolerance | Too high hides failures; too low fails too quickly |
| `linger.ms` | Wait time to build batches | Small positive value for most systems | Too high adds latency; zero wastes batching opportunity |
| `batch.size` | Maximum batch size per partition | Tune later, not first | Oversized batches increase memory pressure |
| `compression.type` | CPU vs bandwidth/storage trade-off | Prefer `lz4`, `snappy`, or `zstd` for volume | Compression is not free CPU-wise |
| `max.in.flight.requests.per.connection` | Unacknowledged send concurrency per connection | Leave defaults unless tuning deeply | More concurrency can complicate failure ordering behavior |

## Consumer Config Quick Table

| Config | What it really controls | Safe default mindset | Watch out for |
|---|---|---|---|
| `group.id` | Which consumers cooperate as one app | One logical service = one group | Wrong group choice causes missing or duplicated work |
| `enable.auto.commit` | Whether Kafka guesses when you are done | Usually `false` for important consumers | Auto-commit can mark progress before work is truly finished |
| `auto.offset.reset` | Start point when no valid saved offset exists | `earliest` for replay, `latest` for live-only | `latest` can silently skip retained history |
| `max.poll.interval.ms` | How long Kafka trusts you between polls | Set above worst-case processing time | Too low causes consumer eviction and rebalances |
| `session.timeout.ms` | How quickly the group gives up on silent consumers | Balance failover speed and noise tolerance | Too low causes churn; too high slows failover |
| `heartbeat.interval.ms` | How often the consumer proves it is alive | Tune with session timeout, not alone | Bad pairing with session timeout causes instability |
| `max.poll.records` | How much work one poll loop takes on | Keep moderate | Too high plus slow work causes lag and timeout risk |

## Topic And Broker Quick Table

| Config | What it really controls | Safe default mindset | Watch out for |
|---|---|---|---|
| `replication.factor` | Number of copies per partition | `3` for important production topics | RF alone does not guarantee safe acknowledged writes |
| `min.insync.replicas` | Safe-write floor for in-sync replicas | Often `2` with RF=`3` | Too high can block writes during broker loss; too low weakens durability |
| `retention.ms` | Time-based replay window | Pick based on true recovery needs | Consumers can fall behind longer than retention and lose history |
| `retention.bytes` | Size-based replay ceiling | Use carefully when disk matters | Can delete data earlier than the time window suggests |
| `cleanup.policy` | Delete vs compaction behavior | `delete` for event streams, `compact` for changelogs | Compacted topics are not full event history topics |

## If You See X, Think Y

| If you see X | Think Y first |
|---|---|
| lag growing steadily | consumer capacity issue, downstream slowdown, or hot partition |
| one partition much worse than others | hot key, stuck consumer instance, skewed workload |
| repeated rebalances | processing taking too long, crashes, session timing, or network instability |
| producer failures spiking | broker health, disk pressure, leader elections, or ISR / `min.insync.replicas` issues |
| DLQ filling up | schema drift, code regression, or dependency behavior change |

## Lag In One Line

```text
lag = latest available offset - committed offset
```

Interpretation:

- low stable lag -> consumer is keeping up
- growing lag -> work is arriving faster than it is being completed
- lag close to retention boundary -> replay/data-loss risk

## Retention In One Line

```text
retention = replay window, not consumer-aware storage
```

Meaning:

- Kafka does not keep records just because a consumer still wants them
- Kafka deletes based on topic policy

## Commit Timing In One Line

```text
commit early -> risk silent loss
commit after processing -> risk duplicates, but safer
```

That is why most serious systems prefer:

- at-least-once delivery
- idempotent consumer behavior

## Priority In One Line

```text
priority in Kafka = separate lane, not broker magic
```

Real priority usually means:

- separate topic
- separate consumer capacity
- separate alerts
- separate lag expectations

## Resilience Pattern Mapping

| Failure mode | Pattern to think of first |
|---|---|
| downstream dependency can be flooded | rate limiting |
| consumer itself is taking too much work | backpressure |
| one dependency starves unrelated work | bulkhead |
| dependency is clearly down and timeouts keep piling up | circuit breaker |
| consumer should reject irrelevant or unsafe data early | filtering |
| one poison record blocks the partition | DLQ |

## Fast Mental Model

```text
topic       = category
partition   = lane
offset      = line number in the lane
group       = one team reading cooperatively
retention   = how long the lane stays replayable
producer    = decides how data enters Kafka
consumer    = decides how work is completed safely
lag         = tells you whether the system is keeping up
```
