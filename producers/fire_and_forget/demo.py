"""
Educational demo for the fire-and-forget producer pattern.

Run this script directly to see the pattern in action:

    python -m producers.fire_and_forget.demo

What this demo teaches:
1. The singleton property — get_instance() always returns the same object.
2. Fire-and-forget send — produce() returns before the broker acks.
3. The delivery callback fires asynchronously via poll().
4. Optional confirmation mode — blocking until the broker acks.
5. Retry behavior — exponential backoff on local enqueue failure.
6. Health metrics — observability built into the shared producer.
7. Graceful shutdown — flush() drains all pending messages.

Prerequisites:
- A running Kafka broker (local Docker or Confluent Cloud)
- KAFKA_BOOTSTRAP_SERVERS env var set, or broker running on localhost:9092
"""

from __future__ import annotations

import logging
import os
import time
import threading
from typing import Any

# ---------------------------------------------------------------------------
# Logging setup — INFO level shows the key demo events clearly
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("demo.fire_and_forget")


def _read_bootstrap_servers() -> str:
    """
    Read bootstrap servers from the environment with a sensible local default.

    Why an env var:
    Avoids hardcoding broker addresses in source code. CI, local Docker, and
    Confluent Cloud deployments all set this variable differently.
    """
    servers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    logger.info("Bootstrap servers: %s", servers)
    return servers


def _build_demo_config(bootstrap_servers: str) -> dict:
    """
    Return a minimal producer config for the demo.

    acks=all: broker waits for all in-sync replicas to confirm the write.
    This is actually stricter than classic fire-and-forget (acks=0), but is
    used here to demonstrate the delivery callback reliably in a local setup.

    For true fire-and-forget with maximum throughput set acks=0 — the broker
    will not send any acknowledgement and the delivery callback will never fire.
    """
    return {
        "bootstrap.servers": bootstrap_servers,
        "client.id": "fire-and-forget-demo",
        "acks": "all",
        "retries": 3,
        "linger.ms": 5,
        "batch.size": 16384,
        "compression.type": "snappy",
    }


# ---------------------------------------------------------------------------
# Demo scenes
# ---------------------------------------------------------------------------


def scene_1_singleton_property(producer_class: Any) -> None:
    """
    Scene 1 — Singleton property: same object regardless of call site.

    Key insight:
    A Kafka producer maintains TCP connections to every broker in the cluster.
    Creating a new producer per request multiplies those connections and defeats
    librdkafka's internal batching. The shared instance avoids both problems.
    """
    logger.info("=" * 60)
    logger.info("Scene 1: Singleton property")
    logger.info("=" * 60)

    instance_a = producer_class.get_instance()
    instance_b = producer_class.get_instance()

    assert instance_a is instance_b, "BUG: get_instance() returned different objects!"
    logger.info("✓ instance_a is instance_b → %s", instance_a is instance_b)
    logger.info("  Both variables point to id=%d", id(instance_a))


def scene_2_fire_and_forget_send(producer_class: Any, topic: str) -> None:
    """
    Scene 2 — Fire-and-forget: produce() returns before the broker acks.

    The delivery callback is wired internally and fires later when poll() is called.
    The caller never blocks on broker I/O.
    """
    logger.info("=" * 60)
    logger.info("Scene 2: Fire-and-forget send")
    logger.info("=" * 60)

    producer = producer_class.get_instance()

    event = {
        "event_type": "page_view",
        "user_id": "u-1001",
        "page": "/home",
        "timestamp_ms": int(time.time() * 1000),
    }

    logger.info("Calling send() — this returns IMMEDIATELY, no waiting for broker.")
    t0 = time.monotonic()
    result = producer.send(topic=topic, data=event, key="u-1001")
    elapsed_ms = (time.monotonic() - t0) * 1000

    logger.info("send() returned in %.2f ms — broker ack is still in flight.", elapsed_ms)
    logger.info(
        "SendResult: success=%s  topic=%s  key=%s", result.success, result.topic, result.message_key
    )

    # Give the delivery callback a moment to fire so the demo output is readable.
    time.sleep(0.5)


def scene_3_confirmation_mode(producer_class: Any, topic: str) -> None:
    """
    Scene 3 — Optional confirmation mode: block until the broker acks.

    When to use:
    When you need to know a specific message was durably written before
    proceeding — e.g., the last record in a batch before signalling downstream.
    """
    logger.info("=" * 60)
    logger.info("Scene 3: Confirmation mode (require_delivery_confirmation=True)")
    logger.info("=" * 60)

    producer = producer_class.get_instance()

    event = {
        "event_type": "checkout_started",
        "order_id": "ord-5001",
        "user_id": "u-1001",
        "timestamp_ms": int(time.time() * 1000),
    }

    logger.info("Calling send() with require_delivery_confirmation=True.")
    t0 = time.monotonic()
    result = producer.send(
        topic=topic,
        data=event,
        key="ord-5001",
        require_delivery_confirmation=True,
        delivery_timeout_seconds=10.0,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000

    logger.info("send() returned after %.2f ms — broker has confirmed receipt.", elapsed_ms)
    logger.info("SendResult: success=%s", result.success)


def scene_4_retry(producer_class: Any, topic: str) -> None:
    """
    Scene 4 — send_with_retry: exponential backoff on local enqueue failure.

    Note: this retries LOCAL produce() errors (e.g., buffer full, producer unhealthy).
    Broker-level delivery retries are handled by the 'retries' config key inside
    librdkafka — a separate mechanism entirely.
    """
    logger.info("=" * 60)
    logger.info("Scene 4: send_with_retry with exponential backoff")
    logger.info("=" * 60)

    producer = producer_class.get_instance()

    event = {
        "event_type": "add_to_cart",
        "product_id": "prod-9900",
        "user_id": "u-1001",
        "timestamp_ms": int(time.time() * 1000),
    }

    result = producer.send_with_retry(
        topic=topic,
        data=event,
        key="u-1001",
        max_retries=2,
        retry_delay_seconds=0.5,
    )

    logger.info(
        "RetryResult: success=%s  attempts=%d  key=%s",
        result.success,
        result.attempts_made,
        result.message_key,
    )


def scene_5_thread_safety(producer_class: Any, topic: str) -> None:
    """
    Scene 5 — Thread safety: multiple threads share one producer instance.

    confluent_kafka.Producer is thread-safe at the C-extension level.
    All threads call produce() on the same underlying librdkafka handle,
    which manages internal synchronization. No application-level locking needed.
    """
    logger.info("=" * 60)
    logger.info("Scene 5: Thread safety — 5 threads, 1 shared producer")
    logger.info("=" * 60)

    results = []
    lock = threading.Lock()

    def worker(thread_id: int) -> None:
        producer = producer_class.get_instance()
        result = producer.send(
            topic=topic,
            data={"event_type": "worker_event", "thread_id": thread_id},
            key=f"thread-{thread_id}",
        )
        with lock:
            results.append((thread_id, result.success))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    logger.info(
        "All threads used the same producer instance: %s", id(producer_class.get_instance())
    )
    for thread_id, success in sorted(results):
        logger.info("  Thread %d → success=%s", thread_id, success)


def scene_6_health_metrics(producer_class: Any) -> None:
    """
    Scene 6 — Health metrics: observability built into the shared producer.

    In production this output feeds a health check endpoint so Kubernetes
    or your monitoring stack can detect producer degradation early.
    """
    logger.info("=" * 60)
    logger.info("Scene 6: Health metrics")
    logger.info("=" * 60)

    producer = producer_class.get_instance()

    # Small flush to trigger any pending delivery callbacks before reading metrics.
    producer.flush(timeout_seconds=2.0)

    metrics = producer.get_health_metrics()
    logger.info("is_healthy      : %s", metrics.is_healthy)
    logger.info("messages_sent   : %d", metrics.messages_sent)
    logger.info("error_count     : %d", metrics.error_count)
    logger.info("error_rate      : %.2f%%", metrics.error_rate * 100)
    logger.info("config (sanitized):")
    for key, value in metrics.sanitized_config.items():
        logger.info("  %-30s = %s", key, value)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_demo() -> None:
    """
    Orchestrate all demo scenes against a live Kafka broker.

    Each scene is self-contained and teaches one aspect of the pattern.
    Scenes run sequentially so log output is easy to follow.
    """
    from .core import FireAndForgetProducer

    bootstrap_servers = _read_bootstrap_servers()
    config = _build_demo_config(bootstrap_servers)
    topic = "fire-and-forget-demo"

    # Inject config for the demo run; only honoured on the very first call.
    FireAndForgetProducer.get_instance(config=config)

    try:
        scene_1_singleton_property(FireAndForgetProducer)
        scene_2_fire_and_forget_send(FireAndForgetProducer, topic)
        scene_3_confirmation_mode(FireAndForgetProducer, topic)
        scene_4_retry(FireAndForgetProducer, topic)
        scene_5_thread_safety(FireAndForgetProducer, topic)
        scene_6_health_metrics(FireAndForgetProducer)
    finally:
        logger.info("=" * 60)
        logger.info("Demo complete — flushing remaining messages.")
        FireAndForgetProducer.get_instance().flush()
        FireAndForgetProducer.reset_instance()


if __name__ == "__main__":
    run_demo()
