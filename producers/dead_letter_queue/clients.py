"""
Adapter / factory layer for the Dead Letter Queue producer dependencies.

This module isolates infrastructure wiring so core workflow logic remains
independent and testable.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..content_based_router.clients import SingletonProducerGateway
from ..content_based_router.core import ContentBasedRouter
from ..singleton.singleton_producer import SingletonProducer

try:
    from config.kafka_config import Environment, get_producer_config
except ImportError:  # pragma: no cover - fallback for installed-package layout
    from kafka.config.kafka_config import Environment, get_producer_config  # type: ignore[no-redef]


def default_underlying_producer_factory(
    config: Optional[Dict[str, Any]] = None,
    *,
    environment: Environment = Environment.DEVELOPMENT,
    bootstrap_servers: Optional[str] = None,
) -> SingletonProducer:
    """
    Create (or retrieve) the shared Kafka producer instance.

    Safety note:
    This wiring must never hardcode production endpoints. Caller-controlled
    environment/bootstrap selection prevents accidental production writes in
    demo/test/local workflows.
    """
    resolved_config = (
        dict(config) if config is not None else get_producer_config(environment=environment)
    )
    if bootstrap_servers:
        resolved_config["bootstrap.servers"] = bootstrap_servers
    return SingletonProducer.get_instance(resolved_config)


def default_routing_producer_factory(
    service_name: str,
    underlying_producer: Optional[SingletonProducer] = None,
) -> ContentBasedRouter:
    """Create a content-based router scoped to the given service."""
    dispatcher = SingletonProducerGateway(underlying_producer) if underlying_producer else None
    return ContentBasedRouter(
        service_name=service_name,
        dispatcher=dispatcher,
    )
