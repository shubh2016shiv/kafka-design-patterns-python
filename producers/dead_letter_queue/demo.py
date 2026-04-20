"""
Demonstration entrypoint for the Dead Letter Queue (DLQ) producer pattern.

Run against a local broker:
    python -m producers.dead_letter_queue.demo

What this demo exercises (in order):
  Stage 1  — Build the producer with short demo-friendly retry delays.
  Stage 2  — Show initial circuit + health status (everything CLOSED / healthy).
  Stage 3  — Send a normal message; report the structured result.
  Stage 4  — Show post-send health snapshot; compare with initial state.
  Stage 5  — Explain each config field so the reader understands every knob.

Prerequisites:
  - Kafka broker reachable at the address in config/kafka_config.py DEVELOPMENT
    settings (default: localhost:9092).
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
from typing import Optional

# Import the primary entry point and config from this package.
from .core import (
    FaultToleranceConfig,
    create_dlq_producer,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("producers.dead_letter_queue.demo")


# ── Public demo API ────────────────────────────────────────────────────────────


def run_demo(bootstrap_servers: Optional[str] = None) -> None:
    """
    Walk through the DLQ pattern with annotated, step-by-step execution.

    Input:  bootstrap_servers — optional override (e.g. "localhost:9092").
                                When None, DEVELOPMENT config defaults apply.
    """
    service_name = "demo-service"

    # ── Pattern overview ───────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("Dead Letter Queue Producer — pattern walkthrough")
    logger.info("=" * 60)
    logger.info(
        "\n"
        "  Four fault-tolerance layers (outermost → innermost):\n"
        "\n"
        "  ┌─ Circuit Breaker ─────────────────────────────────────┐\n"
        "  │  Fast-fail when the broker is known bad.              │\n"
        "  │  Prevents hammering a struggling broker.              │\n"
        "  │                                                       │\n"
        "  │  ┌─ Bulkhead ───────────────────────────────────────┐ │\n"
        "  │  │  Caps in-flight sends.  Protects app threads.    │ │\n"
        "  │  │                                                  │ │\n"
        "  │  │  ┌─ Retry + Exponential Backoff ───────────────┐ │ │\n"
        "  │  │  │  Recovers from transient broker blips.      │ │ │\n"
        "  │  │  │                                             │ │ │\n"
        "  │  │  │  ┌─ Dead Letter Queue (DLQ) ─────────────┐ │ │ │\n"
        "  │  │  │  │  Preserves messages that exhaust      │ │ │ │\n"
        "  │  │  │  │  retries for later replay / triage.   │ │ │ │\n"
        "  │  │  │  └───────────────────────────────────────┘ │ │ │\n"
        "  │  │  └─────────────────────────────────────────────┘ │ │\n"
        "  │  └──────────────────────────────────────────────────┘ │\n"
        "  └────────────────────────────────────────────────────────┘\n"
    )

    # Stage 1.0 — Build the producer with demo-friendly short retry delays.
    # In production use create_dlq_producer() or create_ha_dlq_producer() which
    # apply the right defaults automatically.
    logger.info("Stage 1.0 — Building DeadLetterQueueProducer...")
    demo_config = FaultToleranceConfig(
        failure_threshold=3,
        recovery_timeout_seconds=10,  # short for demo; default is 60 s
        max_retries=2,  # short for demo; default is 3
        initial_retry_delay_seconds=0.5,
        max_retry_delay_seconds=2.0,
        send_timeout_seconds=5.0,
    )
    # Safety note:
    # bootstrap_servers override is wired into producer creation so local demos
    # never have to rely on implicit defaults that could target another cluster.
    producer = create_dlq_producer(
        service_name=service_name,
        config=demo_config,
        bootstrap_servers=bootstrap_servers,
    )

    # Stage 2.0 — Inspect initial state before any sends.
    logger.info("Stage 2.0 — Initial health status:")
    _print_json(producer.health_status())

    # Stage 3.0 — Attempt a normal message send.
    demo_payload = {
        "event": "order.created",
        "order_id": "ord-demo-001",
        "amount_cents": 4999,
        "currency": "USD",
        "sent_at_unix": int(time.time()),
    }
    target_topic = f"{service_name}-events"
    logger.info("Stage 3.0 — Sending message to topic '%s'...", target_topic)

    result = producer.send_with_fault_tolerance(
        topic=target_topic,
        data=demo_payload,
    )

    # Stage 3.1 — Interpret the structured result.
    if result.success:
        logger.info(
            "Stage 3.1 ✓ Message delivered.\n  retry_count=%d, elapsed=%.3fs, circuit=%s",
            result.retry_count,
            result.execution_time_seconds,
            result.circuit_state.value,
        )
    else:
        logger.warning(
            "Stage 3.1 ✗ Message delivery failed after %d retries (%.3fs elapsed).\n"
            "  Message was routed to DLQ: %s\n"
            "  Circuit state after failure: %s\n"
            "  Error: %s",
            result.retry_count,
            result.execution_time_seconds,
            f"{service_name}-dead-letter-queue",
            result.circuit_state.value,
            result.error,
        )

    # Stage 4.0 — Post-send health snapshot to see how the monitor updated.
    logger.info("Stage 4.0 — Post-send health status:")
    _print_json(producer.health_status())

    # Stage 5.0 — Config explanation for interview / onboarding readiness.
    logger.info("Stage 5.0 — Configuration explanation:")
    _explain_config(demo_config)

    logger.info("=" * 60)
    logger.info("Demo complete.")
    logger.info("=" * 60)


# ── Helper utilities ───────────────────────────────────────────────────────────


def _print_json(data: dict) -> None:
    """Pretty-print a dict as JSON to the log."""
    logger.info("\n%s", json.dumps(data, indent=2, default=str))


def _explain_config(config: FaultToleranceConfig) -> None:
    """
    Log a human-readable explanation of every FaultToleranceConfig field.

    This is the kind of explanation you would give in an interview or
    during an on-call handover — what each knob does and why it's set
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
