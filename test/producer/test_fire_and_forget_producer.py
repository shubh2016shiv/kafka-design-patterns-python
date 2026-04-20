"""
Unit tests for the fire-and-forget producer pattern.

Test philosophy:
- No live Kafka broker required — FakeProducer satisfies KafkaProducerLike.
- Each test class covers one behavioral concern so failures are easy to locate.
- Test names describe the scenario, not the implementation, so they read as
  executable documentation of the fire-and-forget contract.
"""

import threading
import unittest
from typing import Any, Callable, Dict, List, Optional, Tuple

from producers.fire_and_forget.core import (
    FireAndForgetProducer,
    _sanitize_config,
    get_shared_producer,
    produce_event,
    produce_event_with_retry,
)
from producers.fire_and_forget.types import ProducerHealthMetrics, RetryResult, SendResult


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeProducer:
    """
    In-memory Kafka producer stand-in that satisfies KafkaProducerLike.

    Behaviour switches:
    - produce_error:         if set, produce() raises this exception.
    - delivery_error:        if set, the delivery callback receives this error object.
    - fire_callback_on_poll: if True (default), fires the pending callback when poll() is called.
    """

    def __init__(
        self,
        produce_error: Optional[Exception] = None,
        delivery_error: Optional[Any] = None,
        fire_callback_on_poll: bool = True,
    ) -> None:
        self.produce_error = produce_error
        self.delivery_error = delivery_error
        self.fire_callback_on_poll = fire_callback_on_poll

        self.produced_records: List[Dict[str, Any]] = []
        self.poll_calls: List[float] = []
        self.flush_calls: List[float] = []
        self._pending_callback: Optional[Callable] = None
        self._pending_topic: str = ""

    def produce(
        self,
        topic: str,
        value: bytes,
        key: Optional[bytes] = None,
        headers: Optional[List[Tuple[str, bytes]]] = None,
        callback: Optional[Callable[..., None]] = None,
        timestamp: Optional[int] = None,
    ) -> None:
        if self.produce_error is not None:
            raise self.produce_error

        self.produced_records.append(
            {"topic": topic, "value": value, "key": key, "headers": headers}
        )
        self._pending_callback = callback
        self._pending_topic = topic

    def poll(self, timeout: float) -> int:
        self.poll_calls.append(timeout)
        if self.fire_callback_on_poll and self._pending_callback is not None:
            cb = self._pending_callback
            self._pending_callback = None
            if self.delivery_error is not None:
                cb(self.delivery_error, None)
            else:
                cb(None, FakeKafkaMessage(self._pending_topic, partition=0, offset=42))
        return 0

    def flush(self, timeout: float) -> int:
        self.flush_calls.append(timeout)
        if self._pending_callback is not None:
            self.poll(timeout)
        return 0


class FakeKafkaMessage:
    """Delivery message stub with Confluent-style accessor methods."""

    def __init__(self, topic_name: str, partition: int, offset: int) -> None:
        self._topic_name = topic_name
        self._partition = partition
        self._offset = offset

    def topic(self) -> str:
        return self._topic_name

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset


def _make_fake_factory(fake_producer: FakeProducer):
    """Return a producer factory that always returns the given fake."""
    return lambda _config: fake_producer


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestSingletonBehavior(unittest.TestCase):
    """FireAndForgetProducer returns the same object on every get_instance() call."""

    def setUp(self) -> None:
        FireAndForgetProducer.reset_instance()

    def tearDown(self) -> None:
        FireAndForgetProducer.reset_instance()

    def test_get_instance_returns_same_object_on_repeated_calls(self) -> None:
        fake = FakeProducer()
        factory = _make_fake_factory(fake)
        config = {"bootstrap.servers": "localhost:9092"}

        instance_a = FireAndForgetProducer.get_instance(config=config, producer_factory=factory)
        instance_b = FireAndForgetProducer.get_instance(config=config, producer_factory=factory)

        self.assertIs(instance_a, instance_b)

    def test_get_instance_from_different_call_sites_returns_same_object(self) -> None:
        fake = FakeProducer()
        factory = _make_fake_factory(fake)
        config = {"bootstrap.servers": "localhost:9092"}

        direct_instance = FireAndForgetProducer.get_instance(
            config=config, producer_factory=factory
        )
        convenience_instance = get_shared_producer()

        self.assertIs(direct_instance, convenience_instance)

    def test_reset_instance_allows_fresh_initialization(self) -> None:
        fake_a = FakeProducer()
        fake_b = FakeProducer()
        config = {"bootstrap.servers": "localhost:9092"}

        instance_a = FireAndForgetProducer.get_instance(
            config=config, producer_factory=_make_fake_factory(fake_a)
        )
        FireAndForgetProducer.reset_instance()
        instance_b = FireAndForgetProducer.get_instance(
            config=config, producer_factory=_make_fake_factory(fake_b)
        )

        self.assertIsNot(instance_a, instance_b)

    def test_concurrent_get_instance_calls_return_same_object(self) -> None:
        """Multiple threads racing to initialize must all get the same instance."""
        fake = FakeProducer()
        factory = _make_fake_factory(fake)
        config = {"bootstrap.servers": "localhost:9092"}
        results: list = []
        lock = threading.Lock()

        def _worker() -> None:
            inst = FireAndForgetProducer.get_instance(config=config, producer_factory=factory)
            with lock:
                results.append(inst)

        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        first = results[0]
        self.assertTrue(all(r is first for r in results))


class TestSendHappyPath(unittest.TestCase):
    """send() enqueues the message and returns SendResult(success=True)."""

    def setUp(self) -> None:
        FireAndForgetProducer.reset_instance()
        self.fake = FakeProducer()
        config = {"bootstrap.servers": "localhost:9092"}
        self.producer = FireAndForgetProducer.get_instance(
            config=config, producer_factory=_make_fake_factory(self.fake)
        )

    def tearDown(self) -> None:
        FireAndForgetProducer.reset_instance()

    def test_send_returns_success_result(self) -> None:
        result = self.producer.send(topic="events", data={"user_id": 1})

        self.assertIsInstance(result, SendResult)
        self.assertTrue(result.success)
        self.assertIsNone(result.error_message)

    def test_send_enqueues_one_record(self) -> None:
        self.producer.send(topic="events", data={"user_id": 1})

        self.assertEqual(len(self.fake.produced_records), 1)

    def test_send_serializes_data_as_json_bytes(self) -> None:
        import json

        self.producer.send(topic="events", data={"x": 42})

        raw = self.fake.produced_records[0]["value"]
        self.assertEqual(json.loads(raw), {"x": 42})

    def test_send_encodes_key_as_utf8_bytes(self) -> None:
        self.producer.send(topic="events", data={"x": 1}, key="my-key")

        self.assertEqual(self.fake.produced_records[0]["key"], b"my-key")

    def test_send_without_key_passes_none_key(self) -> None:
        self.producer.send(topic="events", data={"x": 1})

        self.assertIsNone(self.fake.produced_records[0]["key"])

    def test_send_calls_poll_zero(self) -> None:
        self.producer.send(topic="events", data={"x": 1})

        self.assertIn(0, self.fake.poll_calls)

    def test_result_topic_matches_target_topic(self) -> None:
        result = self.producer.send(topic="page-views", data={"page": "/home"})

        self.assertEqual(result.topic, "page-views")

    def test_result_message_key_matches_supplied_key(self) -> None:
        result = self.producer.send(topic="events", data={"x": 1}, key="k1")

        self.assertEqual(result.message_key, "k1")


class TestSendValidation(unittest.TestCase):
    """send() raises ValueError for invalid inputs before touching the broker."""

    def setUp(self) -> None:
        FireAndForgetProducer.reset_instance()
        self.producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(FakeProducer()),
        )

    def tearDown(self) -> None:
        FireAndForgetProducer.reset_instance()

    def test_send_raises_value_error_when_data_is_none(self) -> None:
        with self.assertRaises(ValueError):
            self.producer.send(topic="events", data=None)  # type: ignore[arg-type]

    def test_send_raises_value_error_when_data_is_not_json_serializable(self) -> None:
        with self.assertRaises(ValueError):
            self.producer.send(topic="events", data={"fn": lambda x: x})  # type: ignore[arg-type]

    def test_send_raises_value_error_when_topic_is_blank(self) -> None:
        with self.assertRaises(ValueError):
            self.producer.send(topic="   ", data={"x": 1})

    def test_send_raises_value_error_when_key_is_blank(self) -> None:
        with self.assertRaises(ValueError):
            self.producer.send(topic="events", data={"x": 1}, key="   ")


class TestSendProduceError(unittest.TestCase):
    """send() returns SendResult(success=False) when produce() raises."""

    def setUp(self) -> None:
        FireAndForgetProducer.reset_instance()
        self.fake = FakeProducer(produce_error=RuntimeError("buffer full"))
        self.producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(self.fake),
        )

    def tearDown(self) -> None:
        FireAndForgetProducer.reset_instance()

    def test_send_returns_failure_result_when_produce_raises(self) -> None:
        result = self.producer.send(topic="events", data={"x": 1})

        self.assertFalse(result.success)
        self.assertIn("buffer full", result.error_message)


class TestHealthGuard(unittest.TestCase):
    """send() refuses messages when the producer is marked unhealthy."""

    def setUp(self) -> None:
        FireAndForgetProducer.reset_instance()

    def tearDown(self) -> None:
        FireAndForgetProducer.reset_instance()

    def test_send_returns_failure_when_producer_is_unhealthy(self) -> None:
        fake = FakeProducer()
        producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(fake),
        )
        # Force unhealthy state directly.
        producer._is_healthy = False

        result = producer.send(topic="events", data={"x": 1})

        self.assertFalse(result.success)
        self.assertIn("unhealthy", result.error_message.lower())

    def test_no_produce_call_when_producer_is_unhealthy(self) -> None:
        fake = FakeProducer()
        producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(fake),
        )
        producer._is_healthy = False
        producer.send(topic="events", data={"x": 1})

        self.assertEqual(len(fake.produced_records), 0)


class TestDeliveryCallback(unittest.TestCase):
    """Default delivery callback updates health metrics correctly."""

    def setUp(self) -> None:
        FireAndForgetProducer.reset_instance()
        self.fake = FakeProducer(fire_callback_on_poll=True)
        self.producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(self.fake),
        )

    def tearDown(self) -> None:
        FireAndForgetProducer.reset_instance()

    def test_successful_delivery_increments_messages_sent(self) -> None:
        self.producer.send(topic="events", data={"x": 1})
        # poll(0) fires the callback in FakeProducer.
        self.assertEqual(self.producer._messages_sent, 1)

    def test_delivery_failure_increments_error_count(self) -> None:
        fake = FakeProducer(delivery_error="leader not available", fire_callback_on_poll=True)
        FireAndForgetProducer.reset_instance()
        producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(fake),
        )
        producer.send(topic="events", data={"x": 1})

        self.assertEqual(producer._error_count, 1)

    def test_custom_callback_is_invoked_on_delivery(self) -> None:
        received: list = []

        def my_callback(err, msg) -> None:
            received.append((err, msg))

        self.producer.send(topic="events", data={"x": 1}, callback=my_callback)

        self.assertEqual(len(received), 1)
        self.assertIsNone(received[0][0])  # err should be None on success

    def test_custom_callback_does_not_skip_default_health_tracking(self) -> None:
        received: list = []
        fake = FakeProducer(delivery_error="broker timeout", fire_callback_on_poll=True)
        FireAndForgetProducer.reset_instance()
        producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(fake),
        )

        def my_callback(err, msg) -> None:
            received.append((err, msg))

        producer.send(topic="events", data={"x": 1}, callback=my_callback)

        self.assertEqual(len(received), 1)
        self.assertEqual(producer._error_count, 1)


class TestConfirmationMode(unittest.TestCase):
    """require_delivery_confirmation=True blocks until the broker acks."""

    def setUp(self) -> None:
        FireAndForgetProducer.reset_instance()

    def tearDown(self) -> None:
        FireAndForgetProducer.reset_instance()

    def test_confirmation_mode_succeeds_when_callback_fires(self) -> None:
        fake = FakeProducer(fire_callback_on_poll=True)
        producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(fake),
        )

        result = producer.send(
            topic="events",
            data={"x": 1},
            require_delivery_confirmation=True,
            delivery_timeout_seconds=5.0,
        )

        self.assertTrue(result.success)

    def test_confirmation_mode_raises_timeout_error_when_callback_never_fires(self) -> None:
        fake = FakeProducer(fire_callback_on_poll=False)
        producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(fake),
        )

        with self.assertRaises(TimeoutError):
            producer.send(
                topic="events",
                data={"x": 1},
                require_delivery_confirmation=True,
                delivery_timeout_seconds=0.2,
            )

    def test_confirmation_mode_raises_runtime_error_on_broker_rejection(self) -> None:
        fake = FakeProducer(
            delivery_error="unknown topic",
            fire_callback_on_poll=True,
        )
        producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(fake),
        )

        with self.assertRaises(RuntimeError):
            producer.send(
                topic="events",
                data={"x": 1},
                require_delivery_confirmation=True,
                delivery_timeout_seconds=5.0,
            )


class TestSendWithRetry(unittest.TestCase):
    """send_with_retry() applies exponential backoff on local enqueue failure."""

    def setUp(self) -> None:
        FireAndForgetProducer.reset_instance()

    def tearDown(self) -> None:
        FireAndForgetProducer.reset_instance()

    def test_returns_success_when_first_attempt_succeeds(self) -> None:
        producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(FakeProducer()),
        )

        result = producer.send_with_retry(topic="events", data={"x": 1}, max_retries=3)

        self.assertIsInstance(result, RetryResult)
        self.assertTrue(result.success)
        self.assertEqual(result.attempts_made, 1)

    def test_returns_failure_after_all_retries_exhausted(self) -> None:
        fake = FakeProducer(produce_error=RuntimeError("buffer full"))
        producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(fake),
        )

        result = producer.send_with_retry(
            topic="events",
            data={"x": 1},
            max_retries=2,
            retry_delay_seconds=0.01,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.attempts_made, 3)
        self.assertIn("buffer full", result.final_error_message)

    def test_result_topic_matches_target(self) -> None:
        producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(FakeProducer()),
        )

        result = producer.send_with_retry(topic="my-topic", data={"x": 1})

        self.assertEqual(result.topic, "my-topic")

    def test_stops_retrying_for_validation_error(self) -> None:
        producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(FakeProducer()),
        )

        result = producer.send_with_retry(topic="events", data=None, max_retries=3)  # type: ignore[arg-type]

        self.assertFalse(result.success)
        self.assertEqual(result.attempts_made, 1)
        self.assertIn("cannot be None", result.final_error_message)

    def test_stops_retrying_when_producer_is_unhealthy(self) -> None:
        producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(FakeProducer()),
        )
        producer._is_healthy = False

        result = producer.send_with_retry(topic="events", data={"x": 1}, max_retries=3)

        self.assertFalse(result.success)
        self.assertEqual(result.attempts_made, 1)
        self.assertIn("unhealthy", result.final_error_message.lower())


class TestHealthMetrics(unittest.TestCase):
    """get_health_metrics() returns a correct snapshot of producer state."""

    def setUp(self) -> None:
        FireAndForgetProducer.reset_instance()
        self.fake = FakeProducer(fire_callback_on_poll=True)
        self.producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092", "sasl.password": "s3cr3t"},
            producer_factory=_make_fake_factory(self.fake),
        )

    def tearDown(self) -> None:
        FireAndForgetProducer.reset_instance()

    def test_initial_metrics_show_healthy_state(self) -> None:
        metrics = self.producer.get_health_metrics()

        self.assertIsInstance(metrics, ProducerHealthMetrics)
        self.assertTrue(metrics.is_healthy)
        self.assertEqual(metrics.messages_sent, 0)
        self.assertEqual(metrics.error_count, 0)

    def test_messages_sent_increments_after_successful_delivery(self) -> None:
        self.producer.send(topic="events", data={"x": 1})

        metrics = self.producer.get_health_metrics()
        self.assertEqual(metrics.messages_sent, 1)

    def test_error_rate_uses_total_delivery_callbacks(self) -> None:
        failing_fake = FakeProducer(delivery_error="transient error", fire_callback_on_poll=True)
        FireAndForgetProducer.reset_instance()
        producer = FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(failing_fake),
        )

        producer.send(topic="events", data={"x": 1})
        failing_fake.delivery_error = None
        for _ in range(9):
            producer.send(topic="events", data={"x": 1})

        metrics = producer.get_health_metrics()
        self.assertAlmostEqual(metrics.error_rate, 0.1)
        self.assertTrue(metrics.is_healthy)

    def test_sanitized_config_redacts_password_fields(self) -> None:
        metrics = self.producer.get_health_metrics()

        self.assertEqual(metrics.sanitized_config["sasl.password"], "***REDACTED***")
        self.assertEqual(metrics.sanitized_config["bootstrap.servers"], "localhost:9092")


class TestSanitizeConfig(unittest.TestCase):
    """_sanitize_config() replaces credential keys and preserves safe keys."""

    def test_password_key_is_redacted(self) -> None:
        result = _sanitize_config({"sasl.password": "secret", "bootstrap.servers": "host:9092"})

        self.assertEqual(result["sasl.password"], "***REDACTED***")
        self.assertEqual(result["bootstrap.servers"], "host:9092")

    def test_token_key_is_redacted(self) -> None:
        result = _sanitize_config({"oauth.token": "tok123"})

        self.assertEqual(result["oauth.token"], "***REDACTED***")

    def test_non_sensitive_keys_are_preserved(self) -> None:
        result = _sanitize_config({"linger.ms": 5, "acks": "all"})

        self.assertEqual(result["linger.ms"], 5)
        self.assertEqual(result["acks"], "all")


class TestConvenienceFunctions(unittest.TestCase):
    """Module-level produce_event() and produce_event_with_retry() delegate to shared producer."""

    def setUp(self) -> None:
        FireAndForgetProducer.reset_instance()
        fake = FakeProducer()
        FireAndForgetProducer.get_instance(
            config={"bootstrap.servers": "localhost:9092"},
            producer_factory=_make_fake_factory(fake),
        )
        self.fake = fake

    def tearDown(self) -> None:
        FireAndForgetProducer.reset_instance()

    def test_produce_event_returns_send_result(self) -> None:
        result = produce_event(topic="events", data={"x": 1})

        self.assertIsInstance(result, SendResult)
        self.assertTrue(result.success)

    def test_produce_event_with_retry_returns_retry_result(self) -> None:
        result = produce_event_with_retry(topic="events", data={"x": 1})

        self.assertIsInstance(result, RetryResult)
        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
