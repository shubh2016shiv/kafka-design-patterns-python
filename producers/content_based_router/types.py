"""
Content-Based Router — shared data shapes and error types.

What this file is for:
    1. The small result objects the router hands back (RouteDecision,
       PublishReceipt), so callers and tests get a stable, typed shape.
    2. The error types we raise so callers can tell a *business* mistake (bad
       event) apart from an *infrastructure* problem (queue stayed full).
    3. The producer "shape contract" (KafkaProducerLike) so real Kafka clients
       and a fake test producer can both be used without changing router code.

ASCII flow — where each shape appears:

    event dict
        |
        v
    route_event(event) -----------------> RouteDecision(topic, key, reason)
        |   (bad event raises)                    |
        v                                         v
    EventValidationError / UnknownEventTypeError  |
                                                  v
    KafkaEventRouter.publish(event) ------> PublishReceipt(event_id, topic, ...)
        |   (local queue stays full)
        v
    LocalQueueTimeoutError
"""

# Lets us write type hints like `int | None` without Python checking them the
# instant this file loads. Purely a convenience for cleaner hints below.
from __future__ import annotations

# dataclass: a decorator that turns a small class into a plain "value object".
# It writes the __init__, a readable __repr__, and value-based equality for us,
# so we do not hand-write that boilerplate.
from dataclasses import dataclass

# Any: "a value of any type" (used where the Kafka client owns the real type).
# Protocol: lets us describe a type by the methods it has, not by what it
# inherits from. See KafkaProducerLike at the bottom for what that buys us.
from typing import Any, Protocol


# ============================================================================
# STEP 1: Error types — tell business mistakes apart from infrastructure faults
# ============================================================================


class EventValidationError(ValueError):
    """Raised when an event's structure or business fields are invalid.

    This is a *local* mistake found before we ever talk to Kafka — for example
    a missing order id, or a payment with no status. It is NOT a delivery
    failure. The caller should fix or reject the event, not retry sending it.
    """


class UnknownEventTypeError(EventValidationError):
    """Raised when there is no routing rule for this event's type.

    We subclass EventValidationError so a caller that catches validation errors
    also catches this one, but a caller that cares specifically about "unknown
    type" (e.g. to return a 422 vs a 400) can still catch it on its own.
    """


class LocalQueueTimeoutError(TimeoutError):
    """Raised when the producer's local queue stayed full past our deadline.

    This means the producer could not even accept the message locally within
    the time we allowed. It is different from a delivery failure, which happens
    *after* the message was accepted and is reported through a callback instead.
    """


# ============================================================================
# STEP 2: RouteDecision — the pure "where should this go?" answer
# ============================================================================


# frozen=True makes instances read-only after creation, so a decision cannot be
# edited by accident once the router has made it.
@dataclass(frozen=True)
class RouteDecision:
    """The destination chosen for one event, with no Kafka call involved yet.

    Fields:
        topic   the Kafka topic this event should be written to.
        key     the partition key: events sharing a key land on the same
                partition *within that topic*, which keeps them in order there.
        reason  a short label explaining which rule chose this topic, useful
                for logs and tests (e.g. "payment_failed", "stock_healthy").
    """

    topic: str
    key: str
    reason: str


# ============================================================================
# STEP 3: PublishReceipt — proof of LOCAL acceptance, not delivery
# ============================================================================


@dataclass(frozen=True)
class PublishReceipt:
    """Returned by publish() once the producer has accepted the event locally.

    Important: holding this receipt means "the producer took the message into
    its outbound queue", NOT "Kafka stored it". The real delivery result arrives
    later through the delivery callback and is counted in the router's snapshot.

    Fields:
        event_id      the event's own id, handy for correlating logs.
        topic         the topic the event was routed to.
        key           the partition key that was used.
        route_reason  same reason string as the RouteDecision that chose it.
    """

    event_id: str
    topic: str
    key: str
    route_reason: str


# ============================================================================
# STEP 4: KafkaProducerLike — the minimum a producer must offer
# ============================================================================


class KafkaProducerLike(Protocol):
    """The small set of methods the router needs from any producer.

    Why a Protocol instead of a shared base class: the real `confluent_kafka.
    Producer` and the `FakeProducer` we use in tests do not inherit from a
    common parent. A Protocol says "any object with these three methods counts
    as a producer here", so both work without being forced into an inheritance
    tree. This is what lets the tests run with no Kafka broker installed.
    """

    def produce(
        self,
        *,
        topic: str,
        key: bytes,
        value: bytes,
        on_delivery: Any = None,
        headers: Any = None,
    ) -> None:
        """Hand one message to the producer's local outbound queue."""

    def poll(self, timeout: float) -> int:
        """Let the producer do pending work, such as running delivery callbacks."""

    def flush(self, timeout: float) -> int:
        """Wait for queued messages to drain; return how many are still waiting."""
