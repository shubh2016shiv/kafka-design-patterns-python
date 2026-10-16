"""
Client adapter layer for the fire-and-forget producer package.

Why this module exists:
- isolate third-party Kafka client differences behind one stable KafkaProducerLike contract
- provide a deterministic fallback so the module works on machines that only have
  kafka-python installed (common in learning/lab environments)
- keep core.py free of import-guarded try/except blocks

ASCII fallback chain:

    default_producer_factory(config)
              |
              v
    confluent_kafka available?  ──yes──>  confluent_kafka.Producer(config)
              |
             no
              |
              v
    kafka-python available?  ──yes──>  KafkaPythonProducerAdapter(config)
              |
             no
              |
              v
    ImportError with actionable install instructions
"""

from __future__ import annotations

# logging: record fallback decisions so operators know which client is active.
import logging

# time: kafka-python adapter uses sleep to emulate Confluent's non-blocking poll().
import time

# typing: explicit contracts for config maps and callbacks.
from typing import Any, Callable, Dict, List, Optional, Tuple

from .types import KafkaProducerLike

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional import path 1 — confluent_kafka (preferred)
#
# confluent_kafka wraps the librdkafka C library:
# - true async I/O, internal batching, full Kafka protocol support
# - preferred for production and staging environments
# ---------------------------------------------------------------------------
try:
    from confluent_kafka import Producer as ConfluentProducer
except ImportError:  # pragma: no cover
    ConfluentProducer = None

# ---------------------------------------------------------------------------
# Optional import path 2 — kafka-python (fallback)
#
# Pure-Python Kafka client:
# - easier to install in restricted/air-gapped environments
# - no C compiler needed; works on all Python wheels
# - used as a learning/lab fallback when confluent_kafka is unavailable
# ---------------------------------------------------------------------------
try:
    from kafka import KafkaProducer as KafkaPythonProducer
except ImportError:  # pragma: no cover
    KafkaPythonProducer = None


# ---------------------------------------------------------------------------
# Adapter — kafka-python normalized to KafkaProducerLike
# ---------------------------------------------------------------------------


class _KafkaPythonProducedMessage:
    """
    Delivery metadata adapter with Confluent-style accessor methods.

    Why this exists:
    The delivery callback in core.py expects msg.topic(), msg.partition(), msg.offset().
    kafka-python's RecordMetadata uses attribute access (not methods), so this
    thin adapter keeps the callback contract uniform across both backends.
    """

    def __init__(self, topic_name: str, partition: int, offset: int) -> None:
        self._topic_name = topic_name
        self._partition = partition
        self._offset = offset

    def topic(self) -> str:
        """Return destination topic name."""
        return self._topic_name

    def partition(self) -> int:
        """Return destination partition index."""
        return self._partition

    def offset(self) -> int:
        """Return committed broker offset."""
        return self._offset


class KafkaPythonProducerAdapter:
    """
    kafka-python adapter implementing the KafkaProducerLike contract.

    Tradeoffs vs confluent_kafka:
    - kafka-python does not expose a Confluent-style poll() loop; delivery is
      handled via Future callbacks on a background thread.
    - poll() is emulated with a short sleep so one-shot demo scripts give
      background I/O time to complete before exiting.
    - headers and per-message timestamps are silently ignored because
      kafka-python's send() API does not accept them in the same form.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        if KafkaPythonProducer is None:
            raise ImportError("kafka-python is not installed. Run: pip install kafka-python")

        # Stage 1.0: Map dot-notation Kafka config keys to kafka-python kwargs.
        producer_kwargs: Dict[str, Any] = {
            "bootstrap_servers": config["bootstrap.servers"],
            "client_id": config.get("client.id", "fire-and-forget-producer"),
            "acks": config.get("acks", "all"),
            "retries": int(config.get("retries", 3)),
            "linger_ms": int(config.get("linger.ms", 0)),
            "batch_size": int(config.get("batch.size", 16384)),
            # kafka-python needs explicit serializers; value is already bytes from core.
            "key_serializer": lambda v: v if isinstance(v, bytes) else (v.encode() if v else None),
            "value_serializer": lambda v: v,
        }

        compression = config.get("compression.type")
        if compression:
            producer_kwargs["compression_type"] = compression

        # Stage 1.1: Create producer with safe compression fallback when the
        # local Python runtime lacks the requested codec.
        try:
            self._producer = KafkaPythonProducer(**producer_kwargs)
        except AssertionError as exc:
            if "compression codec" not in str(exc):
                raise
            logger.warning(
                "Compression codec '%s' unavailable locally — falling back to no compression.",
                compression,
            )
            producer_kwargs.pop("compression_type", None)
            self._producer = KafkaPythonProducer(**producer_kwargs)

    def produce(
        self,
        topic: str,
        value: bytes,
        key: Optional[bytes] = None,
        headers: Optional[List[Tuple[str, bytes]]] = None,
        callback: Optional[Callable[..., None]] = None,
        timestamp: Optional[int] = None,
    ) -> None:
        """
        Enqueue a message and wire the kafka-python Future into the callback.

        headers and timestamp are accepted for interface compatibility but are
        silently ignored — kafka-python's send() API handles them differently
        and this adapter prioritises portability over feature parity.
        """
        future = self._producer.send(topic, key=key, value=value)

        if callback is not None:

            def _on_success(record_metadata: Any) -> None:
                callback(
                    None,
                    _KafkaPythonProducedMessage(
                        topic_name=record_metadata.topic,
                        partition=record_metadata.partition,
                        offset=record_metadata.offset,
                    ),
                )

            def _on_error(exception: Exception) -> None:
                callback(exception, None)

            future.add_callback(_on_success)
            future.add_errback(_on_error)

    def poll(self, timeout: float) -> int:
        """
        Emulate Confluent-style poll for one-shot and demo scripts.

        kafka-python delivers messages on a background thread; a brief sleep
        gives that thread time to complete I/O and trigger Future callbacks.
        """
        time.sleep(max(timeout, 0))
        return 0

    def flush(self, timeout: float) -> int:
        """Flush all buffered records and return remaining queue depth (always 0)."""
        self._producer.flush(timeout=timeout)
        return 0


# ---------------------------------------------------------------------------
# Factory — used by core.py (injectable for tests)
# ---------------------------------------------------------------------------


def default_producer_factory(config: Dict[str, Any]) -> KafkaProducerLike:
    """
    Build a Kafka producer client with a deterministic fallback chain.

    Stage 1.0:
    Try confluent_kafka.Producer first — production-grade C library with true
    async I/O, full batching, and complete Kafka protocol support.

    Stage 1.1:
    Fall back to KafkaPythonProducerAdapter for lab/learning environments where
    the C extension is unavailable.

    Stage 1.2:
    Raise ImportError with actionable install instructions when neither
    client is available.
    """
    if ConfluentProducer is not None:
        logger.debug("Using confluent_kafka.Producer.")
        return ConfluentProducer(config)

    if KafkaPythonProducer is not None:
        logger.warning(
            "confluent_kafka not found — using kafka-python adapter. "
            "Install confluent_kafka for production use."
        )
        return KafkaPythonProducerAdapter(config)

    raise ImportError(
        "No Kafka producer client is installed. "
        "Run: pip install confluent_kafka  (preferred) "
        "or:  pip install kafka-python      (fallback)"
    )
