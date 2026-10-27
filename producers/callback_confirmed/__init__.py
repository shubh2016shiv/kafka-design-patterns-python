"""Callback-confirmed producer (Pattern 2). See README.md.

from producers.callback_confirmed import CallbackConfirmedProducer

producer = CallbackConfirmedProducer("orders")
try:
    producer.publish_event({"event_id": "e1", "event_type": "order.created", "order_id": 1})
    producer.poll()
finally:
    summary = producer.finish()
print(summary.all_confirmed)
"""

# Each line below makes a name importable straight from "producers.callback_confirmed",
# so callers never need to know which file a class lives in.

# The producer itself: the one class you use day to day.
from .callback_confirmed_producer import CallbackConfirmedProducer

# The final report type returned by finish().
from .delivery_summary import DeliverySummary

# EventSchema describes YOUR events; ORDER_EVENT_SCHEMA is a ready-made example.
from .event_schema import ORDER_EVENT_SCHEMA, EventSchema

# Builds the Kafka settings, for callers who want to inspect or extend them.
from .producer_config import build_producer_config

# __all__ lists the supported public names. Anything not listed is internal detail.
__all__ = [
    "CallbackConfirmedProducer",
    "DeliverySummary",
    "EventSchema",
    "ORDER_EVENT_SCHEMA",
    "build_producer_config",
]
