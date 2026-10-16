"""
Runnable tutorial for the fire-and-forget producer.

RUN IT
    python -m producers.fire_and_forget

PREREQUISITE
    A reachable Kafka broker. For the local lab from `infrastructure/`:
        cd infrastructure && docker compose up -d
    Override the address with:
        KAFKA_BOOTSTRAP_SERVERS=host:9092 python -m producers.fire_and_forget

WHAT THE FOUR SCENES TEACH
    Scene 1  produce() returns without waiting for a delivery result
    Scene 2  the produce -> poll -> flush lifecycle, with real timings
    Scene 3  the payoff: throughput, measured
    Scene 4  the honest weakness: a failure nobody is told about

Every scene is independent and prints what it is about to do, so the output
reads top to bottom as an explanation rather than a log dump.
"""

# Postpone evaluation of type hints so tutorial annotations have no runtime cost.
from __future__ import annotations

# logging configures the severity and layout of messages emitted by the library.
import logging
# time supplies a steady duration clock and the current Unix timestamp.
import time

# Import configuration helpers for both the working and deliberately broken clients.
from .config import build_producer_config, read_bootstrap_servers
# Import the behavior being demonstrated.
from .core import FireAndForgetProducer
# Import the precise JSON object type returned by the example-event builder.
from .types import JsonObject

# ============================================================
# STEP 1: TUTORIAL SETTINGS
# ============================================================

# Topic used by every scene. Created automatically if the broker allows it;
# see the note in scene 4 about why production disables that.
DEMO_TOPIC: str = "fire-and-forget-demo"

# Scene 3 sends this many messages to measure throughput. Large enough to be
# meaningful, small enough to finish in about a second on a laptop.
THROUGHPUT_MESSAGE_COUNT: int = 50_000

# Scene 4 points a producer at a port where nothing listens, to show a
# failure that is never reported. Kept short so the demo stays brisk.
UNREACHABLE_BOOTSTRAP: str = "localhost:9999"
UNREACHABLE_TIMEOUT_MS: int = 3_000


# ============================================================
# STEP 2: OUTPUT HELPERS
# ============================================================

def announce(scene_number: int, title: str) -> None:
    """Print a visible header so each scene's output is easy to find."""
    # Start on a fresh line so the previous scene's final sentence does not run
    # visually into the next heading.
    print()
    # Use the same fixed-width border above and below every heading.
    print("=" * 70)
    # Include both number and title so terminal output matches this source file.
    print(f"SCENE {scene_number}: {title}")
    # Close the visual heading block.
    print("=" * 70)


def build_example_event(event_number: int) -> JsonObject:
    """Build one example page-view event.

    Fire-and-forget suits this kind of data: high volume, and losing an
    occasional page view does not harm anyone.
    """
    # Return a new dictionary for every message so later changes cannot mutate
    # an event that was already handed to the producer.
    return {
        "event_type": "page_view",  # Tells consumers which schema to apply.
        "user_id": f"u-{1000 + event_number}",  # Example entity identifier.
        "page": "/home",  # Example business data for the page-view event.
        # Kafka and many data systems use milliseconds since 1970 for timestamps.
        "timestamp_ms": int(time.time() * 1000),
    }


# ============================================================
# STEP 3: SCENE 1 - produce() DOES NOT WAIT
# ============================================================


def scene_1_send_returns_immediately(producer: FireAndForgetProducer) -> None:
    """Show that send() returns without waiting for broker delivery.

    The point: send() does not wait for a broker acknowledgement. The message
    may still be local or may already be in flight when the call returns; this
    pattern deliberately does not observe which.
    """
    # Print what this isolated experiment is intended to prove.
    announce(1, "send() does not wait for a delivery result")

    # Explain the next operation before running it, which keeps output readable
    # even when a learner steps through the demo slowly.
    print("Calling send() once, and timing how long it takes to return...")
    # monotonic() measures elapsed duration safely even if the wall clock changes.
    started = time.monotonic()
    # Queue one keyed event. The key keeps this user's events on one partition.
    result = producer.send(DEMO_TOPIC, build_example_event(1), key="u-1001")
    # Convert seconds to milliseconds because this operation should be very fast.
    elapsed_ms = (time.monotonic() - started) * 1000

    # Show timing first, followed by every useful field returned to the caller.
    print(f"  send() returned in {elapsed_ms:.3f} ms")
    print(f"  result.queued       = {result.queued}")
    print(f"  result.topic        = {result.topic}")
    print(f"  result.message_key  = {result.message_key}")
    # Add a blank line before interpreting the raw values.
    print()
    # These three lines make the crucial local-queue versus broker distinction.
    print("Read result.queued carefully. True means the LOCAL QUEUE accepted the")
    print("message. It does NOT prove that the broker stored it. The quick return")
    print("shows that this call did not wait to learn the delivery outcome.")


# ============================================================
# STEP 4: SCENE 2 - THE produce -> poll -> flush LIFECYCLE
# ============================================================


def scene_2_lifecycle(producer: FireAndForgetProducer) -> None:
    """Show the three calls that make up the whole pattern, and their costs.

    The point: sending is instant, and `flush()` is where the waiting
    actually happens, because that is the only call that waits for messages
    to reach an outcome.
    """
    # Mark the start of the lifecycle experiment.
    announce(2, "The produce -> poll -> flush lifecycle")

    # One thousand sends are enough to make timing visible without creating a
    # long-running tutorial on an ordinary laptop.
    message_count = 1_000

    # Stage 2.1: measure only enqueue calls; do not include the final flush yet.
    print(f"1. Queueing {message_count:,} messages with send()...")
    started = time.monotonic()
    # Build and queue one independent example for each loop number.
    for event_number in range(message_count):
        producer.send(DEMO_TOPIC, build_example_event(event_number))
    # Store elapsed time before printing so terminal output is not part of timing.
    queueing_seconds = time.monotonic() - started
    print(f"   done in {queueing_seconds:.3f}s - no waiting for the broker")

    # Stage 2.2: demonstrate a non-blocking event poll. Zero means do not wait.
    print("2. Calling poll(0): handles results already back, returns at once...")
    started = time.monotonic()
    events_handled = producer.poll(0.0)
    poll_seconds = time.monotonic() - started
    print(f"   returned in {poll_seconds:.3f}s, handled {events_handled} event(s)")

    # Stage 2.3: demonstrate the one call that deliberately waits for completion.
    print("3. Calling flush(): waits for queued messages to reach an outcome...")
    started = time.monotonic()
    outstanding = producer.flush()
    flush_seconds = time.monotonic() - started
    print(f"   returned in {flush_seconds:.3f}s, {outstanding} message(s) unresolved")

    # Summarize the measurements in plain language instead of expecting a new
    # learner to infer the lesson from numbers alone.
    print()
    print("Notice where the time went: queueing was nearly free, and flush() is")
    print("the only call that actually waited. That is the shape of every")
    print("fire-and-forget workload: cheap sends, one wait at the end.")


# ============================================================
# STEP 5: SCENE 3 - THE PAYOFF, MEASURED
# ============================================================


def scene_3_throughput(producer: FireAndForgetProducer) -> None:
    """Measure how many messages per second a single thread can send.

    The point: this throughput is the entire reason to accept silent message
    loss. Never waiting for the broker is what makes it possible.
    """
    # Throughput means how many messages a system handles per unit of time.
    announce(3, "The payoff: throughput")

    # Time both queueing and the final flush so the result includes all work.
    print(f"Sending {THROUGHPUT_MESSAGE_COUNT:,} messages from one thread...")
    started = time.monotonic()
    # Use one producer repeatedly; creating a client per message would destroy
    # connection reuse and batching.
    for event_number in range(THROUGHPUT_MESSAGE_COUNT):
        producer.send(DEMO_TOPIC, build_example_event(event_number))
    # Wait for all queued messages to reach success or permanent failure.
    producer.flush()
    elapsed_seconds = time.monotonic() - started

    # Divide completed work by elapsed seconds to calculate messages per second.
    messages_per_second = THROUGHPUT_MESSAGE_COUNT / elapsed_seconds
    # Print raw duration and the easier-to-compare throughput rate.
    print(f"  elapsed            : {elapsed_seconds:.2f}s")
    print(f"  throughput         : {messages_per_second:,.0f} messages/second")
    print()
    print("Tens of thousands per second from a single Python thread, because we")
    print("never pause to wait for the broker. Use this pattern where volume")
    print("matters more than any individual message.")


# ============================================================
# STEP 6: SCENE 4 - THE HONEST WEAKNESS
# ============================================================


def scene_4_silent_failure() -> None:
    """Send to an address where nothing is listening, and observe the silence.

    The point: this is the defining weakness, not a bug. Because no
    `on_delivery=` callback is registered, a guaranteed delivery failure
    produces no per-message delivery error in this application's result.

    This scene builds its own producer, deliberately misconfigured, and does
    not touch the working one used by the other scenes.
    """
    # Tell the learner this scene intentionally creates a failure.
    announce(4, "The honest weakness: failure nobody is told about")

    # Port 9999 is deliberately expected to have no Kafka broker listening.
    doomed_config = build_producer_config(bootstrap_servers=UNREACHABLE_BOOTSTRAP)
    # Shorten the client's total delivery budget so the demonstration finishes
    # in three seconds rather than waiting for the production default.
    doomed_config["delivery.timeout.ms"] = UNREACHABLE_TIMEOUT_MS
    # Silence the client's own connection-error chatter so the point is clear:
    # it is OUR code that learns nothing, even though the library notices.
    doomed_config["log_level"] = 0

    # State the test setup explicitly before producing the doomed message.
    print(f"Pointing a producer at {UNREACHABLE_BOOTSTRAP}, where nothing listens.")
    print("This message cannot possibly be delivered.")

    # The context manager guarantees close(), which performs the final flush.
    with FireAndForgetProducer(config=doomed_config) as doomed_producer:
        # Local queueing still succeeds even though later network delivery fails.
        result = doomed_producer.send(DEMO_TOPIC, {"event_type": "will_never_arrive"})
        # Show that the local result contains no later broker outcome.
        print(f"  send() reported queued = {result.queued}")
        print(f"  send() reported error  = {result.error_message}")
        print()
        print("Waiting out the delivery timeout...")

    # Explain the observed silence after close() has waited out the timeout.
    print()
    print("Our result was never told anything. No exception, no error value - the")
    print("message was simply discarded. That is fire-and-forget BY DEFINITION,")
    print("not a defect: we registered no delivery callback, so there was nothing")
    print("to report the failure to.")
    print()
    print("Need to know about failures? Add the one argument this pattern omits:")
    print("    producer.produce(topic, value=payload, on_delivery=callback)")
    print("which is the callback-confirmed pattern: producers.callback_confirmed")


# ============================================================
# STEP 7: RUN THE TUTORIAL
# ============================================================


def run_demo() -> None:
    """Run all four scenes in order against a live broker.

    Failure behaviour:
        Scenes 1-3 need a reachable broker; if there is none, they report
        queueing failures rather than crashing. Scene 4 needs no broker, as
        being unreachable is its subject. The working producer is always
        closed, even if a scene raises, because of the `finally` block.
    """
    # Configure only warnings and errors from library code. The tutorial's
    # print statements remain the main explanation seen in the terminal.
    logging.basicConfig(
        level=logging.WARNING,  # WARNING: let the scenes' own prints lead
        # Include clock time, severity, logger name, and message in each record.
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        # Keep timestamps compact because this is an interactive tutorial.
        datefmt="%H:%M:%S",
    )

    # Print the environment being used so connection mistakes are diagnosable.
    print("FIRE-AND-FORGET PRODUCER - RUNNABLE TUTORIAL")
    print(f"Broker: {read_bootstrap_servers()}")
    print(f"Topic : {DEMO_TOPIC}")

    # Create one producer and reuse its connections and message batches across
    # the three scenes that need a working broker.
    producer = FireAndForgetProducer()
    # finally guarantees cleanup even if any teaching scene raises an exception.
    try:
        # Run the learning sequence from basic behavior to measured throughput.
        scene_1_send_returns_immediately(producer)
        scene_2_lifecycle(producer)
        scene_3_throughput(producer)
    finally:
        # Always close: anything still queued would otherwise be lost, which
        # is exactly the mistake scene 2 warns about.
        producer.close()

    # Use a separate deliberately broken client for the final failure lesson.
    scene_4_silent_failure()

    # End with a visible completion marker and the pattern's central rule.
    print()
    print("=" * 70)
    print("Tutorial complete.")
    print("=" * 70)
    print()
    print("THE ONE THING TO REMEMBER")
    print("  Fire-and-forget is not a setting. It is the ABSENCE of an")
    print("  on_delivery= callback on produce(). Nothing else distinguishes it.")


# This guard supports ``python demo.py`` while preventing automatic execution
# when tests import one of the scene functions.
if __name__ == "__main__":
    # Run the same orchestrator used by the package's __main__.py entrypoint.
    run_demo()
