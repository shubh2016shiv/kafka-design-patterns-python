"""
Producer configuration for the fire-and-forget pattern.

WHY A SEPARATE MODULE
---------------------
Configuration used to be built inside `demo.py`, which hid an important fact:
*config is a production decision, not a demo detail*. Every setting below
changes durability, throughput or latency in a real deployment, so it deserves
its own reviewable module.

THE MOST IMPORTANT THING ON THIS PAGE
-------------------------------------
None of these settings is what makes a producer "fire-and-forget".

Fire-and-forget is decided by *how you call* the producer - specifically, by
omitting the `on_delivery=` callback argument - not by any configuration key.
In particular `acks` does **not** control it:

    acks       = how careful the BROKER is before it answers
    on_delivery= whether YOUR CODE ever listens to that answer

Those are independent choices. You can fire-and-forget with `acks="all"`
(a very careful broker, whose answer you ignore) exactly as easily as with
`acks="0"` (a broker that never answers at all). See `core.py` for where the
pattern is actually established.

PLAIN-LANGUAGE TERMS
--------------------
Broker: one Kafka server process.
Cluster: a cooperating group of Kafka brokers.
Topic: a named stream of messages.
Partition: one ordered slice of a topic, used to spread work across brokers.
Replica: an extra copy of a partition stored for failure recovery.
Latency: how long one operation takes.
Throughput: how many messages can be handled in a period of time.
"""

# Postpone type-hint evaluation so annotations do not affect runtime behavior.
from __future__ import annotations

# os: read the broker address from the environment instead of hardcoding it.
import os

# Import the configuration type alias. It documents the dictionary boundary
# without importing or constructing the real Kafka client.
from .types import ProducerConfig

# ============================================================
# STEP 1: WHERE IS KAFKA?
# ============================================================

# Environment variable holding the broker address list.
BOOTSTRAP_SERVERS_ENV_VAR: str = "KAFKA_BOOTSTRAP_SERVERS"

# Fallback used when the variable is unset: the local learning broker from
# the `infrastructure/docker-compose.yml` setup.
LOCAL_BOOTSTRAP_SERVERS: str = "localhost:9092"


def read_bootstrap_servers() -> str:
    """Return the Kafka broker address list, from the environment if set.

    WHY AN ENVIRONMENT VARIABLE
    ---------------------------
    The same code must run against a local Docker broker, a CI broker, and a
    production cluster. Hardcoding an address forces a code change per
    environment, which means a code review and a deployment to change a
    hostname. An environment variable moves that decision to deploy time.

    Returns:
        A comma-separated `host:port` list, e.g. "broker1:9092,broker2:9092".

    Failure behaviour:
        Raises `ValueError` when the variable exists but is blank. When it is
        unset, returns the local learning default.
    """
    # Read the deployment-specific value. ``os.environ`` is the process's map
    # of environment-variable names to text values.
    configured_servers = os.environ.get(
        BOOTSTRAP_SERVERS_ENV_VAR,
        LOCAL_BOOTSTRAP_SERVERS,
    )
    # Reject empty or whitespace-only text now, where the error is easy to fix.
    if not configured_servers.strip():
        # ValueError means the caller supplied an invalid value, not that Kafka
        # suffered a temporary network failure.
        raise ValueError(
            f"{BOOTSTRAP_SERVERS_ENV_VAR} must contain at least one host:port address."
        )
    # Return the comma-separated broker addresses unchanged; confluent-kafka
    # parses and validates individual host:port entries later.
    return configured_servers


# ============================================================
# STEP 2: BUILD THE PRODUCER CONFIGURATION
# ============================================================


def build_producer_config(
    bootstrap_servers: str | None = None,
    client_id: str = "fire-and-forget-producer",
) -> ProducerConfig:
    """Build the configuration dictionary for a fire-and-forget producer.

    Every key is explained inline below, including why this value and when to
    change it for production.

    Args:
        bootstrap_servers: Broker address list. When None, read from the
            environment (see `read_bootstrap_servers`).
        client_id: A name for this application, attached to every request the
            producer sends.

    Returns:
        A config dict suitable for `confluent_kafka.Producer(...)`.

    Failure behaviour:
        Raises `ValueError` for blank broker or client names. Other invalid
        Kafka values are rejected when the concrete producer is constructed.
    """
    # Prefer an explicit function argument. Only consult the environment when
    # the caller passes None; an empty string remains an error rather than
    # silently changing to localhost.
    resolved_servers = (
        bootstrap_servers if bootstrap_servers is not None else read_bootstrap_servers()
    )
    # Fail near the source of a blank broker list so the later client error is
    # not mistaken for a Kafka outage.
    if not resolved_servers.strip():
        raise ValueError("bootstrap_servers must contain at least one host:port address.")
    # A client id appears in Kafka logs and metrics, so a blank value would make
    # production diagnosis needlessly difficult.
    if not client_id.strip():
        raise ValueError("client_id must be a non-empty string.")

    # Return a new dictionary on every call. The caller may safely add security
    # settings without changing future configurations.
    return {
        # ---------------------------------------------------------------
        # WHERE TO CONNECT
        # ---------------------------------------------------------------
        # A broker is one Kafka server. The producer contacts one of these
        # addresses, asks "which brokers are in this cluster?", then talks to
        # the broker responsible for each partition (ordered slice of a topic)
        # directly. So this is a STARTING point for discovery, not the full
        # cluster list.
        #
        # PRODUCTION: list 2-3 brokers, not 1. If the single address you
        # listed is the one that is down, the producer cannot start at all,
        # even though the rest of the cluster is healthy.
        "bootstrap.servers": resolved_servers,
        # ---------------------------------------------------------------
        # WHO IS CALLING
        # ---------------------------------------------------------------
        # Attached to every request and visible in broker logs, broker
        # metrics, and quota accounting. It changes no behaviour at all.
        #
        # PRODUCTION: always set something meaningful like
        # "checkout-service-producer". During an incident, "which application
        # is flooding this topic?" becomes answerable instead of guesswork.
        "client.id": client_id,
        # ---------------------------------------------------------------
        # HOW CAREFUL SHOULD THE BROKER BE?
        # ---------------------------------------------------------------
        # A leader is the broker currently responsible for a partition. A
        # replica is another broker's copy. "In-sync" means that copy is caught
        # up. "all" makes the leader wait for all caught-up copies before it
        # answers. This is the safest setting, and it is NOT in
        # tension with fire-and-forget (see this module's docstring).
        #
        # We choose "all" even though we ignore the answer because it gives
        # the message the best chance of surviving a broker failure. It does
        # increase broker work and can reduce maximum throughput; measure
        # before weakening durability. Choosing "0" also disables the
        # producer's ability to detect and retry broker rejections.
        #
        # PRODUCTION: pair with topic settings `replication.factor=3` and
        # `min.insync.replicas=2`. Without those, "all" can mean "one
        # replica", which is a far weaker guarantee than it sounds.
        "acks": "all",
        # ---------------------------------------------------------------
        # PREVENT DUPLICATES CAUSED BY THE CLIENT'S OWN RETRIES
        # ---------------------------------------------------------------
        # Idempotence means repeating the same request has the same effect as
        # performing it once. It lets the broker remove duplicate retry attempts
        # and preserves ordering under retry. It does not provide end-to-end exactly-once
        # processing, and it does not make failures visible to this class.
        # librdkafka also enforces compatible acknowledgements, retry counts,
        # and "in-flight" limits (requests sent but not answered yet).
        "enable.idempotence": True,
        # ---------------------------------------------------------------
        # HOW LONG TO WAIT BEFORE SENDING A BATCH
        # ---------------------------------------------------------------
        # A batch is a group sent in one network request. The producer groups
        # messages into batches, and this is how long it will
        # wait for more messages to accumulate before sending what it has.
        #
        # WHY 5ms: measurably large batches for very little added delay. Going
        # from 0 to 5 typically collapses hundreds of tiny network requests
        # into a handful of large ones.
        #
        # PRODUCTION: raise to 20-100 when throughput (messages per second)
        # matters most, such as logs or metrics. Lower to 0 only when latency
        # (time taken by one message)
        # genuinely matters more than efficiency.
        "linger.ms": 5,
        # ---------------------------------------------------------------
        # HOW BIG MAY ONE BATCH GET
        # ---------------------------------------------------------------
        # Maximum bytes in a single batch. A batch is sent when it reaches
        # either this size or the `linger.ms` deadline, whichever is first.
        #
        # PRODUCTION: raise for high-volume topics so fewer, larger requests
        # are sent. Memory use grows with (batch size x active partitions),
        # so do not raise it blindly on a topic with many partitions.
        "batch.size": 256 * 1024,
        # ---------------------------------------------------------------
        # COMPRESS BATCHES ON THE WIRE AND ON DISK
        # ---------------------------------------------------------------
        # Compression represents the same data using fewer bytes. It is applied
        # per batch, which is another reason
        # `linger.ms > 0` helps: bigger batches compress better.
        #
        # A codec is one compression algorithm. WHY zstd: it gives a strong
        # size reduction for reasonable CPU work on typical JSON payloads.
        # `lz4` is faster but compresses less;
        # `gzip` compresses slightly more but costs noticeably more CPU.
        #
        # PRODUCTION: keep compression on for anything high-volume. The
        # network and disk savings almost always outweigh the CPU cost.
        "compression.type": "zstd",
        # ---------------------------------------------------------------
        # HOW LONG MAY DELIVERY TAKE IN TOTAL
        # ---------------------------------------------------------------
        # The total budget for one message, including all internal retries,
        # measured from the produce() call. When it expires, the message is
        # failed permanently.
        #
        # THE FIRE-AND-FORGET CATCH: because we register no delivery callback,
        # that permanent failure is never reported to this process. The
        # message is dropped silently. That is the accepted cost of this
        # pattern, and the reason not to use it for data you cannot lose.
        #
        # PRODUCTION: this, not `retries`, is the setting that bounds retrying.
        # `retries` defaults to effectively unlimited, so this timeout is what
        # actually decides when the producer gives up.
        "delivery.timeout.ms": 120_000,
    }
