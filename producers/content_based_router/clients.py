"""
Content-Based Router — Kafka dispatcher adapter.

Why this module exists:
  Isolate the Kafka infrastructure dependency (SingletonProducer) behind a thin
  adapter so core routing logic stays testable without a live broker.

  In tests, inject any object that satisfies KafkaMessageDispatcher (from types.py)
  instead of building a real SingletonProducer.

ASCII wiring:

    build_singleton_dispatcher(config)
               │
               ▼
    SingletonProducerGateway
               │   wraps
               ▼
    SingletonProducer.get_instance(config)
               │   delegates to
               ▼
    confluent_kafka.Producer (production)
"""

from __future__ import annotations

# logging: operational breadcrumbs for dispatcher construction and config decisions.
import logging

# typing: explicit contracts for config and dispatcher interfaces.
from typing import Any, Dict, Optional

# SingletonProducer ensures one shared Kafka producer per application process,
# avoiding repeated connection setup and reducing broker load.
from ..singleton.singleton_producer import SingletonProducer

logger = logging.getLogger(__name__)


class SingletonProducerGateway:
    """
    Thin adapter wrapping SingletonProducer to satisfy the KafkaMessageDispatcher
    protocol expected by ContentBasedRouter.

    Why wrap instead of using SingletonProducer directly:
    - The gateway is the narrow seam tests replace with a fake object.
    - It also lets us add cross-cutting concerns (header injection, metrics
      counters) without editing the shared SingletonProducer class.

    Production note:
    SingletonProducer uses the double-checked locking pattern to guarantee one
    shared confluent_kafka.Producer per process.  All routing strategies share
    the same underlying connection pool and internal message queue.
    """

    def __init__(self, underlying_producer: SingletonProducer) -> None:
        """
        Wrap an already-initialised SingletonProducer instance.

        Args:
            underlying_producer: A live SingletonProducer instance.  Obtain
                                  one via build_singleton_dispatcher() below.
        """
        self._producer = underlying_producer

    def send(self, topic: str, data: Dict[str, Any], **kwargs: Any) -> None:
        """
        Enqueue one message for asynchronous delivery.

        Args:
            topic:  Target Kafka topic name resolved by the routing rule chain.
            data:   Raw Python dict that SingletonProducer will JSON-serialise
                    before handing to the confluent_kafka producer.
            **kwargs: Passed through to SingletonProducer.send() — supports
                      `key`, `headers`, `partition`, and delivery callbacks.

        Failure behavior:
            Propagates any exception from SingletonProducer.send() so the caller
            (ContentBasedRouter) can update its error metrics and re-raise.
        """
        # Stage 1.0: Delegate to the underlying singleton; it owns serialisation
        # and the delivery-callback lifecycle.
        self._producer.send(topic, data, **kwargs)

    def flush(self, timeout: float) -> int:
        """
        Block until the internal message queue drains or timeout expires.

        Args:
            timeout: Maximum seconds to wait.  Use DEFAULT_FLUSH_TIMEOUT_SECONDS
                     from constants.py unless you have a specific SLA requirement.

        Returns:
            Number of messages still in the queue after the timeout.  A non-zero
            return does not mean those messages were lost — the producer continues
            retrying in the background; call flush() again if ordering matters.
        """
        return self._producer.flush(timeout)


def build_singleton_dispatcher(
    config: Optional[Dict[str, Any]] = None,
) -> SingletonProducerGateway:
    """
    Construct the default dispatcher used by ContentBasedRouter in production.

    Stage 1.0:
    Resolve Kafka producer config — caller-supplied config takes precedence;
    falls back to the PRODUCTION environment defaults from kafka_config.py.

    Stage 2.0:
    Obtain the process-wide SingletonProducer instance (creates on first call,
    returns the same object on subsequent calls).

    Stage 3.0:
    Wrap in SingletonProducerGateway and return.

    Args:
        config: Optional Kafka producer config dict.  Pass None to use the
                production defaults from kafka_config.py.

    Returns:
        SingletonProducerGateway: Ready-to-use dispatcher backed by a live
        confluent_kafka.Producer.
    """
    # Stage 1.0: Resolve config — deferred import avoids circular dependency
    # during module-level initialisation of the producers package.
    try:
        from config.kafka_config import Environment, get_producer_config
    except ImportError:  # pragma: no cover
        from kafka.config.kafka_config import Environment, get_producer_config  # type: ignore[no-redef]

    resolved_config = (
        config if config is not None else get_producer_config(environment=Environment.PRODUCTION)
    )
    logger.debug(
        "Building singleton dispatcher with bootstrap.servers=%s",
        resolved_config.get("bootstrap.servers", "<unset>"),
    )

    # Stage 2.0: Retrieve (or create) the singleton producer instance.
    underlying = SingletonProducer.get_instance(resolved_config)

    # Stage 3.0: Wrap and return.
    return SingletonProducerGateway(underlying)
