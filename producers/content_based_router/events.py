"""
Content-Based Router — building, validating, and serializing events.

What this file is for (three separate jobs, one function each):
    1. make_event()       wrap raw business data in a consistent envelope.
    2. validate_event()   reject malformed or unknown events BEFORE we route.
    3. serialize_event()  turn a validated event into the bytes Kafka sends.

Why validate before routing: an invalid event is a business-contract mistake,
not a Kafka problem. Catching it here means we never publish a broken event to
a topic just because a field was missing.

The event envelope every event shares:
    event_id     unique id for tracing and de-duplication downstream
    event_type   which business thing happened (e.g. "order.created")
    occurred_at  when it happened, as a UTC timestamp string
    data         the event-specific business fields
"""

from __future__ import annotations

# json: turns a Python dict into text/bytes and back. Kafka moves bytes, so we
# must convert our dict into JSON bytes before sending.
import json

# uuid: generates a random, globally-unique id string for each new event.
import uuid

# datetime/timezone: records exactly when the event happened, in UTC, so events
# from different machines and timezones are still comparable.
from datetime import datetime, timezone

# Any: a value whose type we do not constrain here (the caller's business data).
from typing import Any

from .constants import SUPPORTED_EVENT_TYPES, VALID_PAYMENT_STATUSES
from .types import EventValidationError, UnknownEventTypeError


# ============================================================================
# STEP 1: make_event — wrap business data in the shared envelope
# ============================================================================


def make_event(event_type: str, data: dict, *, event_id: str | None = None) -> dict:
    """Wrap domain data in the consistent, JSON-friendly event envelope.

    Args:
        event_type: which business event this is, e.g. "order.created".
        data:       the event-specific business fields, as a dict.
        event_id:   optional caller-supplied id. Leave as None to auto-generate
                    one; pass your own only when you need a known id (e.g. tests
                    or replaying a specific event).

    Returns:
        A new dict with event_id, event_type, occurred_at, and data.
    """
    return {
        # Use the caller's id if given, otherwise create a fresh random one.
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": event_type,
        # isoformat() gives a readable, sortable UTC timestamp string.
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }


# ============================================================================
# STEP 2: small shared check used by validation
# ============================================================================


def require_nonempty_string(value: Any, field_name: str) -> str:
    """Return the value if it is a non-blank string, else raise a clear error.

    A single helper keeps every "this field must be present text" check worded
    the same way, so error messages across the app are consistent.
    """
    # isinstance(...) rejects numbers/None; .strip() rejects "" and "   ".
    if not isinstance(value, str) or not value.strip():
        raise EventValidationError(f"{field_name} must be a non-empty string")
    return value


# ============================================================================
# STEP 3: validate_event — fail fast on bad input
# ============================================================================


def validate_event(event: dict) -> None:
    """Check an event against our supported contracts; raise if it is invalid.

    Raises:
        EventValidationError:   the event is malformed (missing/invalid fields).
        UnknownEventTypeError:  the event_type has no routing rule defined.

    Returns None on success. We return nothing because the job is simply "let
    good events through, stop bad ones here".
    """
    # ---- 3.1: the envelope itself must be a dict with the required labels ----
    if not isinstance(event, dict):
        raise EventValidationError("event must be a dictionary")

    for field_name in ("event_id", "event_type", "occurred_at"):
        require_nonempty_string(event.get(field_name), field_name)

    data = event.get("data")
    if not isinstance(data, dict):
        raise EventValidationError("data must be a dictionary")

    event_type = event["event_type"]

    # ---- 3.2: per-type business rules ----------------------------------------
    if event_type == "order.created":
        require_nonempty_string(data.get("order_id"), "data.order_id")
        require_nonempty_string(data.get("customer_id"), "data.customer_id")

    elif event_type == "payment.updated":
        require_nonempty_string(data.get("payment_id"), "data.payment_id")
        require_nonempty_string(data.get("order_id"), "data.order_id")
        if data.get("status") not in VALID_PAYMENT_STATUSES:
            allowed = ", ".join(sorted(VALID_PAYMENT_STATUSES))
            raise EventValidationError(f"data.status must be one of: {allowed}")

    elif event_type == "inventory.changed":
        require_nonempty_string(data.get("sku"), "data.sku")
        for field_name in ("available_units", "reorder_point"):
            number = data.get(field_name)
            # `type(number) is not int` deliberately rejects booleans too:
            # in Python True == 1, so `isinstance(True, int)` would pass and let
            # a True slip through as a count. We want real integers only.
            if type(number) is not int or number < 0:
                raise EventValidationError(
                    f"data.{field_name} must be a non-negative integer"
                )

    else:
        # Reaching here means the type is not in SUPPORTED_EVENT_TYPES. We refuse
        # it on purpose rather than guessing a destination for it.
        raise UnknownEventTypeError(
            f"No route defined for event_type={event_type!r}. "
            f"Supported types: {', '.join(SUPPORTED_EVENT_TYPES)}"
        )


# ============================================================================
# STEP 4: serialize_event — convert a validated event into Kafka bytes
# ============================================================================


def serialize_event(event: dict) -> bytes:
    """Convert a validated event dict into compact UTF-8 JSON bytes.

    Kafka transmits bytes, not Python objects, so this is the boundary where our
    dict becomes a wire payload.

    Raises:
        EventValidationError: the event contains something JSON cannot encode
        (for example a set, or a NaN float). We treat that as a contract error
        because retrying the send would never fix it.
    """
    try:
        text = json.dumps(
            event,
            # ensure_ascii=False keeps non-English characters readable instead
            # of escaping them, which makes logged payloads easier to read.
            ensure_ascii=False,
            # allow_nan=False rejects NaN/Infinity, which are not valid JSON and
            # would break strict consumers.
            allow_nan=False,
            # separators without spaces produce the smallest possible payload.
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise EventValidationError(f"event cannot be JSON-encoded: {error}") from error

    return text.encode("utf-8")
