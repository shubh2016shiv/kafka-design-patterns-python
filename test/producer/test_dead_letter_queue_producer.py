"""Unit tests for dead_letter_queue producer safety and wiring behavior."""

import sys
import types
import unittest
from unittest.mock import Mock, patch

# Test shims:
# dead_letter_queue clients import singleton/topic-routing modules at import
# time; those modules pull environment-specific dependencies and package paths.
# Provide lightweight shims so unit tests can run in isolation.
if "producers.singleton.singleton_producer" not in sys.modules:
    fake_singleton_module = types.ModuleType("producers.singleton.singleton_producer")

    class _FakeSingletonProducer:  # pragma: no cover - import shim only
        @staticmethod
        def get_instance(config):
            return {"shim_singleton_config": config}

    fake_singleton_module.SingletonProducer = _FakeSingletonProducer
    sys.modules["producers.singleton.singleton_producer"] = fake_singleton_module

if "producers.topic_routing.topic_routing_producer" not in sys.modules:
    fake_routing_module = types.ModuleType("producers.topic_routing.topic_routing_producer")

    class _FakeTopicRoutingProducer:  # pragma: no cover - import shim only
        def __init__(self, service_name, producer=None):
            self.service_name = service_name
            self.producer = producer

        def send_to_topic(self, topic, data, **kwargs):
            return None

        def send_with_metadata(self, data, metadata, **kwargs):
            return None

    fake_routing_module.TopicRoutingProducer = _FakeTopicRoutingProducer
    sys.modules["producers.topic_routing.topic_routing_producer"] = fake_routing_module

from config.kafka_config import Environment
from producers.dead_letter_queue.core import (
    DeadLetterQueueProducer,
    FaultToleranceConfig,
    SlidingWindowHealthMonitor,
)
from producers.dead_letter_queue.demo import run_demo
from producers.dead_letter_queue.types import CircuitState, SendAttemptResult


class DeadLetterQueueProducerTests(unittest.TestCase):
    """Focused regression tests for DLQ producer reliability fixes."""

    def test_default_underlying_producer_factory_respects_environment_and_bootstrap(self):
        """Factory must use caller environment and bootstrap override, not hardcoded prod."""
        from producers.dead_letter_queue.clients import default_underlying_producer_factory

        with (
            patch("producers.dead_letter_queue.clients.get_producer_config") as get_config,
            patch(
                "producers.dead_letter_queue.clients.SingletonProducer.get_instance"
            ) as get_instance,
        ):
            get_config.return_value = {
                "bootstrap.servers": "prod:9092",
                "client.id": "orders-service",
            }
            sentinel = object()
            get_instance.return_value = sentinel

            result = default_underlying_producer_factory(
                environment=Environment.DEVELOPMENT,
                bootstrap_servers="localhost:19094",
            )

            self.assertIs(result, sentinel)
            get_config.assert_called_once_with(environment=Environment.DEVELOPMENT)
            called_config = get_instance.call_args.args[0]
            self.assertEqual(called_config["bootstrap.servers"], "localhost:19094")

    def test_constructor_propagates_connection_context_to_underlying_factory(self):
        """Core constructor should pass caller config/environment/bootstrap to factory wiring."""
        with (
            patch(
                "producers.dead_letter_queue.core.default_underlying_producer_factory"
            ) as build_underlying,
            patch(
                "producers.dead_letter_queue.core.default_routing_producer_factory"
            ) as build_routing,
        ):
            underlying = Mock()
            routing = Mock()
            build_underlying.return_value = underlying
            build_routing.return_value = routing

            DeadLetterQueueProducer(
                service_name="payments",
                producer_config={"bootstrap.servers": "cluster-a:9092"},
                environment=Environment.STAGING,
                bootstrap_servers="localhost:19094",
            )

            build_underlying.assert_called_once()
            kwargs = build_underlying.call_args.kwargs
            self.assertEqual(kwargs["config"], {"bootstrap.servers": "cluster-a:9092"})
            self.assertEqual(kwargs["environment"], Environment.STAGING)
            self.assertEqual(kwargs["bootstrap_servers"], "localhost:19094")

    def test_sliding_window_snapshot_reports_metrics_without_lock_reentry(self):
        """Snapshot should compute metrics in one lock scope and return consistent values."""
        monitor = SlidingWindowHealthMonitor(
            FaultToleranceConfig(
                health_window_size=5,
                error_rate_unhealthy_threshold=0.4,
            )
        )

        monitor.record_operation(success=True, elapsed_seconds=0.10)
        monitor.record_operation(success=False, elapsed_seconds=0.30)

        snapshot = monitor.get_metrics_snapshot()

        self.assertEqual(snapshot["window_sample_count"], 2)
        self.assertAlmostEqual(snapshot["window_error_rate"], 0.5)
        self.assertAlmostEqual(snapshot["avg_latency_seconds"], 0.2)
        self.assertFalse(snapshot["is_healthy"])

    def test_run_demo_uses_bootstrap_override_when_provided(self):
        """Demo API must actually wire the bootstrap override into producer creation."""
        fake_producer = Mock()
        fake_producer.health_status.return_value = {
            "circuit_breaker": {"state": "closed", "failure_count": 0},
            "bulkhead": {"active_count": 0, "max_concurrent_sends": 100},
            "health_monitor": {
                "total_operations": 0,
                "total_errors": 0,
                "window_error_rate": 0.0,
                "avg_latency_seconds": 0.0,
                "is_healthy": True,
                "window_sample_count": 0,
            },
        }
        fake_producer.send_with_fault_tolerance.return_value = SendAttemptResult(
            success=True,
            retry_count=0,
            circuit_state=CircuitState.CLOSED,
        )

        with (
            patch("producers.dead_letter_queue.demo.create_dlq_producer") as create_producer,
            patch("producers.dead_letter_queue.demo._print_json"),
            patch("producers.dead_letter_queue.demo._explain_config"),
            patch("producers.dead_letter_queue.demo.logger.info"),
            patch("producers.dead_letter_queue.demo.logger.warning"),
        ):
            create_producer.return_value = fake_producer

            run_demo(bootstrap_servers="localhost:19094")

            create_producer.assert_called_once()
            self.assertEqual(
                create_producer.call_args.kwargs["bootstrap_servers"],
                "localhost:19094",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
