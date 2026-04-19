# Chapter 4: Consumer Patterns — How to Read Events from Kafka Safely

## How a Consumer Actually Works

A Kafka consumer is fundamentally a polling loop. Unlike a traditional message queue that pushes messages to a waiting listener, Kafka consumers actively pull messages from the broker by calling `poll()` repeatedly. Each call to `poll()` asks Kafka: "Give me any new messages that have arrived since my last committed offset, up to a configurable maximum batch size."

This pull-based model is one of the design choices that makes Kafka scalable. The consumer controls its own processing rate. If the consumer is slow, it simply polls less frequently and its lag grows — but the broker is not burdened with tracking a queue of pending push deliveries for every consumer. The broker's job is just to serve data from its log when asked.

```
Basic Consumer Poll Loop:

while True:
    messages = consumer.poll(timeout=1.0)   # "give me messages, wait up to 1s"
    
    for msg in messages:
        process(msg)                         # do your actual work
        
    consumer.commit()                        # tell Kafka: "I'm done up to here"
```

Every consumer must belong to a consumer group (specified in the `group.id` configuration). When the consumer starts, it contacts the group coordinator broker to announce its presence. The group coordinator assigns partitions to this consumer. If other consumers in the same group are already running, a **rebalance** occurs — Kafka redistributes the partitions among all current group members so that each partition is assigned to exactly one consumer.

Rebalances are a significant operational concern in production. During a rebalance, **all consumers in the group pause consuming** while the redistribution is negotiated. In a group with many consumers and many partitions, this pause can last several seconds. In systems with strict latency requirements, frequent rebalances are a serious problem. They are triggered by consumers joining or leaving (due to crashes, deployments, or scaling operations) and also by consumers that fail to call `poll()` frequently enough, causing the broker to assume they have died.

The configuration parameter `max.poll.interval.ms` controls how long Kafka will wait between `poll()` calls before declaring the consumer dead and triggering a rebalance. If your message processing takes longer than this interval, your consumer will be kicked out of the group mid-processing. The default is 5 minutes, but if your processing can ever take longer than that, you must increase this value or redesign your processing to complete faster.

## Pattern 1: The Sequential Consumer

The Sequential Consumer processes messages one at a time, in order, before moving on to the next. It is the simplest possible consumer implementation and the right choice when correctness of ordering matters more than throughput.

```
Sequential Consumer Processing Model:

Poll returns: [msg_A, msg_B, msg_C, msg_D]
                |
                v
          process msg_A   <-- blocks until done
                |
                v
          process msg_B   <-- blocks until done
                |
                v
          process msg_C   <-- blocks until done
                |
                v
          process msg_D   <-- blocks until done
                |
                v
          commit offsets
                |
                v
          poll again
```

The critical property of sequential processing is that messages from the same partition are always processed in the exact order Kafka stored them. If Kafka stores events [user_42_created, user_42_updated, user_42_deleted] in that order, a sequential consumer processes them in that order, and your state machine based on those events is always correct.

The offset commitment strategy in sequential processing deserves careful attention. There are two moments when you could commit: before processing (which is dangerous, as discussed in Chapter 2 — you lose the message if you crash) or after processing. Committing after processing is safe, but it creates a subtle issue: if you process messages A through D and commit D, but then crash before the next commit covering messages E through H, those four messages will be reprocessed when you restart. This is the at-least-once delivery model in action. Your code must be idempotent to handle this.

The production decision for sequential consumers comes down to one question: are you processing CPU-bound work (computation that uses the processor heavily, like image transformation or cryptography) or I/O-bound work (work that mostly waits for external systems, like API calls or database queries)? Sequential processing is efficient for CPU-bound work, where one thread can keep a CPU core busy. For I/O-bound work, sequential processing leaves CPU cores idle while waiting for network responses — and this is where the concurrent consumer pattern becomes valuable.

**When to use sequential consumers:** Financial transactions where ordering matters (debit must happen before credit reversal), state machine updates (a user's lifecycle must go through states in order), audit log processing where sequence is part of the audit trail, and any situation where processing message N+1 depends on the result of processing message N.

## Pattern 2: The Concurrent Consumer

The Concurrent Consumer is designed for a specific problem: your processing is bottlenecked not by CPU speed or Kafka throughput, but by waiting for external systems to respond. If processing a message requires calling a REST API that takes 200 milliseconds to respond, a sequential consumer with one thread can only handle 5 messages per second, because it spends 200ms waiting on each one. With 20 concurrent threads all making API calls simultaneously, you can handle 100 messages per second — the same math that makes web servers use thread pools rather than single threads.

```
Sequential Consumer (I/O bound processing):

[msg1] ---200ms wait---> done
[msg2]                   ---200ms wait---> done
[msg3]                                     ---200ms wait---> done
Time: 0ms                200ms             400ms             600ms

Throughput: 5 msg/sec


Concurrent Consumer (same workload, 20 threads):

Thread 1:  [msg1] ---200ms wait---> done
Thread 2:  [msg2] ---200ms wait---> done
Thread 3:  [msg3] ---200ms wait---> done
...
Thread 20: [msg20] ---200ms wait---> done
           |
All start simultaneously at time 0ms, all finish at ~200ms

Throughput: ~100 msg/sec (20x improvement)
```

Your `concurrent_consumer.py` implements this pattern using a `ThreadPoolExecutor` — a fixed pool of worker threads managed by Python's standard library. The main thread's only job is to call `poll()` and submit messages to the thread pool. Worker threads do the actual processing. This separation is important: if you create an unbounded number of threads (one thread per message), you will eventually exhaust system resources. A bounded thread pool with a fixed maximum ensures you have controlled, predictable resource usage regardless of how fast messages arrive.

The complexity of concurrent consumers comes from two sources. First, **offset management becomes non-trivial**. If Thread 1 is processing message at offset 100 and Thread 3 has already finished processing message at offset 102, you cannot commit offset 102 yet — doing so would skip offset 100, and if you crash, offset 100 would be lost. Your consumer must track which offsets have been completed and only commit the highest offset for which all lower offsets are also complete.

```
Concurrent Offset Management Problem:

Partition 0 messages dispatched to thread pool:
  offset 100 -> Thread A (still processing...)
  offset 101 -> Thread B (finished, waiting)
  offset 102 -> Thread C (finished, waiting)
  offset 103 -> Thread D (still processing...)

Safe to commit: offset 99 (nothing completed continuously from 100 onward)

Time passes:
  offset 100 -> Thread A (FINISHED)

Now safe to commit: offset 103 (100, 101, 102 all complete; 103 still in progress)
Actually safe to commit: offset 102 (100, 101, 102 complete; 103 not yet)
```

The second source of complexity is **backpressure**. If messages arrive faster than your thread pool can process them, the queue of pending work grows unboundedly, consuming more and more memory until your process runs out and crashes. Your `backpressure.py` addresses this by using a semaphore to limit the total number of in-flight messages. When the queue is full, the main polling thread pauses instead of submitting more work, which naturally slows down Kafka consumption and lets the worker threads catch up.

```
Backpressure State Machine:

[Queue < 80% full]   -> normal operation, poll and submit freely
[Queue >= 80% full]  -> BACKPRESSURE ACTIVATED: pause new submissions
                        worker threads drain the queue
[Queue < 60% full]   -> BACKPRESSURE DEACTIVATED: resume normal operation

The 80% threshold (not 100%) gives breathing room —
you stop accepting new work before you run out of capacity,
not at the exact moment you hit the limit.
```

**When to use concurrent consumers:** External API calls, database queries or bulk inserts, file I/O operations, sending emails or SMS, calling ML model inference endpoints, and any processing that spends most of its time waiting rather than computing. The key signal is: if you profile your consumer and see that CPU usage is low while processing time is high, you have I/O-bound work and concurrent processing will help.

**When not to use concurrent consumers:** When message ordering is required within an entity (use keyed messages + sequential processing), when processing is CPU-bound (adding threads won't help if you're already saturating CPU cores; use more partitions and more consumer instances instead), and when your processing code is not thread-safe.

## Pattern 3: The Priority Consumer

The Priority Consumer is an architectural pattern built on top of the sequential and concurrent consumers — it is not a new way of reading Kafka, but rather a way of organizing multiple consumer instances to create the *effect* of priority processing.

The core insight is that Kafka itself does not support message priority within a topic. You cannot mark a message as "urgent" and have Kafka serve it before other messages in the same partition. But you can create multiple topics with different resource allocations, and run different consumers against each, dedicating more processing power and tighter SLAs to the high-priority topic.

```
Priority Consumer Architecture:

                    [High Priority Topic]   (6 partitions, 3 replicas)
                           |
                    +------+------+
                    |      |      |
                 [C1]    [C2]   [C3]    <-- 3 consumer instances
                 [10      10     10]        each with 10 threads
                 workers] workers] workers]
                 SLA: 100ms              = 30 concurrent workers total


                    [Low Priority Topic]    (2 partitions, 1 replica)
                           |
                         [C4]             <-- 1 consumer instance
                         [1 worker]           1 thread
                         SLA: 60 seconds  = 1 sequential worker


Result:
  High priority messages: processed by 30 workers targeting 100ms SLA
  Low priority messages: processed 1 at a time, eventually
```

Your `priority_consumer.py` implements this through a `PriorityConsumerManager` that manages multiple consumer instances with different `PriorityConfig` objects. Each config specifies the processing strategy (sequential or concurrent), the number of workers, and the SLA target. The manager starts all consumers simultaneously, and each runs independently in its own thread or process.

The SLA monitoring capability in the priority consumer is particularly important for production operations. By tracking how long each message takes to process and comparing against the SLA target, you can generate alerts when processing times drift — before users actually experience the latency degradation. This is the difference between reactive operations (a customer calls to report slowness) and proactive operations (your alerting system pages the on-call engineer when P95 processing time exceeds 80% of SLA target).

**The production decision between sequential and concurrent within a priority tier:** High-priority consumers handling time-sensitive events often benefit from concurrent processing even for CPU-bound work, because raw throughput matters less than ensuring no single slow message blocks others. Low-priority consumers for batch workloads can often use sequential processing safely, because a few seconds of added latency is acceptable and sequential processing is far simpler to reason about and debug.

## The Commit Strategy Decision: Your Most Important Consumer Design Choice

Across all three consumer patterns, the single most consequential design decision is the offset commit strategy. Your code must choose one of three approaches, and each has different implications for data safety and processing complexity.

**Auto-commit** is the simplest: the Kafka client library automatically commits offsets at a configured interval (typically every 5 seconds). This means you do not write any commit code. But it also means that if your consumer crashes between the automatic commits, you may reprocess the last few seconds of messages. Additionally, auto-commit commits the current offset regardless of whether your processing succeeded — meaning a message that failed processing but was auto-committed will not be retried from Kafka (you need a Dead Letter Queue for this, which we cover in Chapter 6).

**Synchronous commit after processing** is the safest approach: after processing each message (or batch), you call `commit()` and wait for the broker to acknowledge the commit before continuing. This guarantees that if you crash, you will never have committed an offset for a message you did not process. The cost is added latency — every batch of processing incurs an extra network round trip.

**Asynchronous commit** is a middle ground: you call `commitAsync()` which sends the commit request without waiting for acknowledgment, then continue processing immediately. If the commit fails, you must handle the failure in a callback. This is faster but requires more careful error handling.

```
Commit Strategy Comparison:

Auto-commit (every 5 seconds):
  Simplest code. Risk: crash within 5 seconds means duplicate processing.
  Good for: non-critical data where occasional duplicates are acceptable.
  
Sync commit after each message:
  Safest. Risk: 1 network round trip per message, adds ~5ms latency each.
  Good for: financial data, exactly-once-sensitive processing.
  
Sync commit after each batch:
  Balance. Risk: crash during batch means whole batch is reprocessed.
  Good for: most production systems. Process 100 messages, then commit once.
  
Async commit:
  Fast. Risk: if commit fails and you don't handle it, you may lose progress.
  Good for: high-throughput systems where you have robust failure handling.
```

The pattern used by most sophisticated production consumers is: process messages in batches, use synchronous commit at the end of each batch, and handle commit failures by triggering a controlled shutdown and restart (letting the consumer reload from the last committed offset). This provides a balance between safety and throughput that works for the vast majority of production use cases.

---

**What comes next:** Chapter 5 addresses one of the most important architectural decisions in any Kafka deployment: how to design a priority architecture across topics, and the specific configuration trade-offs that create meaningfully different service levels for different classes of events.
