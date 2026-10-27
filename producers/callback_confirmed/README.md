# Callback-Confirmed Producer

**In one sentence:** you send messages to Kafka without waiting, and Kafka tells you later, one by one, whether each message arrived safely.

---

## 1. The problem

Your shop website gets an order. You want to tell Kafka: "order 101 was created."

Two simple ways to do it:

| Way | What happens | Problem |
|---|---|---|
| **Send and forget** | You hand the message over and move on. | If it got lost, you never find out. |
| **Send and wait for each one** | You wait for "got it" after every message. | Very slow. |

The **callback-confirmed producer** is the middle way:
**send fast, and still find out what happened to each message.**

---

## 2. A story to remember

You run a small shop and ship parcels with a courier.

1. You put a parcel on the courier's desk and write your phone number on it.
   You do **not** wait. You go back to work.
2. The courier carries it to the warehouse.
3. The warehouse says "received" or "we could not take it."
4. The courier **phones the number on the parcel** and tells you the result.

Now match it to Kafka:

| Story | Kafka |
|---|---|
| Putting the parcel on the desk | `publish_event()` |
| The courier's desk (a waiting area) | the **local queue**, a small waiting list in your program's memory |
| The warehouse | the Kafka **broker** (the server that stores messages) |
| The phone number on the parcel | the **callback**, a function you hand over now so Kafka can call it later |
| The courier phoning you | Kafka running your callback |

> **Callback = "call me back with the result."** That is the whole idea.

---

## 3. What happens, step by step

```text
You:     publish_event(order)      "Courier, take this."
                    |
                    v
         [ local queue ]                 Message waits in your program's memory.
                    |                    publish_event() returns NOW.
                    |                    It has NOT reached Kafka yet.
                    v
Kafka client (in the background)         Sends the message over the network.
                    |
                    v
         Kafka broker                    Says "received", or "failed".
                    |
                    v
You:     poll() or finish()              Your callback runs HERE and
                                         counts: delivered, or failed.
```

Two things people get wrong:

1. **`publish_event()` finishing does NOT mean the message arrived.**
   It only means: "it is in the waiting list."
2. **The callback does not run by itself.** It runs when you call `poll()` or `finish()`.
   Think of `poll()` as "check my phone for messages from the courier."

---

## 4. The code

```python
from producers.callback_confirmed import CallbackConfirmedProducer

producer = CallbackConfirmedProducer("orders")  # 1. Create ONE producer

try:
    for order in orders:
        producer.publish_event(order)  # 2. Hand over the parcel
        producer.poll()  # 3. Check for results
finally:
    summary = producer.finish()  # 4. At the end: wait for the rest

print(summary)
```

With the default settings, each event must look like this (all three fields are required):

```python
{"event_id": "evt-1", "event_type": "order.created", "order_id": 101}
```

- `event_id` : a unique name for this message. Used to tell you *which* message failed.
- `event_type` : what happened.
- `order_id` : which order. Messages with the same `order_id` stay in order.

Not sending orders? You describe your own events in one line. See section 9.

The four steps:

| Step | Call | Think of it as |
|---|---|---|
| 1 | `CallbackConfirmedProducer(...)` | Hire one courier. Use the same courier for everything. |
| 2 | `publish_event(order)` | Put a parcel on the desk. Don't wait. |
| 3 | `poll()` | Look at your phone. Any news? |
| 4 | `finish()` | End of day: wait for the last news, then get a final report. |

---

## 5. Reading the final report

`finish()` gives you a report like this:

```text
queued_count=20  delivered_count=20  failed_count=0
awaiting_callback_count=0  unfinished_message_count=0
```

| Number | Plain meaning |
|---|---|
| `queued_count` | Parcels you put on the desk. |
| `delivered_count` | Kafka said "received." |
| `failed_count` | Kafka said "could not take it." |
| `awaiting_callback_count` | Still no answer when time ran out. **Unknown, not success.** |
| `unfinished_message_count` | Parcels the courier still holds. |

**The one line you need:**

```python
if summary.all_confirmed:
    print("Every message arrived safely.")
else:
    print("Something failed or is unknown. Look at the report.")
```

`all_confirmed` is `True` only when **nothing failed and nothing is unknown.**

**Running a service that never stops?** `finish()` only runs at shutdown. To watch progress while it runs, call `producer.snapshot_delivery_counts()` now and then and send the numbers to your monitoring:

```python
producer.snapshot_delivery_counts()
# {'queued': 120, 'delivered': 118, 'failed': 0, 'awaiting_callback': 2}
```

It is a moving picture, not a verdict. A small `awaiting_callback` is normal. If it keeps growing, or `failed` goes above 0, something is wrong.

---

## 6. When things go wrong

| What you see | What it means | What to do |
|---|---|---|
| `ValueError` from `publish_event()` | Your order is missing a field, has an empty `event_id` or `order_id`, or can't be turned into JSON. | Fix the order. It was **not** queued. |
| `TimeoutError` from `publish_event()` | The waiting list stayed full for 5 seconds. | Send slower, or try later. That message was **not** queued. You can keep using the producer. |
| `KafkaException` from `publish_event()` | Kafka refused the message outright, for example it is too big. | Make the message smaller. It was **not** queued. |
| `Kafka client error` in your logs | The program cannot reach Kafka. | Check the broker address and that Kafka is running. Messages sent meanwhile will show up as `failed_count`. |
| `failed_count` is more than 0 | Kafka could not take some messages. | Look at `summary.recent_delivery_failures`. It lists the `event_id` and the reason. |
| `awaiting_callback_count` is more than 0 | We stopped waiting before Kafka answered. | Treat those messages as **not confirmed**. |

---

## 7. Three mistakes to avoid

1. **Thinking "publish returned" means "delivered."**
   It only means "in the waiting list." Delivered is counted later, in the callback.

2. **Thinking "nothing is left to send" means "all succeeded."**
   A failed message also leaves the list. Always check `summary.all_confirmed`.

3. **Making a new producer for every message.**
   Create one at program start and reuse it.

---

## 8. What "delivered" does NOT mean

"Delivered" means **Kafka stored the message.**
It does **not** mean another program has read it or acted on it.
Reading and acting is the consumer's job, and it is a separate step.

---

## 9. Reuse in a new project (the blueprint)

This folder is built to be **copied into any project**. It has no ties to the rest of this repo.

**You need:** Python 3.10 or newer, and `pip install confluent-kafka`.

### Step 1: Copy the folder

Copy the whole `producers/callback_confirmed/` folder into your project. All imports inside it are relative, so it works wherever you put it. (`main.py` and `__main__.py` are only the demo. You can delete them.)

### Step 2: Describe YOUR events (this is the only code you write)

Say which field is the unique id, and which field decides the ordering lane (the key).
Pick for the key the thing whose events must stay in order: an order, a customer, a device.

```python
from producers.callback_confirmed import CallbackConfirmedProducer, EventSchema

PAYMENT_EVENT_SCHEMA = EventSchema(
    event_id_field="payment_event_id",  # unique id of each event
    key_field="payment_id",  # same payment -> same lane -> kept in order
    other_required_fields=("amount_cents",),  # extra fields every event must have
)

producer = CallbackConfirmedProducer(
    "payments",  # the topic (must already exist)
    event_schema=PAYMENT_EVENT_SCHEMA,
)
```

That is all. Validation, ordering, and delivery tracking now follow your schema.

### Step 3: Connect it to your Kafka

| What | How |
|---|---|
| Broker address | `bootstrap_servers="broker-a:9092,broker-b:9092"` or the `KAFKA_BOOTSTRAP_SERVERS` environment variable |
| Login / encryption | `extra_kafka_settings={...}` (see section 10) |
| Topic | Create it first. Use 3 copies (replication factor 3) in production. |

### Step 4: Use it in your app

```python
producer = CallbackConfirmedProducer("payments", event_schema=PAYMENT_EVENT_SCHEMA)  # at startup

producer.publish_event(event)  # wherever you have an event
producer.poll()  # in the same loop, right after publishing

summary = producer.finish()  # at shutdown, once
if not summary.all_confirmed:
    ...  # log it, alert, exit with an error code
```

### Go-live checklist

- [ ] Event schema matches what your consumers expect.
- [ ] Topic exists, with enough partitions and 3 replicas.
- [ ] Broker address and login come from environment variables, not from code.
- [ ] `finish()` is called at shutdown, and `all_confirmed` is checked and acted on.
- [ ] Failures are visible: your logging collects the `producers.callback_confirmed` logger.
- [ ] `snapshot_delivery_counts()` is sent to monitoring if the service runs for long.
- [ ] Events that must never be lost are saved in your database first (outbox pattern).
- [ ] You tried it once against a real Kafka (see "Try it" below).

### Which file does what

Read them in this order. Every line is explained in a comment.

| File | What it is | Do you edit it? |
|---|---|---|
| `callback_confirmed_producer.py` | The producer. Steps 0 to 4, one comment per sub-step. | No |
| `event_schema.py` | Describes your events (id, key, required fields) and turns them into bytes. | Add your own `EventSchema` (in your code, or here) |
| `producer_config.py` | Kafka settings and wait times, each with a reason. | Only to tune |
| `delivery_summary.py` | The final report returned by `finish()`. | No |
| `main.py`, `__main__.py` | A runnable demo. | Delete, or keep as a smoke test |

---

## 10. Using it for real

Before you rely on it in a real service, check these five things.

**1. Tell it where Kafka is.** Do not rely on the default `localhost:9092`.

```python
producer = CallbackConfirmedProducer("orders", bootstrap_servers="broker-a:9092,broker-b:9092")
# or set the KAFKA_BOOTSTRAP_SERVERS environment variable
```

**2. If your Kafka needs a login or encryption (TLS / SASL), pass the settings in.**
They are added on top of the safe defaults. Read secrets from the environment, never write them in code.

```python
import os

producer = CallbackConfirmedProducer(
    "orders",
    extra_kafka_settings={
        "security.protocol": "SASL_SSL",
        "sasl.mechanism": "PLAIN",
        "sasl.username": os.environ["KAFKA_USERNAME"],
        "sasl.password": os.environ["KAFKA_PASSWORD"],
    },
)
```

This package never prints these settings.

**3. Create the topic first.** Many real Kafka servers do not create topics automatically. A message sent to a missing topic is not rejected right away. It ends up in `failed_count` after about 30 seconds, and the reason only says "Message timed out", so a slow failure plus that reason is a hint to check the topic name.

**4. Use it from one thread.** The counters are plain numbers with no locks. For many threads sharing one producer, use `producers/singleton`.

**5. Always check `all_confirmed` at shutdown, and act on it.** Log it, raise an alert, or exit with an error code. The demo does the last one: it exits `0` only when every message was confirmed.

Safe defaults you get without doing anything: `acks=all` (wait for all copies), no duplicates from the client's own retries, and a 30 second limit per message.

> **Still not covered:** if your program crashes, messages still in the waiting list are lost. For orders and payments that must never be lost, save them in your database first and send from there (the "outbox" pattern).

---

## 11. If you remember only this

```text
publish_event()  ->  put it on the desk       (not delivered yet)
poll()                 ->  check for results        (callbacks run here)
finish()               ->  final wait + report      (check all_confirmed)
```

**Send fast. Get a phone call for each parcel. Trust `all_confirmed`.**

---

## Try it

Start Kafka (from the repo's `infrastructure/` folder):

```bash
cd infrastructure
docker compose up -d
```

Run the demo. This repo's Docker Kafka listens on port `19094`:

```bash
export KAFKA_BOOTSTRAP_SERVERS=localhost:19094
python -m producers.callback_confirmed
```

It sends 5 orders and prints the summary. Exit code `0` means every message was confirmed.

Tests that need no Kafka:

```bash
python -m unittest test.producer.test_callback_confirmed_producer
```

Tests against a real Kafka (the Docker one above must be running):

```bash
RUN_KAFKA_INTEGRATION_TESTS=1 KAFKA_BOOTSTRAP_SERVERS=localhost:19094 \
  python -m unittest test.producer.test_callback_confirmed_integration
```
