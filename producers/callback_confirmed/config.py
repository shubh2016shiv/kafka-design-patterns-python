"""Configuration for the reusable callback-confirmed producer.

Plain-language terms used here:

    broker       one Kafka server
    acknowledgement (ack)
                 the broker's answer that it accepted a message
    idempotence  protection against duplicates created by eligible client retries
    latency      how long one operation takes
    throughput   how many messages are processed in a period of time
    batch        several messages grouped into one network request

None of these settings makes the pattern callback-confirmed. The
``on_delivery`` function passed in :mod:`publisher` does that. Configuration
controls how the client sends and how strong a broker acknowledgement must be.
"""

# Lets us write type hints like `str | None` below without Python evaluating
# them immediately when this file loads.
from __future__ import annotations

# os exposes environment variables, which let deployments change broker
# addresses without changing application source code.
import os

# ProducerConfig describes the heterogeneous dictionary accepted by the Kafka
# client. "Heterogeneous" means its values have several different types.
from .types import ProducerConfig

# Name of the environment variable used by local, CI, and production deployments.
BOOTSTRAP_SERVERS_ENV_VAR: str = "KAFKA_BOOTSTRAP_SERVERS"

# Local learning default used when the environment variable is absent.
LOCAL_BOOTSTRAP_SERVERS: str = "localhost:9092"


def read_bootstrap_servers() -> str:
    """Return the configured comma-separated Kafka broker addresses.

    Returns:
        Text such as ``"broker-a:9092,broker-b:9092"``.

    Failure behavior:
        Raises ``ValueError`` when the environment variable exists but contains
        only whitespace. A blank deployment setting should not silently connect
        to a developer's local broker.
    """
    # Use localhost only when the variable is absent, not when it is explicitly blank.
    bootstrap_servers = os.environ.get(
        BOOTSTRAP_SERVERS_ENV_VAR,
        LOCAL_BOOTSTRAP_SERVERS,
    )
    # strip() removes surrounding spaces and makes a whitespace-only value empty.
    if not bootstrap_servers.strip():
        # Raise near the source so the problem is not mistaken for a network outage.
        raise ValueError(
            f"{BOOTSTRAP_SERVERS_ENV_VAR} must contain at least one host:port address."
        )
    # Keep the value unchanged because the Kafka client parses the address list.
    return bootstrap_servers


def build_callback_producer_config(
    bootstrap_servers: str | None = None,
    client_id: str = "callback-confirmed-producer",
) -> ProducerConfig:
    """Build safe learning defaults for callback-confirmed publishing.

    Args:
        bootstrap_servers: Broker addresses. ``None`` reads the environment.
        client_id: Application name shown in Kafka logs and metrics.

    Returns:
        A new dictionary accepted by ``confluent_kafka.Producer``.

    Failure behavior:
        Raises ``ValueError`` for blank broker addresses or client identity.
        The Kafka client validates Kafka-specific values during construction.
    """
    # Prefer an explicit argument so tests and applications can be deterministic.
    resolved_servers = (
        bootstrap_servers if bootstrap_servers is not None else read_bootstrap_servers()
    )
    # Reject a blank explicit value rather than unexpectedly changing to localhost.
    if not resolved_servers.strip():
        raise ValueError("bootstrap_servers must contain at least one host:port address.")
    # A useful client id makes the producer identifiable during an incident.
    if not client_id.strip():
        raise ValueError("client_id must be a non-empty string.")

    # Return a fresh mapping so callers may safely add authentication settings.
    return {
        # These are starting addresses for discovering the rest of the cluster.
        # Production should normally list two or three different brokers.
        "bootstrap.servers": resolved_servers,
        # This label appears in broker logs, metrics, and quota reports.
        "client.id": client_id,
        # "all" asks the partition leader to wait for every caught-up replica.
        # Topic replication settings still determine how many copies actually exist.
        "acks": "all",
        # Idempotence prevents duplicates caused by supported automatic retries.
        # It does not make manual resends or consumer side effects exactly once.
        "enable.idempotence": True,
        # Bound the complete client delivery process, including automatic retries.
        # Thirty seconds is a learning default; production must align it with its SLO.
        "delivery.timeout.ms": 30_000,
        # Wait up to five milliseconds to form more efficient message batches.
        # Raise for throughput or lower for latency only after measurement.
        "linger.ms": 5,
    }
