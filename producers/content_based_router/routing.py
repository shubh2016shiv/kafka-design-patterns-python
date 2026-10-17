"""
Content-Based Router — the pure routing decision.

What this file is for:
    One function, route_event(), that looks at a validated event and decides
    which topic it belongs in and which partition key to use. It does NOT talk
    to Kafka, so you can test it with plain assertions and no broker.

The routing rules (one destination per event):

    | Event content                                   | Topic suffix       |
    |-------------------------------------------------|--------------------|
    | order.created                                   | .orders            |
    | payment.updated with status == FAILED           | .payments.failed   |
    | payment.updated with any other valid status     | .payments          |
    | inventory.changed, units <= reorder point       | .inventory.alerts  |
    | inventory.changed, units above reorder point    | .inventory         |

Topic names are built from a fixed prefix plus these fixed suffixes. The caller
never supplies a raw topic name, which prevents typos and stray writes.
"""

from __future__ import annotations

from .constants import DEFAULT_TOPIC_PREFIX
from .events import validate_event
from .types import RouteDecision


# ============================================================================
# STEP 1: route_event — validate, then choose topic and key
# ============================================================================


def route_event(event: dict, *, prefix: str = DEFAULT_TOPIC_PREFIX) -> RouteDecision:
    """Validate an event and return where it should be published.

    Args:
        event:  the event dict to route.
        prefix: the text placed in front of every topic name. Defaults to the
                package default; pass your own to isolate another app's topics.

    Returns:
        A RouteDecision(topic, key, reason). Pure data — no Kafka call happens.

    Raises:
        EventValidationError / UnknownEventTypeError: if the event is invalid.
        We validate first so routing logic below can trust every field exists.
    """
    # ---- 1.1: refuse to route anything that is not valid ----
    validate_event(event)

    event_type = event["event_type"]
    data = event["data"]

    # ---- 1.2: order events -> the orders topic, keyed by order id ----
    # Keying by order_id keeps all events for one order on the same partition,
    # which preserves their order within that topic.
    if event_type == "order.created":
        return RouteDecision(
            topic=f"{prefix}.orders",
            key=data["order_id"],
            reason="order_created",
        )

    # ---- 1.3: payment events -> split failed vs. non-failed ----
    if event_type == "payment.updated":
        if data["status"] == "FAILED":
            # Failed payments get their own topic so a dedicated consumer can
            # handle recovery without scanning every successful payment.
            return RouteDecision(
                topic=f"{prefix}.payments.failed",
                key=data["order_id"],
                reason="payment_failed",
            )
        return RouteDecision(
            topic=f"{prefix}.payments",
            key=data["order_id"],
            reason="payment_non_failed",
        )

    # ---- 1.4: inventory events -> alert when stock is low ----
    # validate_event() guarantees the only remaining type here is
    # "inventory.changed", so we can route on the stock level directly.
    if data["available_units"] <= data["reorder_point"]:
        return RouteDecision(
            topic=f"{prefix}.inventory.alerts",
            key=data["sku"],
            reason="at_or_below_reorder_point",
        )

    return RouteDecision(
        topic=f"{prefix}.inventory",
        key=data["sku"],
        reason="stock_healthy",
    )
