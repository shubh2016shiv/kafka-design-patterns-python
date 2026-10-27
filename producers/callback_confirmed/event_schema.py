"""The event contract: which fields an event must have, and how it becomes Kafka bytes.

THIS IS THE FILE YOU CHANGE WHEN YOU REUSE THE PRODUCER FOR A NEW KIND OF EVENT.
Nothing else in the package knows what an "order" or a "payment" is.

Example for a new project (payments, keyed by payment id):

    PAYMENT_EVENT_SCHEMA = EventSchema(
        event_id_field="event_id",
        key_field="payment_id",
        other_required_fields=("event_type", "amount_cents"),
    )
    CallbackConfirmedProducer("payments", event_schema=PAYMENT_EVENT_SCHEMA)
"""

# json turns a Python dict into text. Kafka stores bytes, so we need this step.
import json

# dataclass writes the boring parts of a small "bag of settings" class for us.
from dataclasses import dataclass


# frozen=True means: once created, the schema cannot be changed by accident later.
@dataclass(frozen=True)
class EventSchema:
    """Describes the shape of the events you publish.

    Fields:
        event_id_field: Name of the field holding a unique id for each event.
            It is how a later "delivered / failed" result is matched to an event.
        key_field: Name of the field used as the Kafka message key. Events with
            the same key value always go to the same partition (one ordered lane
            of the topic), so they stay in order. Pick the thing whose events
            must stay in order: an order id, a customer id, a device id.
        other_required_fields: Any further fields every event must contain.
    """

    # Which field is the unique id of the event.
    event_id_field: str
    # Which field decides the ordering lane (the Kafka message key).
    key_field: str
    # Extra fields that must be present. Empty by default: only id and key are required.
    other_required_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Run right after creation: reject a schema that could never work."""
        # Both names must be real, non-empty text; otherwise every event would fail.
        for field_name in (self.event_id_field, self.key_field):
            if not isinstance(field_name, str) or not field_name.strip():
                raise ValueError("event_id_field and key_field must be non-empty strings")

    @property
    def required_fields(self) -> tuple[str, ...]:
        """Every field an event must contain: the id, the key, then the extras."""
        # dict.fromkeys removes duplicates but keeps the order. This way the id and
        # key are ALWAYS required, even if the caller forgot to list them.
        return tuple(
            dict.fromkeys((self.event_id_field, self.key_field, *self.other_required_fields))
        )

    def validate(self, event: dict) -> None:
        """Raise ValueError if the event is unusable. Called BEFORE anything is sent."""
        # An event must be a dict, because we look fields up by name.
        if not isinstance(event, dict):
            raise ValueError("event must be a dict")

        # Collect ALL missing fields so the caller can fix them in one go.
        missing_fields = [name for name in self.required_fields if name not in event]
        if missing_fields:
            raise ValueError(f"event is missing required fields: {missing_fields}")

        # The id and key must also be meaningful. None or "" would silently turn into
        # the text "None" or "", which ruins tracing (id) or ordering (key).
        for field_name in (self.event_id_field, self.key_field):
            field_value = event[field_name]
            if field_value is None or not str(field_value).strip():
                raise ValueError(f"event field '{field_name}' must not be blank")

    def build_message_key(self, event: dict) -> bytes:
        """Return the Kafka message key (the key field's value) as bytes."""
        # Kafka only stores bytes, so text must be encoded. UTF-8 handles every language.
        return str(event[self.key_field]).encode("utf-8")

    def read_event_id(self, event: dict) -> str:
        """Return the event's unique id as text, for logs and for matching results."""
        return str(event[self.event_id_field])


def encode_event_as_json(event: dict) -> bytes:
    """Turn the event dict into JSON bytes, the format stored in Kafka.

    Raises ValueError if the event holds something JSON cannot represent.
    """
    try:
        # separators=(",", ":") removes spaces, so the message is smaller.
        # allow_nan=False refuses NaN/Infinity: they are not valid JSON, and other
        # languages' JSON readers on the consumer side would crash on them.
        return json.dumps(event, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as encoding_error:
        # TypeError: the event holds an object JSON cannot store (e.g. a custom class).
        # ValueError: NaN or Infinity, or a self-referencing structure.
        # "from encoding_error" keeps the original reason visible in the traceback.
        raise ValueError(f"event is not JSON-serializable: {encoding_error}") from encoding_error


# A ready-made schema for order events. Used by default, and by the demo.
# Events need: event_id, order_id (the key), and event_type.
ORDER_EVENT_SCHEMA = EventSchema(
    event_id_field="event_id",
    key_field="order_id",
    other_required_fields=("event_type",),
)
