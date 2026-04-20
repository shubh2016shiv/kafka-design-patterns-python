"""
Demonstration entrypoint for the Dead Letter Queue (DLQ) producer pattern.

Run against a local broker:
    python -m producers.dead_letter_queue.demo

What this demo exercises (in order):
  Stage 1  - Build the producer with short demo-friendly retry delays.
  Stage 2  - Show initial circuit + health status (everything CLOSED / healthy).
  Stage 3  - Send a normal message; report the structured result.
  Stage 4  - Show post-send health snapshot; compare with initial state.
  Stage 5  - Explain each config field so the reader understands every knob.

Prerequisites:
  - Kafka broker reachable at the resolved bootstrap servers.
  - The target topic exists, or auto-create is enabled on the broker.

Learning goals:
  After running this demo you should be able to answer:
  - What happens to a message when all retries are exhausted?
  - How does the circuit breaker protect the broker during an outage?
  - What does a SendAttemptResult tell you that a raw exception does not?
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, Optional

# Import the primary entry point and config from this package.
from .core import (
    FaultToleranceConfig,
    create_dlq_producer,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("producers.dead_letter_queue.demo")


def read_demo_bootstrap_servers() -> str:
    """
    Resolve bootstrap servers for local demo execution.

    Stage 1.0:
    Prefer infrastructure/.env KAFKA_EXTERNAL_HOST + KAFKA_EXTERNAL_PORT when present.
    Stage 1.1:
    Fall back to localhost:19094 so host-machine demos match container external listeners.
    """
    env_file_path = Path(__file__).resolve().parents[2] / "infrastructure" / ".env"
    default_host = "localhost"
    default_port = "19094"
    if not env_file_path.exists():
        return f"{default_host}:{default_port}"

    env_values: Dict[str, str] = {}
    for raw_line in env_file_path.read_text(encoding="utf-8").splitlines():
        normalized_line = raw_line.strip()
        if not normalized_line or normalized_line.startswith("#") or "=" not in normalized_line:
            continue
        key, value = normalized_line.split("=", maxsplit=1)
        env_values[key.strip()] = value.strip()

    host = env_values.get("KAFKA_EXTERNAL_HOST", default_host)
    port = env_values.get("KAFKA_EXTERNAL_PORT", default_port)
    return f"{host}:{port}"


def run_demo(bootstrap_servers: Optional[str] = None) -> None:
    """
    Walk through the DLQ pattern with annotated, step-by-step execution.

    Input:  bootstrap_servers - optional override (for example "localhost:19094").
            When None, demo bootstrap resolution reads infrastructure/.env and
            falls back to localhost:19094.
    """
    service_name = "demo-service"
    resolved_bootstrap_servers = bootstrap_servers or read_demo_bootstrap_servers()

    logger.info("=" * 60)
    logger.info("Dead Letter Queue Producer - pattern walkthrough")
    logger.info("=" * 60)
    logger.info(
        "\n"
        "Four fault-tolerance layers (outermost -> innermost):\n"
        "\n"
        "  [Circuit Breaker] -> [Bulkhead] -> [Retry + Backoff] -> [DLQ]\n"
        "\n"
        "  Circuit Breaker : Fast-fail when broker is known bad.\n"
        "  Bulkhead        : Cap in-flight sends to protect app threads.\n"
        "  Retry + Backoff : Recover from transient broker issues.\n"
        "  DLQ             : Preserve exhausted messages for replay and triage."
    )

    # Stage 1.0 - Build producer with demo-friendly short retry delays.
    logger.info("Stage 1.0 - Building DeadLetterQueueProducer...")
    demo_config = FaultToleranceConfig(
        failure_threshold=3,
        recovery_timeout_seconds=10,  # short for demo; default is 60 s
        max_retries=2,  # short for demo; default is 3
        initial_retry_delay_seconds=0.5,
        max_retry_delay_seconds=2.0,
        send_timeout_seconds=5.0,
    )
    producer = create_dlq_producer(
        service_name=service_name,
        config=demo_config,
        bootstrap_servers=resolved_bootstrap_servers,
    )
    logger.info("Resolved demo bootstrap servers: %s", resolved_bootstrap_servers)

    # Stage 2.0 - Inspect initial state before any sends.
    logger.info("Stage 2.0 - Initial health status:")
    _print_json(producer.health_status())

    # Stage 3.0 - Attempt a normal message send.
    demo_payload = {
        "event": "order.created",
        "order_id": "ord-demo-001",
        "amount_cents": 4999,
        "currency": "USD",
        "sent_at_unix": int(time.time()),
    }
    target_topic = f"{service_name}-events"
    logger.info("Stage 3.0 - Sending message to topic '%s'...", target_topic)

    result = producer.send_with_fault_tolerance(
        topic=target_topic,
        data=demo_payload,
    )

    # Stage 3.1 - Interpret the structured result.
    if result.success:
        logger.info(
            "Stage 3.1 [OK] Message delivered.\n  retry_count=%d, elapsed=%.3fs, circuit=%s",
            result.retry_count,
            result.execution_time_seconds,
            result.circuit_state.value,
        )
    else:
        logger.warning(
            "Stage 3.1 [FAIL] Message delivery failed after %d retries (%.3fs elapsed).\n"
            "  Message was routed to DLQ: %s\n"
            "  Circuit state after failure: %s\n"
            "  Error: %s",
            result.retry_count,
            result.execution_time_seconds,
            f"{service_name}-dead-letter-queue",
            result.circuit_state.value,
            result.error,
        )

    # Stage 4.0 - Post-send health snapshot to see how the monitor updated.
    logger.info("Stage 4.0 - Post-send health status:")
    _print_json(producer.health_status())

    # Stage 5.0 - Config explanation for interview / onboarding readiness.
    logger.info("Stage 5.0 - Configuration explanation:")
    _explain_config(demo_config)

    logger.info("=" * 60)
    logger.info("Demo complete.")
    logger.info("=" * 60)


def _print_json(data: dict) -> None:
    """Pretty-print a dict as JSON to the log."""
    logger.info("\n%s", json.dumps(data, indent=2, default=str))


def _explain_config(config: FaultToleranceConfig) -> None:
    """
    Log a human-readable explanation of every FaultToleranceConfig field.

    This is the kind of explanation you would give in an interview or
    during an on-call handover - what each knob does and why it's set
    to its current value.
    """
    explanations = {
        "failure_threshold": (
            f"{config.failure_threshold} consecutive failures open the circuit. "
            "Lower = faster detection; higher = more tolerance for flaky networks."
        ),
        "recovery_timeout_seconds": (
            f"{config.recovery_timeout_seconds}s before the circuit probes recovery. "
            "Shorter = faster recovery window; longer = more protection for a restarting broker."
        ),
        "success_threshold": (
            f"{config.success_threshold} consecutive successes close the circuit. "
            "Higher = more confident the broker has truly recovered."
        ),
        "max_retries": (
            f"{config.max_retries} retry attempts after the initial failure. "
            "Each retry uses exponential backoff before the message goes to DLQ."
        ),
        "initial_retry_delay_seconds": (
            f"{config.initial_retry_delay_seconds}s delay before first retry. "
            "Multiplied by retry_backoff_multiplier on each subsequent attempt."
        ),
        "max_retry_delay_seconds": (
            f"Retry delay capped at {config.max_retry_delay_seconds}s "
            "to prevent multi-minute stalls under sustained broker failure."
        ),
        "retry_backoff_multiplier": (
            f"Each retry delay is {config.retry_backoff_multiplier}x the previous. "
            "Spreads retry load over time (thundering-herd mitigation)."
        ),
        "max_concurrent_sends": (
            f"At most {config.max_concurrent_sends} sends in-flight at once. "
            "Bulkhead: prevents resource exhaustion when the broker slows down."
        ),
        "send_timeout_seconds": (
            f"{config.send_timeout_seconds}s max wait for a bulkhead slot. "
            "Should be shorter than the caller's own SLA timeout."
        ),
        "delivery_confirmation_timeout_seconds": (
            f"{config.delivery_confirmation_timeout_seconds}s max wait for broker delivery ack. "
            "Protects reliability by treating queue-only sends as incomplete."
        ),
        "health_window_size": (
            f"Error rate computed over the last {config.health_window_size} operations. "
            "Sliding window: old successes don't hide current failures."
        ),
        "error_rate_unhealthy_threshold": (
            f"Producer flagged UNHEALTHY when error rate exceeds "
            f"{config.error_rate_unhealthy_threshold:.0%}. "
            "Trigger for alerting or auto-scaling."
        ),
    }
    for field, explanation in explanations.items():
        logger.info("  %-38s  %s", field, explanation)


if __name__ == "__main__":
    run_demo()
