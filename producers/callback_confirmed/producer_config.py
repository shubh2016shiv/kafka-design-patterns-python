"""Settings for the callback-confirmed producer: Kafka client config and wait times.

Everything you might tune for a new project lives in this file, each with a reason.
"""

# logging lets us write messages that your app's logging setup can collect and filter.
import logging

# os lets us read environment variables (the safe place for broker addresses and secrets).
import os

# KafkaError holds the numeric error codes the Kafka client uses (e.g. "connection failed").
from confluent_kafka import KafkaError

# One shared logger name for the whole package, so you can turn its logs up or down in one place.
logger = logging.getLogger("producers.callback_confirmed")

# How long publish_event() may wait for room when the local waiting list is full.
# 5 seconds is a balance: long enough to ride out a short slowdown, short enough that a
# stuck Kafka cannot freeze your app for long.
# Tune: batch jobs can wait longer (30+); web requests should wait less (1 or less).
LOCAL_QUEUE_FULL_MAX_WAIT_SECONDS = 5.0

# How long finish() waits at shutdown for the last results.
# It MUST be longer than "delivery.timeout.ms" below (30 s), so every message has had
# time to either arrive or be declared failed before we stop waiting.
SHUTDOWN_FLUSH_TIMEOUT_SECONDS = 35.0

# While the waiting list is full, we wait in small slices of this length, then try again.
# Short slices mean we notice freed space quickly. Rarely needs tuning.
LOCAL_QUEUE_FULL_POLL_SLICE_SECONDS = 0.25


def log_kafka_client_error(kafka_error) -> None:
    """Send connection-level Kafka problems (for example "all brokers down") to our logs.

    The Kafka client calls this by itself while we poll(). Without it these problems
    only appear as raw text on the screen, where monitoring tools cannot see them.

    Repeated connection-refused errors go to DEBUG: the client retries many times
    per second and would flood the logs. The useful signals stay visible: "all
    brokers down" (once), fatal errors, and each failed message (logged by the producer).
    """
    # A fatal error means the client cannot continue at all. Log it as critical.
    if kafka_error.fatal():
        logger.critical("Kafka client fatal error: %s", kafka_error)
    # A transport error is "could not connect right now". The client keeps retrying
    # by itself, and it repeats often, so we keep it quiet (DEBUG).
    elif kafka_error.code() == KafkaError._TRANSPORT:
        logger.debug("Kafka transport error (client will retry): %s", kafka_error)
    # Anything else is worth a warning, e.g. "all brokers are down".
    else:
        logger.warning("Kafka client error: %s", kafka_error)


def build_producer_config(
    bootstrap_servers: str | None = None,
    extra_kafka_settings: dict | None = None,
) -> dict:
    """Return the settings dict handed to the Kafka client.

    Args:
        bootstrap_servers: Broker addresses, e.g. "broker-a:9092,broker-b:9092".
            None reads the KAFKA_BOOTSTRAP_SERVERS environment variable, then falls
            back to localhost:9092 (fine on a laptop, never for production).
        extra_kafka_settings: Added on top of the defaults below, and WINS if a key
            appears in both. This is where TLS/SASL (login/encryption) settings go,
            for example {"security.protocol": "SASL_SSL", "sasl.mechanism": "PLAIN"}.
            We never log this dict, because it may contain passwords.
    """
    # Use the address passed in; otherwise the environment variable; otherwise localhost.
    broker_addresses = bootstrap_servers or os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    # An empty or spaces-only address can never work. Fail now with a clear message.
    if not broker_addresses.strip():
        raise ValueError("bootstrap_servers must contain at least one host:port address")

    producer_config = {
        # Where to find Kafka. The client uses this to discover the rest of the cluster.
        # In production list 2-3 brokers, so one being down does not stop startup.
        "bootstrap.servers": broker_addresses,
        # A name shown in Kafka's own logs and metrics, so you can find this app there.
        # Change it per application.
        "client.id": "callback-confirmed-producer",
        # "all" = the main broker waits until ALL its in-sync copies have the message
        # before saying "got it". Safest choice. (With only one broker it adds no extra
        # copies; real safety also needs a replicated topic.)
        "acks": "all",
        # Stops the client's own automatic retries from storing a message twice.
        "enable.idempotence": True,
        # The most time one message may spend being delivered, retries included
        # (30 s). After that it is declared failed. finish() waits longer than this.
        "delivery.timeout.ms": 30_000,
        # Wait up to 5 ms to group several messages into one network trip. Slightly
        # more latency, much better throughput. Use 0 if every millisecond matters.
        "linger.ms": 5,
        # Send connection-level problems into our logging (see function above).
        "error_cb": log_kafka_client_error,
    }
    # Apply the caller's extra settings last, so they can add (or deliberately override)
    # anything above. "or {}" makes None mean "no extras".
    producer_config.update(extra_kafka_settings or {})
    return producer_config
