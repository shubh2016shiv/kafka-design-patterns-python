"""
producers.content_based_router
===============================
A content-based router for Kafka: it reads a business event, decides which
topic that event belongs in, and publishes it through ONE shared producer.

The router chooses the destination; the producer sends the bytes. Choosing a
topic is not the same as confirming delivery — publish() means the message was
accepted locally, and the real delivery result is counted later via callbacks.

What each piece does
--------------------
Building and checking events:
    make_event            wrap business data in the shared event envelope.
    validate_event        reject malformed or unknown events before routing.
    serialize_event       turn a validated event into the bytes Kafka carries.

Deciding the destination (pure, no Kafka):
    route_event           return a RouteDecision(topic, key, reason).
    RouteDecision         the chosen topic + partition key + reason label.

Publishing through a shared producer:
    KafkaEventRouter      validate -> route -> publish -> count delivery results.
    PublishReceipt        proof of LOCAL acceptance (not delivery) from publish().
    build_shared_producer create the one reliable producer to inject.

Error types:
    EventValidationError  the event is malformed (a local, pre-Kafka mistake).
    UnknownEventTypeError there is no routing rule for this event type.
    LocalQueueTimeoutError the producer's local queue stayed full too long.

Quickstart
----------
    from producers.content_based_router import (
        KafkaEventRouter,
        build_shared_producer,
        make_event,
    )

    producer = build_shared_producer()          # once, at application startup
    router = KafkaEventRouter(producer)

    receipt = router.publish(make_event(
        "order.created",
        {"order_id": "ORD-1", "customer_id": "CUST-1"},
    ))
    # receipt means LOCAL acceptance; check router.snapshot() for delivery.

    producer.flush(30)                           # once, at application shutdown
"""

from .clients import build_shared_producer
from .core import KafkaEventRouter
from .events import make_event, require_nonempty_string, serialize_event, validate_event
from .routing import route_event
from .types import (
    EventValidationError,
    KafkaProducerLike,
    LocalQueueTimeoutError,
    PublishReceipt,
    RouteDecision,
    UnknownEventTypeError,
)

__all__ = [
    # Events
    "make_event",
    "validate_event",
    "require_nonempty_string",
    "serialize_event",
    # Routing
    "route_event",
    "RouteDecision",
    # Router + producer
    "KafkaEventRouter",
    "PublishReceipt",
    "build_shared_producer",
    "KafkaProducerLike",
    # Errors
    "EventValidationError",
    "UnknownEventTypeError",
    "LocalQueueTimeoutError",
]
