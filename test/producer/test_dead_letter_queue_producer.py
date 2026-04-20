"""Unit tests for dead_letter_queue producer safety and wiring behavior."""

import sys
import types
import unittest
from unittest.mock import Mock, patch

from pybreaker import CircuitBreakerError

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

if "producers.content_based_router.core" not in sys.modules:
    fake_router_core = types.ModuleType("producers.content_based_router.core")

    class _FakeContentBasedRouter:  # pragma: no cover - import shim only
        def __init__(self, service_name, _dispatcher=None, **_kwargs):
            self.service_name = service_name

        def send_direct(self, _topic, _data, **_kwargs):
            return None

        def send_with_context(self, _data, _context, **_kwargs):
            return None

    fake_router_core.ContentBasedRouter = _FakeContentBasedRouter
    sys.modules["producers.content_based_router.core"] = fake_router_core

if "producers.content_based_router.clients" not in sys.modules:
    fake_router_clients = types.ModuleType("producers.content_based_router.clients")

    class _FakeSingletonProducerGateway:  # pragma: no cover - import shim only
        def __init__(self, underlying_producer):
            self._producer = underlying_producer

    fake_router_clients.SingletonProducerGateway = _FakeSingletonProducerGateway
    sys.modules["producers.content_based_router.clients"] = fake_router_clients

if "producers.content_based_router.types" not in sys.modules:
    fake_router_types = types.ModuleType("producers.content_based_router.types")

    class _FakeMessageRoutingContext:  # pragma: no cover - import shim only
        def __init__(self, priority=None, source_service=None, **kwargs):
            self.priority = priority
            self.source_service = source_service

    fake_router_types.MessageRoutingContext = _FakeMessageRoutingContext
    sys.modules["producers.content_based_router.types"] = fake_router_types

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

    def test_send_with_fault_tolerance_forces_delivery_confirmation_on_primary_send(self):
        """Success path must require broker-confirmed delivery before returning success."""
        underlying = Mock()
        routing = Mock()
        config = FaultToleranceConfig(max_retries=0, delivery_confirmation_timeout_seconds=7.5)

        producer = DeadLetterQueueProducer(
            service_name="payments",
            config=config,
            underlying_producer=underlying,
            routing_producer=routing,
        )
        result = producer.send_with_fault_tolerance(
            topic="payments-events",
            data={"transaction_id": "txn-1001"},
        )

        self.assertTrue(result.success)
        routing.send_to_topic.assert_called_once()
        send_kwargs = routing.send_to_topic.call_args.kwargs
        self.assertTrue(send_kwargs["require_delivery_confirmation"])
        self.assertEqual(send_kwargs["delivery_timeout_seconds"], 7.5)
        underlying.send.assert_not_called()

    def test_send_with_fault_tolerance_confirms_dlq_write_when_retries_exhaust(self):
        """Failure path must preserve to DLQ with broker confirmation requirements."""
        underlying = Mock()
        routing = Mock()
        routing.send_to_topic.side_effect = RuntimeError("broker rejected message")
        config = FaultToleranceConfig(max_retries=0, delivery_confirmation_timeout_seconds=4.0)

        producer = DeadLetterQueueProducer(
            service_name="payments",
            config=config,
            underlying_producer=underlying,
            routing_producer=routing,
        )
        result = producer.send_with_fault_tolerance(
            topic="payments-events",
            data={"transaction_id": "txn-2002"},
        )

        self.assertFalse(result.success)
        underlying.send.assert_called_once()
        self.assertEqual(underlying.send.call_args.args[0], "payments-dead-letter-queue")
        dlq_kwargs = underlying.send.call_args.kwargs
        self.assertTrue(dlq_kwargs["require_delivery_confirmation"])
        self.assertEqual(dlq_kwargs["delivery_timeout_seconds"], 4.0)

    def test_circuit_open_fast_fails_without_secondary_network_send(self):
        """Once the circuit opens, the next call should fast-fail before routing/DLQ network sends."""
        underlying = Mock()
        routing = Mock()
        routing.send_to_topic.side_effect = RuntimeError("broker down")
        config = FaultToleranceConfig(
            failure_threshold=1,
            recovery_timeout_seconds=120,
            max_retries=3,
            initial_retry_delay_seconds=0.0,
            max_retry_delay_seconds=0.0,
        )
        producer = DeadLetterQueueProducer(
            service_name="payments",
            config=config,
            underlying_producer=underlying,
            routing_producer=routing,
        )

        first_result = producer.send_with_fault_tolerance(
            topic="payments-events",
            data={"transaction_id": "txn-cb-open-1"},
        )
        second_result = producer.send_with_fault_tolerance(
            topic="payments-events",
            data={"transaction_id": "txn-cb-open-2"},
        )

        self.assertFalse(first_result.success)
        self.assertFalse(second_result.success)
        self.assertIn("Circuit breaker OPEN", str(second_result.error))
        self.assertEqual(routing.send_to_topic.call_count, 1)
        self.assertEqual(underlying.send.call_count, 1)

    def test_retry_stops_immediately_when_circuit_breaker_opens(self):
        """tenacity should stop retrying once pybreaker raises CircuitBreakerError."""
        underlying = Mock()
        routing = Mock()
        routing.send_to_topic.side_effect = RuntimeError("broker timeout")
        config = FaultToleranceConfig(
            failure_threshold=1,
            max_retries=5,
            initial_retry_delay_seconds=0.0,
            max_retry_delay_seconds=0.0,
        )
        producer = DeadLetterQueueProducer(
            service_name="payments",
            config=config,
            underlying_producer=underlying,
            routing_producer=routing,
        )

        result = producer.send_with_fault_tolerance(
            topic="payments-events",
            data={"transaction_id": "txn-stop-open"},
        )

        self.assertFalse(result.success)
        self.assertIsInstance(result.error, CircuitBreakerError)
        self.assertEqual(result.retry_count, 0)
        self.assertEqual(routing.send_to_topic.call_count, 1)
        underlying.send.assert_called_once()

    def test_retry_count_tracks_tenacity_attempts_when_send_eventually_succeeds(self):
        """Result retry_count should reflect failed attempts before final success."""
        underlying = Mock()
        routing = Mock()
        routing.send_to_topic.side_effect = [
            RuntimeError("transient-1"),
            RuntimeError("transient-2"),
            None,
        ]
        config = FaultToleranceConfig(
            failure_threshold=10,
            max_retries=5,
            initial_retry_delay_seconds=0.0,
            max_retry_delay_seconds=0.0,
        )
        producer = DeadLetterQueueProducer(
            service_name="payments",
            config=config,
            underlying_producer=underlying,
            routing_producer=routing,
        )

        result = producer.send_with_fault_tolerance(
            topic="payments-events",
            data={"transaction_id": "txn-retry-success"},
        )

        self.assertTrue(result.success)
        self.assertEqual(result.retry_count, 2)
        self.assertEqual(routing.send_to_topic.call_count, 3)
        underlying.send.assert_not_called()

    def test_bulkhead_active_count_returns_to_zero_after_send_completion(self):
        """Bulkhead slot must always be released, even when send ends in failure."""
        underlying = Mock()
        routing = Mock()
        routing.send_to_topic.side_effect = RuntimeError("persistent failure")
        config = FaultToleranceConfig(
            failure_threshold=10,
            max_retries=0,
            initial_retry_delay_seconds=0.0,
            max_retry_delay_seconds=0.0,
        )
        producer = DeadLetterQueueProducer(
            service_name="payments",
            config=config,
            underlying_producer=underlying,
            routing_producer=routing,
        )

        result = producer.send_with_fault_tolerance(
            topic="payments-events",
            data={"transaction_id": "txn-bulkhead-release"},
        )

        self.assertFalse(result.success)
        self.assertEqual(producer._bulkhead.active_count, 0)

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

    def test_run_demo_resolves_bootstrap_servers_when_override_is_missing(self):
        """Demo should resolve bootstrap servers from helper when caller gives no override."""
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
            patch("producers.dead_letter_queue.demo.read_demo_bootstrap_servers") as read_bootstrap,
            patch("producers.dead_letter_queue.demo.create_dlq_producer") as create_producer,
            patch("producers.dead_letter_queue.demo._print_json"),
            patch("producers.dead_letter_queue.demo._explain_config"),
            patch("producers.dead_letter_queue.demo.logger.info"),
            patch("producers.dead_letter_queue.demo.logger.warning"),
        ):
            read_bootstrap.return_value = "localhost:19094"
            create_producer.return_value = fake_producer

            run_demo()

            create_producer.assert_called_once()
            self.assertEqual(
                create_producer.call_args.kwargs["bootstrap_servers"],
                "localhost:19094",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
