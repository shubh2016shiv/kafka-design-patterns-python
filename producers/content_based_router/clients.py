"""
Content-Based Router — building the one shared Kafka producer.

What this file is for:
    One function, build_shared_producer(), that creates a single, correctly
    configured Kafka producer. The application calls this ONCE at startup and
    hands the result to KafkaEventRouter. Reusing one producer (instead of
    making a new one per topic or per request) is the whole point of the
    "shared producer" lesson this pattern builds on.

Plain-language terms:
    bootstrap servers   the first broker addresses the client connects to in
                        order to discover the rest of the Kafka cluster
    acks="all"          wait until every in-sync copy of the data has the
                        message before calling it acknowledged (safest setting)
    idempotence         lets the client safely retry without creating duplicates
"""

from __future__ import annotations

# os: reads environment variables so broker addresses can change between
# laptop, CI, and production without editing this source file.
import os

from .types import KafkaProducerLike

# Name of the environment variable that holds the broker addresses.
BOOTSTRAP_SERVERS_ENV_VAR: str = "KAFKA_BOOTSTRAP_SERVERS"

# Address used for a local learning broker when the variable is not set.
LOCAL_BOOTSTRAP_SERVERS: str = "localhost:9092"


# ============================================================================
# STEP 1: build_shared_producer — create one producer with safe defaults
# ============================================================================


def build_shared_producer(
    bootstrap_servers: str | None = None,
    client_id: str = "content-based-router",
) -> KafkaProducerLike:
    """Create one confluent_kafka Producer configured for reliable publishing.

    Args:
        bootstrap_servers: broker addresses like "host:9092". None reads the
                           KAFKA_BOOTSTRAP_SERVERS environment variable, falling
                           back to localhost for a local lab.
        client_id:         a label for this producer shown in Kafka logs and
                           metrics, which makes it identifiable during incidents.

    Returns:
        A ready-to-use producer that satisfies KafkaProducerLike.

    Raises:
        ImportError: confluent_kafka is not installed. We import it inside the
        function (not at the top of the file) so that merely importing this
        package for the pure routing logic does not require the Kafka client.
    """
    # ---- 1.1: resolve where to connect ----
    resolved_servers = bootstrap_servers or os.environ.get(
        BOOTSTRAP_SERVERS_ENV_VAR, LOCAL_BOOTSTRAP_SERVERS
    )

    # ---- 1.2: import the real client only when we actually build a producer --
    try:
        from confluent_kafka import Producer
    except ImportError as error:
        raise ImportError(
            "confluent_kafka is required to build a live producer. "
            "Install it with: pip install confluent-kafka"
        ) from error

    # ---- 1.3: build with reliability-first settings ----
    return Producer(
        {
            # Starting broker addresses used to discover the rest of the cluster.
            "bootstrap.servers": resolved_servers,
            # Identifies this producer in broker logs and metrics.
            "client.id": client_id,
            # Wait for every in-sync replica before counting a write as done.
            "acks": "all",
            # Safe automatic retries that do not create duplicate records.
            "enable.idempotence": True,
            # Give the whole delivery process (including retries) 30 seconds.
            "delivery.timeout.ms": 30000,
        }
    )
