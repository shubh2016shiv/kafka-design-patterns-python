# Chapter 3: Producer Patterns — How to Write Events to Kafka

## What a Producer Actually Does

A Kafka producer is any piece of code that writes messages to a Kafka topic. At its most basic level, this involves three steps: establishing a connection to the Kafka cluster, serializing your data (converting a Python dictionary or object into bytes that can be sent over the network), and calling the `produce` method to hand the message off to Kafka's internal send buffer.

That last part — the internal send buffer — is important to understand, because the Kafka client library does not immediately send each message over the network the moment you call `produce`. Instead, it batches messages together in an in-memory buffer and sends them in groups for efficiency. This is invisible to most application code, but it has implications: calling `produce` is non-blocking (it returns almost instantly), but the actual network delivery happens asynchronously in a background thread. This is why your codebase consistently uses `poll()` and `flush()` — these calls force the library to process its delivery queue and trigger your callback functions.

```
Your Code                    Kafka Client Library              Kafka Broker
    |                              |                                |
    |  producer.produce(msg1)      |                                |
    |----------------------------->|                                |
    |  (returns immediately)       |  [msg1 added to buffer]       |
    |                              |                                |
    |  producer.produce(msg2)      |                                |
    |----------------------------->|                                |
    |                              |  [msg2 added to buffer]       |
    |                              |                                |
    |  producer.poll(0)            |                                |
    |----------------------------->|                                |
    |                              |--- batch send [msg1, msg2] -->|
    |                              |<-- delivery ack --------------|
    |                              |                                |
    |  (delivery callback fires)   |                                |
    |<-----------------------------|                                |
```

The delivery callback is how you learn whether your message actually arrived at the broker. Until the callback fires, you do not know if the delivery succeeded or failed. This is a fundamental pattern in all Kafka producer code, and every producer pattern in your codebase is organized around managing this asynchronous delivery lifecycle.

## Producer Configuration: The Parameters That Matter Most

Before diving into patterns, there are four producer configuration parameters that every production engineer must understand deeply, because they control the fundamental behavior and trade-offs of every message you send.

The first is `acks` (acknowledgments). This setting controls how many brokers must confirm receipt of a message before the producer considers it "sent successfully." Setting `acks=0` means the producer fires and forgets — the message is never acknowledged, there is no durability guarantee whatsoever, and you get maximum throughput at the cost of potential data loss. Setting `acks=1` means only the partition leader must acknowledge — fast, but if the leader crashes before replicating, data is lost. Setting `acks=all` (or `acks=-1`) means all in-sync replicas must acknowledge — the slowest option, but also the strongest guarantee. For anything involving money, user data, or critical business events, you should always use `acks=all`.

The second is `enable.idempotence`. Setting this to `true` tells the Kafka producer to assign a unique sequence number to each message, which allows the broker to detect and discard duplicate messages that can occur when the producer retries after a failed acknowledgment (the message was delivered but the ack was lost in a network blip). Enabling idempotence essentially upgrades your delivery guarantee to exactly-once within the producer's lifetime, at essentially no performance cost. This should be the default in all new production code.

The third is `retries` and `retry.backoff.ms`. When a produce request fails transiently (network timeout, leader election in progress), the producer can automatically retry. The number of retries and the delay between them are configured by these parameters. With idempotence enabled, retries are safe. Without idempotence, retries can cause duplicates.

The fourth is `linger.ms`. This controls how long the producer waits, in milliseconds, before sending a batch — even if the batch is not full. A value of 0 means send immediately (lowest latency, but smaller batches). A value of 5 means wait up to 5 milliseconds to accumulate more messages (slightly higher latency, but much higher throughput due to larger batches). For high-volume systems, a `linger.ms` of 5 to 20 can dramatically improve throughput with minimal latency impact.

## Pattern 1: The Simple Producer

The simplest possible producer connects, sends a message, and disconnects. Your `simple_producer.py` demonstrates this pattern. It is the right tool when you have a script or a one-off job that needs to publish a single event or a small batch of events and then terminate.

```
Simple Producer lifecycle:

START
  |
  v
Create Producer Connection
  |
  v
Call produce(topic, key, value, callback)
  |
  v
Call poll(1)   <-- wait up to 1 second for delivery events
  |
  v
Call flush(timeout=10)  <-- wait for ALL pending messages to deliver
  |
  v
Producer falls out of scope, connection closes
  |
  v
END
```

The pattern works, but it has a significant inefficiency: every time you call this code, you pay the cost of establishing a new connection to the Kafka cluster. This includes TLS handshaking, authentication, metadata fetching (learning about brokers and topic configurations), and warming up internal buffers. For a script that runs once, this is fine. For a web application that receives thousands of requests per second, creating a new producer per request would be catastrophic.

The production rule for simple producers is: use them for batch scripts, data migration jobs, testing, and one-off event publishing. Never use them inside a hot code path that executes on every incoming request.

## Pattern 2: The Singleton Producer

The Singleton pattern solves the connection overhead problem by ensuring your application maintains exactly one producer instance for its entire lifetime. Your `singleton_producer.py` implements this with a classic thread-safe singleton using Python's double-checked locking pattern.

The insight is that a Kafka producer is a long-lived, stateful object managing a pool of connections to the broker cluster. There is no benefit to having multiple producers in the same process — they would all need their own connections, their own in-memory buffers, and their own background threads. One producer, shared across the entire application, is both more efficient and easier to monitor.

```
WITHOUT Singleton (problematic):

Request 1 arrives ---> [create producer] ---> send ---> [destroy producer]
Request 2 arrives ---> [create producer] ---> send ---> [destroy producer]
Request 3 arrives ---> [create producer] ---> send ---> [destroy producer]
                        ^^^^^^^^^^^^^^^^^^^
                        Each creation costs: TCP connect + TLS + auth + metadata
                        On a busy service: 1000+ unnecessary connections per second


WITH Singleton:

Application starts ---> [create ONE producer]
                              |
Request 1 arrives ----------->|----> send (reuses existing connection)
Request 2 arrives ----------->|----> send (reuses existing connection)
Request 3 arrives ----------->|----> send (reuses existing connection)
                              |
Application exits ---> [flush + close the ONE producer]
```

The thread safety of the singleton is critical. Your implementation uses `threading.Lock()` and the double-checked locking pattern: it first checks whether the instance exists without acquiring the lock (fast path for the common case after initialization), and only acquires the lock and checks again if the instance appears to be null. This avoids unnecessary lock contention after startup.

```python
# The double-checked locking pattern (from your singleton_producer.py):

if cls._instance is None:          # First check (no lock, fast)
    with cls._lock:                # Acquire lock only if needed
        if cls._instance is None:  # Second check (with lock, safe)
            cls._instance = super().__new__(cls)
```

The `atexit.register(self._cleanup)` call in your singleton is an important production detail. When the Python process exits (due to SIGTERM, a normal shutdown, or even an unhandled exception), Python runs the registered cleanup functions. Your cleanup calls `flush(60.0)` — meaning it gives the producer up to 60 seconds to deliver all pending messages before the process dies. Without this, any messages in the producer's internal buffer at shutdown time would be silently lost.

The health monitoring in your singleton — tracking `_message_count`, `_error_count`, and computing an error rate — is a production necessity. If more than 10% of messages are failing delivery, the singleton marks itself as unhealthy and refuses to accept new messages. This is a form of **fail-fast** behavior: it is better to loudly reject new messages and surface the problem than to silently queue messages that have no chance of being delivered.

**When to use the Singleton:** In any long-running service (web application, API server, background daemon) that publishes Kafka messages as part of its normal operation. This is the default pattern for most microservices.

**When not to use it:** When different parts of your application need radically different producer configurations — for example, one part needs `acks=all` with high durability and another part needs `acks=0` with maximum throughput. In this case, you might use two separate producers with different configurations, possibly implemented as a small pool rather than a strict singleton.

## Pattern 3: The Topic Routing Producer

The Topic Routing Producer adds a layer of business logic on top of the singleton: instead of the calling code deciding which topic to write to, the producer contains routing rules that automatically direct messages to the appropriate topic based on their priority or content.

Your `topic_routing_producer.py` implements this for a system with multiple priority tiers. The core idea is that high-priority events (a payment from a VIP customer, a critical system alert) should go to a topic with more partitions, more replicas, and consumers configured to process them with minimal latency. Low-priority events (background analytics, batch jobs) should go to a separate topic with fewer resources.

```
Without Topic Routing (routing logic in every caller):

Service A: if vip: send("payments-high") else: send("payments-low")
Service B: if vip: send("payments-high") else: send("payments-low")
Service C: if urgent: send("payments-high") else: send("payments-low")
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
           Routing logic duplicated in every service.
           If you rename a topic, you change 50 files.
           If routing rules change, they change inconsistently.


With Topic Routing Producer (routing logic in one place):

Service A: router.send("payment-service", data, priority=HIGH)
Service B: router.send("payment-service", data, priority=LOW)
Service C: router.send("payment-service", data, priority=HIGH)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
           Routing logic lives in the router.
           Topic names are an implementation detail, hidden from callers.
           Routing rules can change without touching any service code.
```

The routing producer also serves as the appropriate place to enforce naming conventions. In enterprise systems, topic names often encode information about the environment (dev, staging, production), the service, the priority tier, and the version. Centralizing this logic means all topics are named consistently and the naming convention is enforced uniformly.

**The production trade-off to understand:** The routing producer adds a layer of indirection. If a junior engineer calls `router.send_high_priority(data)` and the routing rules are wrong, the message goes to the wrong topic and the error is non-obvious. The benefit — centralized, consistent routing — must be weighed against the added complexity. This pattern is most valuable when you have many services all publishing to the same set of topics, making the centralized routing rules worth the investment.

## Pattern 4: The Resilient Producer

The Resilient Producer is the most sophisticated pattern in your codebase. It combines three separate resilience mechanisms — the Circuit Breaker, the Bulkhead, and Retry with Exponential Backoff — into a unified producer that can withstand failures in the Kafka cluster without crashing or cascading failures into the calling service.

Understanding why this pattern exists requires understanding a failure scenario that simple producers cannot handle gracefully. Suppose your Kafka cluster goes through a leader election — the broker holding a partition's leadership role crashes and another broker takes over. During this election, which typically takes 10 to 30 seconds, any produce requests to that partition will fail. What should a simple producer do? If it retries indefinitely, it blocks the thread. If it fails immediately, it potentially loses data. If it queues messages in memory, it may run out of memory during a prolonged outage.

The resilient producer addresses this by integrating three patterns that each handle a different failure scenario.

### Sub-Pattern: Circuit Breaker

The circuit breaker is borrowed from electrical engineering. A circuit breaker in a building monitors the current flowing through it. If the current exceeds safe levels (indicating a fault), it trips — breaking the circuit and preventing damage. After some time, you reset it manually to see if the fault is resolved.

In software, a circuit breaker monitors the error rate of an operation. While failures are rare, the circuit is "closed" and requests flow through normally. When the failure rate exceeds a threshold (your `failure_threshold = 5` in `resilient_producer.py`), the circuit "opens" and immediately rejects all further requests without even trying — this is "failing fast." After a configured timeout (`recovery_timeout = 60` seconds), the circuit moves to "half-open" and allows one test request through. If that succeeds, the circuit closes and normal operation resumes.

```
Circuit Breaker State Machine:

            failures < threshold
CLOSED <----------------------------- HALF-OPEN
  |   normal operation, passes         |   one test request allowed
  |   all requests through             |
  |                                    |
  | failures >= threshold              | test succeeds
  v                                    |
OPEN  ------- timeout elapsed -------->+
  |   fails all requests immediately
  |   without trying (fail fast)
  |
  v
(protects downstream from pointless retry storms)
```

The critical production insight about circuit breakers: they protect your entire service, not just Kafka. If you do not have a circuit breaker and Kafka becomes slow or unavailable, your producer threads will block waiting for timeouts. Those threads belong to your web server or application thread pool. As more requests arrive and more threads block, your application thread pool exhausts, and your entire service becomes unresponsive — not just to Kafka, but to all incoming requests. **A Kafka outage becomes a total service outage**. The circuit breaker prevents this by failing fast and freeing threads immediately when Kafka is known to be unhealthy.

### Sub-Pattern: Bulkhead Isolation

The Bulkhead pattern is also borrowed from physical engineering — specifically, the watertight compartments in a ship's hull. If one compartment floods, the bulkheads prevent the flood from spreading to other compartments and sinking the ship.

In software, the bulkhead pattern uses a bounded thread pool (backed by `ThreadPoolExecutor` in your code) with a fixed maximum size. If 100 threads are available for Kafka operations and all 100 are blocked waiting for a slow Kafka response, the bulkhead rejects any new Kafka requests with a `BulkheadFullException` rather than creating thread 101, 102, 103... indefinitely. The calling code (your web server, your API handler) is insulated from the failure and can take alternative action — queue the request for later, return a degraded response, or log the event for retry.

```
WITHOUT Bulkhead:

Request 1 -> Kafka (slow) -> Thread 1 blocked...
Request 2 -> Kafka (slow) -> Thread 2 blocked...
...
Request 200 -> creates Thread 200 -> blocked...
Request 201 -> creates Thread 201 -> blocked...
[JVM/Python process runs out of memory or file descriptors]
[Service crashes]


WITH Bulkhead (max_concurrent=10):

Request 1  -> Thread 1 -> Kafka (slow) -> blocked
Request 2  -> Thread 2 -> Kafka (slow) -> blocked
...
Request 10 -> Thread 10 -> Kafka (slow) -> blocked
Request 11 -> [Bulkhead is full] -> BulkheadFullException
              [caller handles gracefully: logs, queues, returns 503]
[Service stays up; only Kafka operations fail, nothing else]
```

### Sub-Pattern: Retry with Exponential Backoff

The retry mechanism in your `send_with_retry` method uses **exponential backoff** — each successive retry waits twice as long as the previous one. If the first retry waits 1 second, the second waits 2 seconds, the third waits 4 seconds, the fourth waits 8 seconds.

```
Exponential Backoff (retry_delay=1.0, multiplier=2.0):

Attempt 1: FAIL
Wait:  1.0 second
Attempt 2: FAIL
Wait:  2.0 seconds
Attempt 3: FAIL
Wait:  4.0 seconds
Attempt 4: FAIL
Wait:  8.0 seconds
Attempt 5: SUCCESS ✓

vs. Fixed Retry (retry every 1 second):

Attempt 1: FAIL
Wait: 1s
Attempt 2: FAIL    <-- if 1000 producers all retry every 1 second,
Wait: 1s              that is 1000 requests/second hammering
Attempt 3: FAIL       an already-struggling Kafka cluster
...                   making recovery SLOWER (retry storm)
```

The exponential backoff is essential to avoid **retry storms** — the phenomenon where many failed clients all retry simultaneously at the same interval, creating a traffic spike that overwhelms the system trying to recover, which causes more failures, which causes more retries, in a destructive feedback loop. Adding a small random jitter to the delay (your `max_retry_delay` cap handles the upper bound) further distributes retry traffic.

**When to use the Resilient Producer:** In any service where a Kafka outage should not cause a complete service failure, where you are handling money or other critical data, or where your service has strict availability SLAs. The added complexity is substantial — you are operating three patterns simultaneously — but for mission-critical systems, this complexity is justified and the alternative (unprotected failures causing cascading outages) is worse.

## The Delivery Callback: Your Window into What Actually Happened

Every producer pattern in your codebase uses a delivery callback. This function is the only reliable way to know whether a message was actually delivered to Kafka. The callback receives either an error object (delivery failed) or a message object (delivery succeeded, including the partition and offset where it was stored).

```python
def delivery_callback(err, msg):
    if err is not None:
        # This message was NOT delivered. You must decide:
        # 1. Log it and accept the loss (only for non-critical data)
        # 2. Write it to a local fallback (database, file) for retry
        # 3. Raise an exception to alert the calling code
        logger.error(f"Delivery failed: {err}")
    else:
        # This message IS durably stored in Kafka at:
        partition = msg.partition()
        offset = msg.offset()
        # You can now safely proceed with downstream actions
        # that depend on this message having been written
```

A production pattern that your codebase hints at but does not fully implement is writing failed deliveries to a local database or file as a **local fallback queue**. If Kafka is unavailable and the circuit breaker is open, instead of losing the message or blocking indefinitely, you write it to a local table. A separate background process periodically reads from this local table and retries delivery to Kafka. This pattern, sometimes called the **Outbox Pattern**, guarantees at-least-once delivery even during prolonged Kafka outages, at the cost of additional infrastructure (a local database or persistent queue).

---

**What comes next:** The producer side is only half the picture. Chapter 4 covers consumer patterns — how to read events from Kafka efficiently, how to handle failures without losing messages, and the critical trade-offs between sequential and concurrent processing.
