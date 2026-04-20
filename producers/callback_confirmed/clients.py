"""
Client adapter layer for the callback-confirmed producer workflow.

Why this module exists:
- isolate third-party Kafka client differences behind one stable contract
- provide deterministic fallback behavior across local dev and production hosts
- keep core produce logic independent from specific client libraries

ASCII flow:
    core requests producer/admin dependency
        |
        v
    factory prefers confluent_kafka when available
        |
        v
    fallback to kafka-python adapter if needed
        |
        v
    uniform contract returned to core workflow
"""

from __future__ import annotations

# logging: operational breadcrumbs for fallback decisions and compatibility behavior.
import logging

# time: kafka-python adapter uses sleep to emulate callback-serving `poll(...)`.
import time

# typing: explicit contracts for config and callback interfaces.
from typing import Any, Callable, Dict, Optional

from .types import KafkaAdminClientLike, KafkaProducerLike

logger = logging.getLogger(__name__)

# Optional dependency path 1:
# - confluent_kafka is preferred for production-like behavior/performance.
# - if unavailable, module still works through kafka-python fallback.
try:
    from confluent_kafka import Producer
    from confluent_kafka.admin import AdminClient
    from confluent_kafka.admin import NewTopic as ConfluentNewTopic
except ImportError:  # pragma: no cover - optional in local environments.
    Producer = None
    AdminClient = None
    ConfluentNewTopic = None

# Optional dependency path 2:
# - kafka-python is used as compatibility fallback for learning and local labs.
# - if unavailable and confluent_kafka is also missing, factories fail explicitly.
try:
    from kafka import KafkaConsumer as KafkaPythonConsumer
    from kafka import KafkaProducer as KafkaPythonProducer
    from kafka.admin import KafkaAdminClient as KafkaPythonAdminClient
    from kafka.admin import NewTopic as KafkaPythonNewTopic
    from kafka.errors import TopicAlreadyExistsError
except ImportError:  # pragma: no cover - optional in local environments.
    KafkaPythonConsumer = None
    KafkaPythonProducer = None
    KafkaPythonAdminClient = None
    KafkaPythonNewTopic = None
    TopicAlreadyExistsError = None


class KafkaPythonProducedMessage:
    """
    Record metadata adapter with Confluent-like accessor methods.

    Why:
    Core delivery callback code expects `topic()/partition()/offset()` methods.
    kafka-python returns metadata in a different shape, so this adapter keeps the
    public callback contract stable.
    """

    def __init__(self, topic_name: str, partition: int, offset: int):
        self._topic_name = topic_name
        self._partition = partition
        self._offset = offset

    def topic(self) -> str:
        """Return destination topic name for delivery logging."""

        return self._topic_name

    def partition(self) -> int:
        """Return destination partition for delivery logging."""

        return self._partition

    def offset(self) -> int:
        """Return committed offset for delivery logging."""

        return self._offset


class KafkaPythonProducerAdapter:
    """
    kafka-python adapter implementing the `KafkaProducerLike` contract.

    Tradeoffs:
    - keeps this teaching module portable when Confluent client is unavailable
    - preserves callback-driven behavior expected by core flow
    - uses compatible settings mapping from dot-key config to kafka-python kwargs
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        if KafkaPythonProducer is None:
            raise ImportError("kafka-python is not installed.")

        # Stage 1.0: Translate shared producer config into kafka-python kwargs.
        producer_kwargs = {
            "bootstrap_servers": config["bootstrap.servers"],
            "client_id": config.get("client.id", "simple-producer-demo"),
            "acks": config.get("acks", "all"),
            "retries": int(config.get("retries", 3)),
            "compression_type": config.get("compression.type"),
            "linger_ms": int(config.get("linger.ms", 0)),
            "batch_size": int(config.get("batch.size", 16384)),
            "max_request_size": int(config.get("message.max.bytes", 1048576)),
            "key_serializer": lambda value: value.encode("utf-8")
            if isinstance(value, str)
            else value,
            "value_serializer": lambda value: value,
        }

        # Stage 1.1: Create producer with safe compression fallback when codec
        # is configured but not available in the local Python runtime.
        try:
            self._producer = KafkaPythonProducer(**producer_kwargs)
        except AssertionError as exc:
            if "compression codec not found" not in str(exc):
                raise
            logger.warning(
                "Compression codec '%s' is configured but unavailable locally. "
                "Falling back to no compression for this run.",
                producer_kwargs["compression_type"],
            )
            producer_kwargs["compression_type"] = None
            self._producer = KafkaPythonProducer(**producer_kwargs)

    def produce(
        self, topic: str, key: Optional[str], value: bytes, callback: Callable[..., None]
    ) -> None:
        """
        Queue a message and map kafka-python futures into callback style.

        Input contract:
        - topic/key/value are already validated and serialized by core logic.
        Output contract:
        - callback is invoked on success or failure with a Confluent-like message
          adapter so downstream logging remains uniform.
        """

        send_future = self._producer.send(topic, key=key, value=value)

        def on_success(record_metadata: Any) -> None:
            callback(
                None,
                KafkaPythonProducedMessage(
                    topic_name=record_metadata.topic,
                    partition=record_metadata.partition,
                    offset=record_metadata.offset,
                ),
            )

        def on_error(exception: Exception) -> None:
            callback(exception, None)

        send_future.add_callback(on_success)
        send_future.add_errback(on_error)

    def poll(self, timeout: float) -> int:
        """
        Emulate poll behavior for one-shot scripts.

        kafka-python does not expose Confluent-style `poll`, so we wait briefly
        to give background I/O and callbacks a chance to progress.
        """

        time.sleep(max(timeout, 0))
        return 0

    def flush(self, timeout: float) -> int:
        """Flush producer buffers and return remaining queued count (always 0 here)."""

        self._producer.flush(timeout=timeout)
        return 0


class KafkaPythonTopicMetadata:
    """Small metadata holder matching fields consumed by readiness checks."""

    def __init__(self, partitions: Dict[int, Any], error: Optional[str] = None):
        self.partitions = partitions
        self.error = error


class KafkaPythonClusterMetadata:
    """Cluster metadata container normalized for core readiness checks."""

    def __init__(self, brokers: Dict[Any, Any], topics: Dict[str, KafkaPythonTopicMetadata]):
        self.brokers = brokers
        self.topics = topics


class KafkaPythonAdminClientAdapter:
    """
    Metadata adapter for environments using kafka-python.

    Why:
    The core preflight flow expects a `list_topics(timeout=...)` contract similar
    to Confluent admin metadata APIs. This adapter normalizes kafka-python output.
    """

    def __init__(self, bootstrap_servers: str):
        self._bootstrap_servers = bootstrap_servers

    def list_topics(self, timeout: float) -> KafkaPythonClusterMetadata:
        """
        Fetch cluster metadata and normalize broker/topic structures.

        Failure behavior:
        - raises ImportError when kafka-python is unavailable
        - closes consumer resources in all paths
        """

        if KafkaPythonConsumer is None:
            raise ImportError("kafka-python is not installed.")

        metadata_consumer = KafkaPythonConsumer(
            bootstrap_servers=self._bootstrap_servers,
            request_timeout_ms=max(int(timeout * 1000), 1000),
            consumer_timeout_ms=max(int(timeout * 1000), 1000),
            enable_auto_commit=False,
        )
        try:
            topic_names = metadata_consumer.topics()
            raw_broker_metadata = metadata_consumer._client.cluster.brokers()  # noqa: SLF001
            if isinstance(raw_broker_metadata, dict):
                normalized_brokers = raw_broker_metadata
            else:
                normalized_brokers = {
                    index: broker for index, broker in enumerate(raw_broker_metadata or [])
                }

            normalized_topics: Dict[str, KafkaPythonTopicMetadata] = {}
            for topic_name in topic_names:
                partition_ids = metadata_consumer.partitions_for_topic(topic_name) or set()
                normalized_topics[topic_name] = KafkaPythonTopicMetadata(
                    partitions={partition_id: None for partition_id in partition_ids}
                )

            return KafkaPythonClusterMetadata(
                brokers=dict(normalized_brokers),
                topics=normalized_topics,
            )
        finally:
            metadata_consumer.close()


def default_producer_factory(config: Dict[str, Any]) -> KafkaProducerLike:
    """
    Build a producer client with deterministic fallback selection.

    Stage 1.0:
    Prefer `confluent_kafka.Producer` for production-style reliability and speed.
    Stage 1.1:
    Fall back to `kafka-python` adapter for local compatibility.
    Stage 1.2:
    Raise a clear ImportError when no supported client is available.
    """

    if Producer is not None:
        return Producer(config)
    if KafkaPythonProducer is not None:
        return KafkaPythonProducerAdapter(config)
    raise ImportError(
        "No supported Kafka producer client is installed. Install `confluent_kafka` "
        "or `kafka-python`."
    )


def default_admin_client_factory(config: Dict[str, Any]) -> KafkaAdminClientLike:
    """
    Build metadata client used by readiness checks.

    Stage 1.0:
    Use Confluent AdminClient when available.
    Stage 1.1:
    Fall back to kafka-python metadata adapter.
    Stage 1.2:
    Raise actionable ImportError when neither dependency is installed.
    """

    if AdminClient is not None:
        return AdminClient({"bootstrap.servers": config["bootstrap.servers"]})
    if KafkaPythonConsumer is not None:
        return KafkaPythonAdminClientAdapter(config["bootstrap.servers"])
    raise ImportError(
        "No supported Kafka metadata client is installed. Install `confluent_kafka` "
        "or `kafka-python`."
    )
