"""
Type contracts for the fire-and-forget producer.

WHY A SEPARATE MODULE
---------------------
These are the *shapes* that flow between modules: what a producer must be able
to do, and what a send reports back. Keeping them apart from the logic means
`core.py` reads as behaviour, and tests can depend on the contract without
importing the implementation.

WHAT IS IN HERE
---------------
  1. KafkaProducerLike  - the small slice of a Kafka producer we actually use
  2. DeliveryCallback   - the shape of a delivery-report callback function
  3. ProducerFactory    - the shape of "something that builds a producer"
  4. SendResult         - what send() reports back to the caller

PLAIN-LANGUAGE TERMS
--------------------
Contract: the inputs, outputs, and behavior that code promises to provide.
Type hint: a note for readers and checking tools about allowed value shapes.
Protocol: a type contract based on available methods rather than inheritance.
Callback: a function stored now so another component can call it later.
Serialization: conversion of an in-memory value into transportable bytes.
"""

# Postpone annotation evaluation. Recursive JSON types below can then refer to
# themselves without requiring the type to exist earlier in the file.
from __future__ import annotations

# ``dataclass`` generates the result object's constructor and readable repr,
# keeping the class focused on its meaning instead of boilerplate methods.
from dataclasses import dataclass

# ``Enum`` defines a closed set of named failure reasons. A closed set is safer
# than unrelated text strings that callers might misspell.
from enum import Enum

# Any: unavoidable values owned by the third-party Kafka library.
# Callable: the signature of a function that can be called.
# Protocol: an interface satisfied by matching methods, without inheritance.
# TypeAlias: tells type checkers that a name represents a reusable type.
from typing import Any, Callable, Protocol, TypeAlias


# ============================================================
# STEP 1: WHAT WE REQUIRE OF A KAFKA PRODUCER
# ============================================================


# Inheriting from Protocol makes this an interface for type checking; no real
# producer object is created from this class.
class KafkaProducerLike(Protocol):
    """The only three producer methods this package actually calls.

    WHY A PROTOCOL INSTEAD OF THE REAL `confluent_kafka.Producer` TYPE
    -----------------------------------------------------------------
    A Protocol says "anything with these methods will do". That gives us two
    things for free:

    1. Unit tests can pass a small fake object with these three methods and
       run the full send path with no broker and no network. Tests stay fast
       and deterministic.
    2. Swapping the underlying Kafka library later requires no change here,
       as long as the replacement offers the same three methods.

    This is structural typing: nothing needs to inherit from this class. A
    `confluent_kafka.Producer` already satisfies it without knowing it exists.
    """

    def produce(self, topic: str, **kwargs: Any) -> None:
        """Queue one message locally. Must not block on the network."""
        # Ellipsis means a Protocol declares a method but does not implement it.
        ...

    def poll(self, timeout: float) -> int:
        """Process delivery results that have already arrived.

        Returns the number of events handled.
        """
        # The concrete Kafka client or a test fake supplies this implementation.
        ...

    def flush(self, timeout: float) -> int:
        """Wait for queued messages to reach a final outcome.

        Returns how many messages were still outstanding when it gave up
        (0 means everything reached an outcome).
        """
        # The concrete Kafka client or a test fake supplies this implementation.
        ...


# ============================================================
# STEP 2: FUNCTION SHAPES
# ============================================================

# A delivery callback receives (error, message):
#   error   - None on success, otherwise a library error object
#   message - the delivered message, exposing topic()/partition()/offset()
#
# Typed as Any on both sides deliberately: these objects come from the Kafka
# C library and have no stable Python type we can import. Strict typing
# resumes at our own boundary, SendResult (STEP 3).
# This alias reads as: a callback accepts an optional error plus a Kafka
# message, performs side effects, and returns no value.
DeliveryCallback: TypeAlias = Callable[[Any | None, Any], None]

# JSON aliases make the public payload contract explicit. Values such as
# datetime or Decimal must be converted by the application before send().
# A scalar is one non-container value that JSON supports directly.
JsonScalar: TypeAlias = str | int | float | bool | None
# A JSON value may also contain nested lists or objects. This is "recursive"
# because JsonValue appears inside its own definition.
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
# send() intentionally accepts an object at the top level, not a bare scalar.
JsonObject: TypeAlias = dict[str, JsonValue]

# ``Any`` is deliberately limited to this third-party boundary because Kafka
# configuration values are heterogeneous and owned by confluent-kafka.
# Kafka owns hundreds of settings with different value types, so this external
# boundary is the one intentional use of Any in the package.
ProducerConfig: TypeAlias = dict[str, Any]
# A factory seam lets tests use a fake client without a broker or dependency.
# "Factory" simply means a function whose job is to create an object.
ProducerFactory = Callable[[ProducerConfig], KafkaProducerLike]


# ============================================================
# STEP 3: WHAT A SEND REPORTS BACK
# ============================================================


# Combining str and Enum produces named choices that also serialize as readable
# strings, which is convenient for logs, JSON, and metrics.
class SendFailureReason(str, Enum):
    """Machine-readable reason why a message was not queued locally.

    Broker delivery failures are intentionally absent: without a delivery
    callback this pattern cannot observe them. Callers can use these values
    for retry or load-shedding decisions without parsing a log message.
    """

    # The application already closed this instance, so it cannot accept work.
    PRODUCER_CLOSED = "producer_closed"
    # The in-memory holding area stayed full until our waiting budget expired.
    QUEUE_FULL = "queue_full"
    # The client rejected the request locally, for example due to message size.
    CLIENT_REJECTED = "client_rejected"


# @dataclass generates __init__, equality, and repr methods. ``frozen`` prevents
# later mutation; ``slots`` prevents accidental undeclared attributes and uses
# less memory when many results are created.
@dataclass(frozen=True, slots=True)
class SendResult:
    """The outcome of one `send()` call.

    `queued=True` means the message was **accepted into the producer's local
    in-memory queue**. It does *not* mean the broker has it, and it does not
    mean the message will ever arrive.

    That is not a limitation of this class, it is the definition of
    fire-and-forget: we never registered a delivery callback, so nothing in
    this process will ever learn the delivery outcome. A result object cannot
    report information the pattern deliberately declines to collect.

    If you need to know whether a message truly arrived, fire-and-forget is
    the wrong pattern. Use `producers.callback_confirmed` instead.

    The dataclass is frozen because a result describes an event that already
    happened; changing it later would make logs and control flow disagree.

    Fields:
        topic:         Topic the message was addressed to.
        message_key:   The key used, or None when no key was supplied.
        queued:        True when the local queue accepted the message.
        error_message: Why queueing failed, or None on success. Populated when
                       the local queue is full or the message is malformed or
                       oversized - never for broker-side failures, which this
                       pattern cannot observe.
        failure_reason: Stable local failure category, or None on success.
    """

    # Record the destination so logs can identify which stream was affected.
    topic: str
    # Preserve the ordering key when present; None means no key was supplied.
    message_key: str | None
    # True means only that the local in-memory queue accepted the message.
    queued: bool
    # Human-readable detail for logs and support; code should use the enum below.
    error_message: str | None = None
    # Stable category that application code can safely compare against.
    failure_reason: SendFailureReason | None = None

    # @property lets callers read ``result.retryable`` like data while this
    # method computes the answer from failure_reason.
    @property
    def retryable(self) -> bool:
        """Return whether retrying unchanged input may succeed later."""
        # A full queue may clear as Kafka catches up. Closed and client-rejected
        # states require a lifecycle or input/configuration change instead.
        return self.failure_reason is SendFailureReason.QUEUE_FULL
