"""
Content-Based Router — runnable teaching demo.

What this file is for:
    Show the router end to end with five realistic events. By default it runs
    OFFLINE using a tiny in-memory producer, so you can see routing work with no
    Kafka broker installed. An opt-in LIVE path publishes to a real broker.

The five sample events exercise every routing branch:
    1. order.created                       -> .orders
    2. payment.updated SUCCEEDED           -> .payments
    3. payment.updated FAILED              -> .payments.failed
    4. inventory.changed, low stock        -> .inventory.alerts
    5. inventory.changed, healthy stock    -> .inventory
"""

from __future__ import annotations

# json: deserialize a message body in the demo producer so it can read event_id.
import json

# logging: print the routing decisions as readable, labeled lines.
import logging

# deque: a simple first-in/first-out holder for messages waiting for a callback.
from collections import deque

from .clients import build_shared_producer
from .core import KafkaEventRouter
from .events import make_event
from .types import PublishReceipt

logger = logging.getLogger("producers.content_based_router.demo")


# ============================================================================
# STEP 1: sample events — one per routing branch
# ============================================================================


def build_sample_events() -> list[dict]:
    """Return five events, one for each possible routing destination."""
    return [
        make_event("order.created", {"order_id": "ORD-101", "customer_id": "CUST-7"}),
        make_event(
            "payment.updated",
            {"payment_id": "PAY-200", "order_id": "ORD-101", "status": "SUCCEEDED"},
        ),
        make_event(
            "payment.updated",
            {"payment_id": "PAY-201", "order_id": "ORD-102", "status": "FAILED"},
        ),
        make_event(
            "inventory.changed",
            {"sku": "LAPTOP-15", "available_units": 2, "reorder_point": 5},
        ),
        make_event(
            "inventory.changed",
            {"sku": "MOUSE-9", "available_units": 50, "reorder_point": 5},
        ),
    ]


# ============================================================================
# STEP 2: a tiny in-memory producer so the demo runs without Kafka
# ============================================================================


class _InMemoryProducer:
    """A stand-in producer for the offline demo. It never contacts Kafka.

    It records each message and, when poll()/flush() runs, reports every pending
    message as delivered successfully. This is only enough to demonstrate the
    router's flow — it does not imitate real brokers, partitions, or failures.
    """

    def __init__(self) -> None:
        self._pending: deque = deque()

    def produce(self, *, topic, key, value, on_delivery=None, headers=None) -> None:
        """Accept a message into the local queue and remember its callback."""
        self._pending.append((topic, value, on_delivery))

    def _deliver_all(self) -> None:
        """Report every pending message as delivered, running its callback."""
        while self._pending:
            topic, value, on_delivery = self._pending.popleft()
            if on_delivery is not None:
                # A real client passes a message object; the demo passes None for
                # the error (success) and a simple label for the message.
                on_delivery(None, f"delivered:{topic}")
            # Reading the id back proves the payload is valid JSON bytes.
            json.loads(value)

    def poll(self, timeout: float) -> int:
        """Run any ready callbacks; returns how many were delivered."""
        before = len(self._pending)
        self._deliver_all()
        return before

    def flush(self, timeout: float) -> int:
        """Deliver everything still pending; returns remaining (always 0 here)."""
        self._deliver_all()
        return 0


# ============================================================================
# STEP 3: offline demo — route five events with no broker
# ============================================================================


def run_offline_demo() -> dict:
    """Publish the five sample events through an in-memory producer.

    Returns the router's final snapshot of counters so a caller (or __main__)
    can print and check them.
    """
    producer = _InMemoryProducer()
    router = KafkaEventRouter(producer)

    receipts: list[PublishReceipt] = []
    for event in build_sample_events():
        receipt = router.publish(event)
        receipts.append(receipt)
        logger.info(
            "routed event_id=%s -> topic=%s (%s)",
            receipt.event_id,
            receipt.topic,
            receipt.route_reason,
        )

    # Drain anything still waiting, then report honest counts.
    producer.flush(5)
    return router.snapshot()


# ============================================================================
# STEP 4: live demo — publish to a real broker (opt-in)
# ============================================================================


def run_live_demo() -> dict:
    """Publish the five sample events to a real Kafka broker.

    Requires confluent_kafka and a reachable broker (see KAFKA_BOOTSTRAP_SERVERS).
    The topics must already exist; creating topics is an operator/admin task, not
    something an application should do on every startup.
    """
    producer = build_shared_producer(client_id="content-based-router-demo")
    router = KafkaEventRouter(producer)

    try:
        for event in build_sample_events():
            receipt = router.publish(event)
            logger.info(
                "local acceptance event_id=%s -> topic=%s",
                receipt.event_id,
                receipt.topic,
            )
    finally:
        # The caller owns the producer, so the demo flushes the one it created.
        remaining = producer.flush(30)
        logger.info("flush remaining=%d", remaining)

    return router.snapshot()
