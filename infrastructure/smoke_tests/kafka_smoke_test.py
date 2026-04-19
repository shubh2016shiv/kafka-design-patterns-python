"""
Simple, isolated smoke test for real Kafka infrastructure.

The goal is intentionally small:
- Validate broker connectivity from host to external listener.
- Produce one message.
- Consume the same message back.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any

from kafka import KafkaConsumer, KafkaProducer
from kafka.admin import KafkaAdminClient, NewTopic
from kafka.errors import TopicAlreadyExistsError


class SmokeTestError(Exception):
    """Base exception for smoke-test failures."""


@dataclass(frozen=True)
class SmokeTestConfig:
    """Configuration model for the smoke test."""

    bootstrap_servers: str
    topic_prefix: str
    timeout_seconds: int


class KafkaSmokeTestRunner:
    """Executes stage-based smoke validation for Kafka."""

    def __init__(self, config: SmokeTestConfig) -> None:
        self._config = config

    def run(self) -> None:
        # Stage 1.0 - Initialize identifiers:
        # Unique identifiers make the smoke test repeatable and isolated.
        test_run_id = uuid.uuid4().hex
        topic = f"{self._config.topic_prefix}-{test_run_id[:8]}"
        message_key = f"smoke-key-{test_run_id}"
        payload = self._build_payload(test_run_id=test_run_id)

        # Stage 1.1 - Ensure topic exists:
        self._ensure_topic(topic=topic)

        # Stage 2.0 - Produce a smoke message:
        self._produce(topic=topic, key=message_key, payload=payload)

        # Stage 3.0 - Consume and verify:
        received = self._consume(topic=topic, expected_test_run_id=test_run_id)
        if not received:
            raise SmokeTestError("Smoke test failed: message was not consumed within timeout window.")

        print("SMOKE TEST RESULT: PASS")
        print(f"bootstrap_servers={self._config.bootstrap_servers}")
        print(f"topic={topic}")
        print(f"test_run_id={test_run_id}")

    def _build_payload(self, test_run_id: str) -> dict[str, Any]:
        return {
            "test_run_id": test_run_id,
            "timestamp_utc_epoch": int(time.time()),
            "source_host": socket.gethostname(),
            "purpose": "infrastructure-smoke-test",
            "description": "Validates producer and consumer flow against real Kafka infra.",
        }

    def _ensure_topic(self, topic: str) -> None:
        admin = KafkaAdminClient(
            bootstrap_servers=self._config.bootstrap_servers,
            client_id="infra-smoke-admin",
            request_timeout_ms=15000,
        )
        try:
            admin.create_topics(
                new_topics=[NewTopic(name=topic, num_partitions=1, replication_factor=1)],
                validate_only=False,
            )
        except TopicAlreadyExistsError:
            # Topic already exists from prior interrupted run; this is safe to continue.
            pass
        finally:
            admin.close()

    def _produce(self, topic: str, key: str, payload: dict[str, Any]) -> None:
        producer = KafkaProducer(
            bootstrap_servers=self._config.bootstrap_servers,
            key_serializer=lambda value: value.encode("utf-8"),
            value_serializer=lambda value: json.dumps(value).encode("utf-8"),
            retries=3,
            request_timeout_ms=15000,
            acks="all",
        )
        try:
            future = producer.send(topic, key=key, value=payload)
            future.get(timeout=20)
            producer.flush(timeout=20)
        finally:
            producer.close()

    def _consume(self, topic: str, expected_test_run_id: str) -> bool:
        consumer = KafkaConsumer(
            topic,
            bootstrap_servers=self._config.bootstrap_servers,
            group_id=f"infra-smoke-group-{uuid.uuid4().hex[:8]}",
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            consumer_timeout_ms=self._config.timeout_seconds * 1000,
            value_deserializer=lambda value: json.loads(value.decode("utf-8")),
            key_deserializer=lambda value: value.decode("utf-8") if value else None,
            request_timeout_ms=20000,
        )
        try:
            for message in consumer:
                value = message.value if isinstance(message.value, dict) else {}
                if value.get("test_run_id") == expected_test_run_id:
                    return True
            return False
        finally:
            consumer.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run isolated Kafka smoke test against real infrastructure.")
    parser.add_argument(
        "--bootstrap-servers",
        default="localhost:9094",
        help="Kafka bootstrap servers, e.g. localhost:9094",
    )
    parser.add_argument(
        "--topic-prefix",
        default="infra-smoke",
        help="Prefix for temporary smoke-test topic names.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=30,
        help="Consume timeout window in seconds.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    config = SmokeTestConfig(
        bootstrap_servers=args.bootstrap_servers,
        topic_prefix=args.topic_prefix,
        timeout_seconds=args.timeout_seconds,
    )
    runner = KafkaSmokeTestRunner(config=config)

    try:
        runner.run()
        return 0
    except Exception as error:  # noqa: BLE001 - keep CLI output straightforward for operations use.
        print(f"SMOKE TEST RESULT: FAIL\nreason={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
