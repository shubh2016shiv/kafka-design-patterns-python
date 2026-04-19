# Appendix: Kafka Configurations You Must Know in Production

## How to Use This Appendix

This appendix is built for quick recall, not long-form learning.

Use it when you need to answer:

- what does this config actually control?
- why does it exist?
- what is the safe default mindset?
- when should I change it?
- what usually goes wrong when teams misunderstand it?

Throughout this appendix:

- **safe default mindset** means the default direction a careful production team should usually start with
- **quick interview answer** is the shortest production-quality explanation you can say out loud

## Producer Configs

## `acks`

- **What it controls:** How many broker acknowledgments the producer waits for before treating a send as successful.
- **Why it exists:** To trade durability against latency and throughput.
- **Safe default mindset:** Start with `acks=all` for important business records.
- **When to change it:** Move toward `acks=1` or `acks=0` only when the data is lower value and throughput matters more than strict durability.
- **What can go wrong if misunderstood:** `acks=1` can still lose acknowledged data if the leader dies before followers catch up; `acks=0` can lose data silently.
- **Quick interview answer:** "`acks` is the proof threshold for a successful write; `acks=all` is the strongest durability posture."

## `enable.idempotence`

- **What it controls:** Whether the producer can suppress duplicate writes caused by safe retries within the producer session.
- **Why it exists:** Retries are common in distributed systems, and without idempotence a retry can store the same record twice.
- **Safe default mindset:** Start with `enable.idempotence=true` for almost all serious production producers.
- **When to change it:** Disable only if you have a very specific performance or compatibility reason and you understand duplicate risks clearly.
- **What can go wrong if misunderstood:** Teams enable retries but forget idempotence, then create duplicate records during transient broker or network failures.
- **Quick interview answer:** "Idempotence makes producer retries much safer by preventing duplicate writes from the same producer session."

## `retries`

- **What it controls:** How many times the producer retries failed sends.
- **Why it exists:** Many Kafka write failures are temporary, not permanent.
- **Safe default mindset:** Keep retries enabled for production producers.
- **When to change it:** Increase when short broker or network interruptions are common; keep moderate when user-facing paths must fail faster.
- **What can go wrong if misunderstood:** Zero retries turns short transient failures into unnecessary application errors.
- **Quick interview answer:** "`retries` controls how patient the producer is with transient failures."

## `retry.backoff.ms`

- **What it controls:** Delay between producer retry attempts.
- **Why it exists:** Immediate retries can create retry storms and worsen broker recovery.
- **Safe default mindset:** Use a non-zero backoff.
- **When to change it:** Increase when failures are bursty or downstream recovery needs breathing room.
- **What can go wrong if misunderstood:** Setting it too low hammers an already unhealthy broker or network path.
- **Quick interview answer:** "`retry.backoff.ms` spaces out retries so failure recovery is not made worse by retry storms."

## `delivery.timeout.ms`

- **What it controls:** Maximum total time the producer will spend trying to deliver a record before giving up.
- **Why it exists:** Retries need a total deadline or user-facing paths can hang too long.
- **Safe default mindset:** Use a bounded timeout that fits the application's latency tolerance.
- **When to change it:** Shorten it for user-facing low-latency paths; lengthen it for more resilient background publishing.
- **What can go wrong if misunderstood:** Too high makes failures surface too slowly; too low turns short-lived broker issues into avoidable send failures.
- **Quick interview answer:** "`delivery.timeout.ms` is the producer's final patience limit for a record."

## `linger.ms`

- **What it controls:** How long the producer waits to collect more records into a batch before sending.
- **Why it exists:** Batching improves throughput and efficiency.
- **Safe default mindset:** Use a small positive value for most production systems, often `5` to `20` ms.
- **When to change it:** Lower it for latency-sensitive user paths; raise it for high-volume throughput-oriented pipelines.
- **What can go wrong if misunderstood:** Setting it to `0` everywhere leaves throughput on the table; setting it too high can add visible user-facing latency.
- **Quick interview answer:** "`linger.ms` trades a small amount of latency for better batching efficiency."

## `batch.size`

- **What it controls:** Maximum batch size per partition before the producer sends.
- **Why it exists:** Larger batches improve throughput and amortize protocol overhead.
- **Safe default mindset:** Leave defaults alone until you have real throughput pressure.
- **When to change it:** Increase for high-volume topics where batching is already helping and memory headroom is available.
- **What can go wrong if misunderstood:** Oversizing batches can increase memory pressure and make low-traffic paths wait without much benefit.
- **Quick interview answer:** "`batch.size` controls how large the producer's outgoing packet groups can get."

## `compression.type`

- **What it controls:** Compression algorithm used for outgoing producer batches.
- **Why it exists:** To reduce network traffic and broker disk usage.
- **Safe default mindset:** Prefer `lz4`, `snappy`, or `zstd` for high-volume production workloads.
- **When to change it:** Tune based on CPU budget, payload size, and volume; latency-sensitive tiny payloads benefit less.
- **What can go wrong if misunderstood:** Teams optimize only for throughput and forget the CPU trade-off, or disable compression and overspend on bandwidth and storage.
- **Quick interview answer:** "`compression.type` trades CPU for network and storage efficiency."

## `max.in.flight.requests.per.connection`

- **What it controls:** How many produce requests can be unacknowledged at once on one connection.
- **Why it exists:** More in-flight requests can improve throughput, but they complicate ordering behavior during failures.
- **Safe default mindset:** Do not tune this early; let client defaults and idempotence-safe behavior carry most systems.
- **When to change it:** Only in advanced throughput tuning after understanding ordering and retry implications.
- **What can go wrong if misunderstood:** Teams increase concurrency without realizing it can make failure behavior harder to reason about.
- **Quick interview answer:** "`max.in.flight.requests.per.connection` is a throughput-vs-ordering-safety tuning knob."

## Consumer Configs

## `group.id`

- **What it controls:** Which consumer group the consumer belongs to.
- **Why it exists:** Kafka needs to know which consumers cooperate as one logical application.
- **Safe default mindset:** Use one `group.id` per logical service/application.
- **When to change it:** Change it only when you intentionally want independent consumption rather than shared work.
- **What can go wrong if misunderstood:** Reusing a group across unrelated consumers causes unexpected partition sharing and missing work; using different groups accidentally duplicates work.
- **Quick interview answer:** "`group.id` defines which consumers share partition work as one logical reader."

## `enable.auto.commit`

- **What it controls:** Whether the client commits offsets automatically on a timer.
- **Why it exists:** To simplify consumer code when correctness requirements are light.
- **Safe default mindset:** Start with `enable.auto.commit=false` for serious production consumers.
- **When to change it:** Enable auto-commit only for simpler, low-stakes pipelines where occasional inconsistency is acceptable.
- **What can go wrong if misunderstood:** Offsets can be committed before application work is truly done, causing skipped records after failures.
- **Quick interview answer:** "`enable.auto.commit` decides whether Kafka guesses when you're done or your application decides explicitly."

## `auto.offset.reset`

- **What it controls:** Where to start when no usable committed offset exists.
- **Why it exists:** New groups or expired offsets need a fallback starting point.
- **Safe default mindset:** Use `earliest` when replay/history matters; use `latest` when only future data matters.
- **When to change it:** Choose based on whether bootstrap/replay is expected for that group.
- **What can go wrong if misunderstood:** A new service meant to backfill history starts at `latest` and misses retained records; a low-stakes live-only consumer starts at `earliest` and unexpectedly replays old traffic.
- **Quick interview answer:** "`auto.offset.reset` is the fallback start position when no valid saved offset exists."

## `max.poll.interval.ms`

- **What it controls:** Maximum time allowed between polls before Kafka considers the consumer unhealthy.
- **Why it exists:** Kafka needs to detect consumers that are no longer making progress.
- **Safe default mindset:** Size it above realistic worst-case processing time, not average time.
- **When to change it:** Increase for long-running processing; redesign the consumer if polling is blocked for too long.
- **What can go wrong if misunderstood:** Healthy-but-busy consumers get kicked out of the group, causing repeated rebalances and duplicate work.
- **Quick interview answer:** "`max.poll.interval.ms` is how long Kafka trusts you to stay busy before deciding you are effectively dead."

## `session.timeout.ms`

- **What it controls:** How long the coordinator waits without heartbeats before removing a consumer from the group.
- **Why it exists:** Failed consumers should not hold partitions forever.
- **Safe default mindset:** Choose a value that balances faster failover with tolerance for transient blips.
- **When to change it:** Lower it for faster failure detection if the network is stable; raise it if transient pauses cause unnecessary group churn.
- **What can go wrong if misunderstood:** Too low causes noisy rebalances; too high slows failover.
- **Quick interview answer:** "`session.timeout.ms` is how quickly the group gives up on a silent consumer."

## `heartbeat.interval.ms`

- **What it controls:** How often the consumer sends heartbeats to stay in the group.
- **Why it exists:** Group membership requires ongoing liveness signals.
- **Safe default mindset:** Tune it in relation to `session.timeout.ms`, not in isolation.
- **When to change it:** Mostly when you are already adjusting session timeout behavior.
- **What can go wrong if misunderstood:** Poor heartbeat/session tuning can create unnecessary rebalances or slower failure detection.
- **Quick interview answer:** "`heartbeat.interval.ms` is how often the consumer proves it is still alive to the group coordinator."

## `max.poll.records`

- **What it controls:** Maximum number of records returned by one poll.
- **Why it exists:** To control batch size, memory pressure, and how much work each poll loop takes on.
- **Safe default mindset:** Keep it moderate so one poll does not create too much in-flight work.
- **When to change it:** Lower it when processing is heavy or latency-sensitive; raise it for efficient batch handling when downstream work can keep up.
- **What can go wrong if misunderstood:** Giant polls combined with slow processing increase lag, memory pressure, and timeout risk.
- **Quick interview answer:** "`max.poll.records` controls how big a bite the consumer takes each time it asks Kafka for work."

## Topic And Broker Configs

## `replication.factor`

- **What it controls:** Number of total copies Kafka keeps for each partition.
- **Why it exists:** To improve durability and broker-failure tolerance.
- **Safe default mindset:** Use `3` for important production topics when broker count allows.
- **When to change it:** Lower only for low-value data or smaller clusters; raise only with a clear durability reason and cost awareness.
- **What can go wrong if misunderstood:** Too low increases data-loss risk; teams often assume RF=3 alone guarantees safe writes without considering `acks` and ISR health.
- **Quick interview answer:** "`replication.factor` controls how many copies of a partition exist, which directly affects durability and failover resilience."

## `min.insync.replicas`

- **What it controls:** Minimum number of in-sync replicas required to acknowledge safe writes when the producer asks for strong durability.
- **Why it exists:** To prevent Kafka from pretending a write is safe when too few replicas actually hold it.
- **Safe default mindset:** Pair `min.insync.replicas=2` with `replication.factor=3` for critical topics.
- **When to change it:** Lower only when availability matters more than strict durability; keep aligned with broker count and failure expectations.
- **What can go wrong if misunderstood:** Teams set RF=3 but leave write safety weak; or they set `min.insync.replicas` too aggressively without understanding write availability trade-offs during broker loss.
- **Quick interview answer:** "`min.insync.replicas` is the safe-write floor for how many healthy replicas Kafka needs before acknowledging a durable write."

## `retention.ms`

- **What it controls:** Time-based retention window for records on a topic.
- **Why it exists:** Kafka is a replayable log, but not infinite storage.
- **Safe default mindset:** Choose it as a replay window based on recovery and backfill needs, not as an arbitrary number.
- **When to change it:** Increase for replay/backfill-heavy systems; decrease for urgent short-lived streams when disk footprint matters and recovery is tightly monitored.
- **What can go wrong if misunderstood:** Consumers can fall behind longer than retention and lose access to required history.
- **Quick interview answer:** "`retention.ms` defines how long Kafka keeps records available for replay before they become eligible for deletion."

## `retention.bytes`

- **What it controls:** Size-based retention limit, usually per partition.
- **Why it exists:** To cap disk growth even if time-based retention is long.
- **Safe default mindset:** Use it carefully when disk control matters; understand it can delete data earlier than time-based expectations.
- **When to change it:** Add or lower it when ingest rates are high and broker disk risk is more important than maximum replay history.
- **What can go wrong if misunderstood:** Teams think they have seven days of history, but size limits delete data earlier.
- **Quick interview answer:** "`retention.bytes` is a disk ceiling that can shorten your replay window if volume is high."

## `cleanup.policy`

- **What it controls:** Whether the topic uses delete-based retention, compaction, or both.
- **Why it exists:** Some topics are event histories, while others are changelogs keyed by latest state.
- **Safe default mindset:** Use `delete` for normal event streams; use `compact` only when you intentionally want latest-by-key behavior.
- **When to change it:** Choose compaction for state restoration/changelog topics, not as a general default.
- **What can go wrong if misunderstood:** Teams expect full event history from compacted topics or expect latest-state recovery from pure delete-based event streams.
- **Quick interview answer:** "`cleanup.policy` decides whether Kafka behaves like an event history, a key-based changelog, or both."

## Advanced Note: Segment Settings

- **What they control:** How Kafka rolls and stores log segments on disk.
- **Why they exist:** Segment size and roll intervals affect cleanup timing and storage behavior.
- **Safe default mindset:** Do not tune segment settings early unless you have a clear broker-storage or retention-behavior reason.
- **What can go wrong if misunderstood:** Teams tune advanced segment knobs before understanding partitions, retention, and disk fundamentals.
- **Quick interview answer:** "Segment settings are advanced storage-behavior knobs, not first-line production tuning knobs."

## Scenario Bundles

## Critical Business Events

- **Use when:** Orders, payments, audit trails, account lifecycle changes.
- **Producer direction:** `acks=all`, `enable.idempotence=true`, retries enabled, non-zero retry backoff, bounded delivery timeout, small-to-moderate `linger.ms`, compression on.
- **Consumer direction:** `enable.auto.commit=false`, manual commit after processing, moderate `max.poll.records`, `max.poll.interval.ms` above worst-case processing, careful lag alerts.
- **Topic direction:** `replication.factor=3`, `min.insync.replicas=2`, retention chosen as a true recovery window.
- **Interview summary:** "Optimize for no silent loss, controlled duplicates, and clear recoverability."

## Low-Latency User-Facing Path

- **Use when:** User request path directly depends on publish or consume latency.
- **Producer direction:** `acks=all`, idempotence on, retries on, shorter `delivery.timeout.ms`, lower `linger.ms`, conservative compression.
- **Consumer direction:** Manual commit, moderate `max.poll.records`, avoid overly large batches, keep processing path tight.
- **Topic direction:** Enough partitions for spikes, tight monitoring, no fake sharing with slower batch consumers.
- **Interview summary:** "Preserve safety, but avoid batching and timeout choices that add visible latency."

## High-Volume Analytics / Telemetry

- **Use when:** Clickstream, observability, lower-value usage events.
- **Producer direction:** `acks=1` or occasionally `0`, idempotence depends on duplicate sensitivity, larger `linger.ms`, larger `batch.size`, compression strongly preferred.
- **Consumer direction:** Simpler commit posture may be acceptable, larger batches, concurrency guided by throughput.
- **Topic direction:** High partitions if throughput demands it; retention and `retention.bytes` chosen with disk cost in mind.
- **Interview summary:** "Trade some durability for throughput and storage efficiency when business value allows it."

## History Replay / Backfill Consumer

- **Use when:** New service bootstrapping, replay, backfill, catch-up processing.
- **Producer direction:** Not the main concern unless replay writes into Kafka again.
- **Consumer direction:** `auto.offset.reset=earliest`, `enable.auto.commit=false`, manual commit, batch-oriented processing, larger `max.poll.records` only if downstream can safely keep up.
- **Topic direction:** Retention must actually cover the replay window required.
- **Interview summary:** "Replay depends more on retention and consumer offset strategy than on flashy configs."

## External API Enrichment Consumer

- **Use when:** Consumer reads Kafka, calls external API, and enriches downstream records.
- **Producer direction:** Usually standard safe settings if output is important.
- **Consumer direction:** Manual commit, concurrent processing if I/O-bound, `max.poll.interval.ms` sized for long processing, bounded `max.poll.records`, strong lag monitoring.
- **Resilience direction:** Rate limiter, backpressure, maybe circuit breaker, maybe bulkhead, DLQ for malformed or permanently failing records.
- **Interview summary:** "The hard part is not Kafka storage; it is protecting the consumer from the external dependency."

## Common Bad Combinations And Misconceptions

## Bad Combination: `acks=1` + critical business workflow + "we assumed RF=3 was enough"

- **Why it is bad:** Replication alone does not guarantee safe acknowledged writes if the leader crashes before followers catch up.

## Bad Combination: retries enabled + idempotence disabled + duplicates matter

- **Why it is bad:** Transient failures can create duplicate records.

## Bad Combination: `enable.auto.commit=true` + important side effects

- **Why it is bad:** Kafka may record progress before the business work is truly finished.

## Bad Combination: `auto.offset.reset=latest` on a new replay-oriented service

- **Why it is bad:** The service silently misses retained history it was supposed to process.

## Bad Combination: huge `max.poll.records` + slow processing + low `max.poll.interval.ms`

- **Why it is bad:** Large batches take too long, consumers get kicked from the group, and rebalances spike.

## Misconception: "Retention keeps data as long as a consumer still needs it"

- **Correction:** Retention is controlled by topic policy, not by consumer lag or consumer demand.

## Misconception: "More partitions is always better"

- **Correction:** More partitions raise scaling potential, but also increase overhead, rebalance complexity, and hot-partition debugging difficulty.

## Misconception: "Priority just means label the record high"

- **Correction:** Priority in Kafka comes from separate routing, topics, consumers, and alerting, not broker magic.
