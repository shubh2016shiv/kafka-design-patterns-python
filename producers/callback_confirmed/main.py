"""Demo: python -m producers.callback_confirmed

Sends 5 order events to a lab topic, then prints the delivery summary.
Exit code 0 means every event was acknowledged; 1 means something was not.

Read this file as a worked example of how an application uses the producer:
create ONE producer, publish + poll in a loop, finish in a "finally" block.
"""

# logging: so the producer's own log lines (failures etc.) appear on screen.
import logging

# os: to read the broker address from an environment variable.
import os

# uuid: to make a unique id for every event (event ids must be unique).
import uuid

# AdminClient/NewTopic let the demo create its topic if it is missing. A real app
# usually has its topics created ahead of time by whoever runs Kafka.
from confluent_kafka.admin import AdminClient, NewTopic

# The producer we are demonstrating (the dot means "from this same package").
from .callback_confirmed_producer import CallbackConfirmedProducer

# The lab topic used by the demo. Safe: the demo creates it but never deletes anything.
DEMO_TOPIC_NAME = "callback_confirmed_orders_lab"

# How many events the demo sends.
DEMO_EVENT_COUNT = 5


def create_topic_if_missing(bootstrap_servers: str, topic_name: str) -> None:
    """Create the topic if it is missing. Never deletes anything."""
    # AdminClient is Kafka's "management" connection (list and create topics).
    admin_client = AdminClient({"bootstrap.servers": bootstrap_servers})
    # list_topics also proves Kafka is reachable: it fails after 10 s if it is not.
    if topic_name in admin_client.list_topics(timeout=10).topics:
        return
    # 3 partitions = 3 ordered lanes. replication_factor=1 because the lab has ONE
    # broker; production topics should have 3 copies.
    new_topic = NewTopic(topic_name, num_partitions=3, replication_factor=1)
    # create_topics returns a "future" per topic; .result(15) waits up to 15 s for it.
    admin_client.create_topics([new_topic])[topic_name].result(15)


def build_demo_order_event(order_id: int) -> dict:
    """Build one order.created event. Money is in cents to avoid float rounding."""
    return {
        # A new random id for each event, so each one can be traced on its own.
        "event_id": str(uuid.uuid4()),
        # What happened. Consumers use this to decide how to handle the event.
        "event_type": "order.created",
        # Which order. This is the ordering key: same order, same lane, kept in order.
        "order_id": order_id,
        # Whole cents (4999 = 49.99). Floats like 49.99 can drift when added up.
        "total_cents": 4999,
    }


def main() -> int:
    """Publish the demo events and return a process exit code."""
    # Show the producer's INFO/ERROR log lines with the name of the part that wrote them.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Where Kafka is. Falls back to the usual local address when the variable is unset.
    bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    create_topic_if_missing(bootstrap_servers, DEMO_TOPIC_NAME)

    # Create ONE producer and reuse it for everything. Making one per message would
    # throw away its connections and batching.
    producer = CallbackConfirmedProducer(DEMO_TOPIC_NAME, bootstrap_servers=bootstrap_servers)
    try:
        for order_id in range(1, DEMO_EVENT_COUNT + 1):
            # Put the event on the waiting list. This returns at once.
            producer.publish_event(build_demo_order_event(order_id))
            # Check for results that have already come back (runs their callbacks).
            producer.poll()
    finally:
        # "finally" runs even if publishing raised an error, so we always wait for
        # in-flight messages and always get the honest final report.
        delivery_summary = producer.finish()

    # Show the whole report: queued, delivered, failed, awaiting, and any failures.
    print(delivery_summary)

    # Exit code 0 only when EVERY event was confirmed. Anything else exits 1, so a
    # script or CI job can notice the problem.
    return 0 if delivery_summary.all_confirmed else 1


# Run main() only when this file is started directly, not when it is imported.
if __name__ == "__main__":
    raise SystemExit(main())
