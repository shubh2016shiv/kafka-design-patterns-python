# Kafka Producer and Consumer Mental Model

This guide gives a beginner one simple picture of what a Kafka producer and
consumer do. Learn this flow before studying retries, rebalancing, batching, or
exactly-once processing.

## End-to-end flow

```text
PRODUCER APPLICATION                  KAFKA                 CONSUMER APPLICATION

event                                                     subscribe
  |                                                           |
  v                                                           v
encode to bytes                                           poll for record
  |                                                           |
  v                                                           v
produce  --------------------------> stored record ------> decode bytes
  |                                                           |
  v                                                           v
poll for delivery result                                  process event
  |                                                           |
  v                                                           v
flush when shutting down                                 commit next position
                                                              |
                                                              v
                                                         poll again
                                                              |
                                                              v
                                                         close on shutdown
```

The shortest correct summary is:

```text
Producer:
event → encode → produce → delivery confirmation → flush on shutdown

Consumer:
subscribe → poll → decode → process → commit → repeat → close on shutdown
```

---

## Stage 1.0 — Producer mental model

A producer sends records to Kafka.

### Stage 1.1 — Create the event

The application begins with useful data, often represented by a Python
dictionary:

```python
event = {
    "event_id": "evt-1001",
    "event_type": "order.created",
    "order_id": "order-1001",
}
```

At this point, the event exists only inside the producer application.

### Stage 1.2 — Encode the event

Kafka transports bytes. The producer converts the Python value into JSON text
and then into bytes:

```python
value = json.dumps(event).encode("utf-8")
```

```text
Python dictionary → JSON text → bytes
```

This conversion is commonly called **serialization**. It simply means
converting an in-memory value into a form that can be stored or transmitted.

### Stage 1.3 — Produce the record

The producer queues the encoded record for delivery:

```python
producer.produce(
    topic="orders",
    key=b"order-1001",
    value=value,
    on_delivery=delivery_callback,
)
```

`produce()` normally places the record into the producer client's local queue.
It does not necessarily mean Kafka has already stored the record.

### Stage 1.4 — Poll for delivery results

Producer polling allows delivery callbacks to run:

```python
producer.poll(0)
```

The delivery callback eventually reports one of two outcomes:

- Kafka acknowledged the record.
- Delivery failed and includes an error.

Producer `poll()` does **not** read business records from Kafka.

### Stage 1.5 — Flush at a boundary or during shutdown

Before the producer exits, it waits for queued delivery work to finish:

```python
remaining = producer.flush(10.0)
```

`flush()` waits up to the supplied timeout. Its returned value is the number of
records still unfinished.

Do not normally call `flush()` after every record. Doing that removes much of
the producer's batching and throughput benefit. Use it at controlled boundaries,
especially shutdown.

### Producer lifecycle

```text
START
  |
  v
create producer
  |
  v
event → encode → produce → poll
  ^                         |
  |_________________________|
       repeat for events
  |
  v
flush
  |
  v
STOP
```

---

## Stage 2.0 — Consumer mental model

A consumer asks Kafka for records and gives them to application code.

### Stage 2.1 — Subscribe

The consumer declares which topic it wants to read:

```python
consumer.subscribe(["orders"])
```

The consumer also has a `group.id`. Kafka uses that group to share partition
work and remember reading progress.

### Stage 2.2 — Poll for a record

The consumer asks for the next available result:

```python
message = consumer.poll(1.0)
```

One consumer poll can return:

```text
None             → nothing arrived during this wait
message + error  → Kafka/client error that must be handled
normal message   → bytes and record metadata to process
```

Consumer `poll()` returns records or consumer events. This is different from
producer `poll()`, which mainly serves delivery callbacks.

### Stage 2.3 — Decode the record

The consumer reverses the producer's encoding:

```python
event = json.loads(message.value().decode("utf-8"))
```

```text
bytes → JSON text → Python dictionary
```

This conversion is commonly called **deserialization**.

Real applications must also validate the result. Kafka can transport bytes,
but it does not know whether the JSON contains the business fields the
application requires.

### Stage 2.4 — Process the event

Application code performs the useful business action:

```python
process_order(event)
```

Examples include:

- updating an analytics table;
- sending a notification;
- updating inventory;
- writing an audit record.

Processing is application behavior. Kafka does not perform this business work.

### Stage 2.5 — Commit after successful processing

After processing succeeds, the consumer saves the group's next reading
position:

```python
consumer.commit(message=message, asynchronous=False)
```

If the processed message has offset `8`, committing that message normally saves
position `9`:

```text
offset 8                    offset 9
processed record            read from here next
    |                            ^
    +------ successful commit ---+
```

A commit is a **bookmark**, not the business result itself. It does not prove
that a database update exists. It tells Kafka where this consumer group should
continue.

### Stage 2.6 — Repeat, then close during shutdown

The consumer repeats polling while the application is running. When shutting
down, it leaves the group and releases resources:

```python
consumer.close()
```

`close()` is cleanup. It is not a replacement for processing or committing.

### Consumer lifecycle

```text
START
  |
  v
create consumer
  |
  v
subscribe
  |
  v
poll → decode → process → commit
  ^                         |
  |_________________________|
       repeat for records
  |
  v
close
  |
  v
STOP
```

---

## Stage 3.0 — Producer and consumer `poll()` are different

The identical method name is a common source of confusion.

| Call | Plain-English purpose |
|---|---|
| `producer.poll(0)` | “Run producer delivery callbacks and report completed sends.” |
| `consumer.poll(1.0)` | “Wait up to one second for a consumed record or consumer event.” |

Remember:

```text
Producer poll  → delivery result
Consumer poll  → consumed record/event
```

---

## Stage 4.0 — Correct operation order

### Producer

```text
event
  ↓
encode
  ↓
produce
  ↓
service delivery callbacks with poll
  ↓
flush when shutting down
```

The commonly forgotten producer operation is `produce()`. Encoding only creates
bytes; it does not send them.

### Consumer

```text
subscribe
  ↓
poll
  ↓
decode and validate
  ↓
process
  ↓
commit
  ↓
repeat poll
  ↓
close when shutting down
```

The commonly forgotten consumer operation is decoding. Kafka returns bytes,
not a ready-made business object.

---

## Stage 5.0 — Why process must come before commit

Unsafe ordering:

```text
poll → commit → process
```

If the consumer commits and then crashes before processing, the saved bookmark
may move past unfinished work.

Safer basic ordering:

```text
poll → decode → process → commit
```

If processing fails, the commit does not happen. Kafka can provide that record
again after recovery.

There is still one important failure window:

```text
business work succeeds
        ↓
process crashes before commit
        ↓
Kafka may provide the same record again
```

This is an **at-least-once** foundation: unfinished work is not intentionally
skipped, but completed work can sometimes repeat. Production business handlers
therefore need idempotency—repeating the same event must not create an unwanted
duplicate result.

---

## Final cheat sheet

```text
PRODUCER

event → encode → produce → poll for delivery results
                                  |
                                  v
                         flush on shutdown


CONSUMER

subscribe → poll → decode → process → commit
               ^                         |
               |_________________________|
                         repeat
               |
               v
          close on shutdown
```

The sentence to remember is:

> The producer converts an event to bytes and sends it. The consumer receives
> those bytes, converts them back, performs the business work, and only then
> saves its next reading position.
