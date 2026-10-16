"""
Creation of the real Kafka client object.

WHY A SEPARATE MODULE
---------------------
This is the only file in the package that touches the Kafka library directly.
Isolating that here buys two things:

1. `core.py` depends on the `KafkaProducerLike` contract (see `types.py`),
   not on a concrete library. Unit tests can therefore run the entire send
   path against a small fake object, with no broker and no network.
2. If the underlying library is ever replaced, this is the only file that
   needs to change.

An "adapter" is a small layer that translates our interface into a library's
interface. A "factory" is simply a function whose job is to create an object.
This module is both: it is the one place where our abstract contract (the
methods we require) becomes a concrete confluent-kafka object.
"""

# Postpone evaluation of annotations so type hints never trigger runtime imports.
from __future__ import annotations

# Import the small client interface and configuration shape used at this
# boundary. Neither name imports the real Kafka package.
from .types import KafkaProducerLike, ProducerConfig

# ============================================================
# STEP 1: BUILD A CONCRETE PRODUCER AT THE ADAPTER BOUNDARY
# ============================================================


def default_producer_factory(config: ProducerConfig) -> KafkaProducerLike:
    """Create a real Kafka producer from a configuration dictionary.

    Why a factory function rather than calling `Producer(config)` inline in
    `core.py`: it gives tests a single seam to replace. A test passes its own
    factory that returns a fake, and `core.py` is exercised unchanged.

    Args:
        config: Producer settings, normally from
            `config.build_producer_config()`.

    Returns:
        A live producer satisfying the `KafkaProducerLike` contract.

    Failure behaviour:
        Raises `RuntimeError` with an installation hint when confluent-kafka
        is unavailable. Otherwise propagates `KafkaException` when the client
        rejects configuration. Construction does not prove broker reachability
        because connections are opened lazily in the background.
    """
    # Stage 1.0 - Load the optional infrastructure dependency only when a real
    # client is requested. Core types and fake-based tests stay importable in
    # environments that deliberately do not install Kafka.
    # ``try`` lets us replace Python's short ModuleNotFoundError with guidance
    # that tells a junior developer exactly how to fix the environment.
    try:
        # Import here, rather than at module load time, because this dependency
        # is required only when the application asks for a real Kafka client.
        from confluent_kafka import Producer as ConfluentProducer
    # ImportError means the optional Kafka package is absent or cannot load.
    except ImportError as error:
        # Preserve the original error as the cause while adding an actionable
        # message at this package's abstraction boundary.
        raise RuntimeError(
            "confluent-kafka is required to create a real producer; "
            "install the project dependencies or inject producer_factory."
        ) from error

    # Stage 2.0 - Hand validated configuration to the concrete adapter.
    # Pass the reviewed settings to confluent-kafka and return its asynchronous
    # producer. "Asynchronous" means produce() queues work and returns without
    # waiting for the broker's final answer.
    return ConfluentProducer(config)
