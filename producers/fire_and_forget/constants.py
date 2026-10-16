"""
Tunable defaults for the fire-and-forget producer.

WHY A SEPARATE MODULE
---------------------
Numbers scattered through code ("magic numbers") are impossible to review:
a reader cannot tell whether `0.0` was deliberate or accidental. Collecting
them here, each with an explanation of what it controls and when to change it,
makes every tuning decision visible in one place.

Each constant below documents three things:
  1. WHAT IT CONTROLS  - the behaviour it changes
  2. WHY THIS DEFAULT  - the reasoning behind the chosen value
  3. WHEN TO TUNE IT   - the production situation that justifies changing it

Plain-language terms used below:
  poll          ask the client to run ready callbacks and housekeeping events
  flush         wait for currently queued work to finish or time out
  back-pressure the client saying "I am full; slow down"
  latency       how long one operation takes
  batch job     a finite job that processes many records and then exits
  orchestrator  software such as Kubernetes that starts and stops services
"""

# ============================================================
# STEP 1: HOW LONG poll() MAY BLOCK
# ============================================================

# WHAT IT CONTROLS
#   The `timeout` argument passed to the underlying producer's poll().
#   poll() runs callbacks and other client events that are ready. This package
#   does not register per-message delivery callbacks, but custom configuration
#   may register statistics, errors, logs, or other event callbacks.
#
# WHY THIS DEFAULT (0.0)
#   0.0 means "handle whatever results are ready this instant, then return
#   immediately; do not wait". That is the correct choice inside a hot send
#   loop: fire-and-forget exists to avoid blocking the caller, so blocking in
#   poll() would defeat the entire point of the pattern.
#
# WHEN TO TUNE IT
#   Raise it (e.g. 0.5) only when you are deliberately waiting for capacity
#   after the local queue reported itself full. See core.FireAndForgetProducer
#   for where that happens.
#
# SAFE RANGE
#   0.0 to 1.0 seconds. Larger values make a send loop feel unresponsive.
DEFAULT_POLL_TIMEOUT_SECONDS: float = 0.0


# ============================================================
# STEP 2: HOW LONG flush() WAITS BEFORE GIVING UP
# ============================================================

# WHAT IT CONTROLS
#   The maximum time flush() waits for queued messages to reach a final
#   outcome (delivered or failed) before returning anyway.
#
# WHY THIS DEFAULT (30.0)
#   Long enough to absorb a brief broker hiccup or a slow network, short
#   enough that a stuck broker cannot hang a batch job indefinitely. An
#   unbounded wait is never correct: it converts a Kafka outage into a hung
#   process with no diagnostics.
#
# WHEN TO TUNE IT
#   Raise it for large batches where losing the tail of the batch is worse
#   than waiting. Lower it for latency-sensitive request paths that must fail
#   fast and shed load instead of waiting.
#
# SAFE RANGE
#   1.0 to 120.0 seconds.
DEFAULT_FLUSH_TIMEOUT_SECONDS: float = 30.0


# ============================================================
# STEP 3: HOW LONG TO WAIT AT SHUTDOWN
# ============================================================

# WHAT IT CONTROLS
#   The flush timeout used specifically when closing the producer.
#
# WHY THIS DEFAULT (25.0)
#   Shutdown is the last chance for any queued message to be delivered.
#   Twenty-five seconds fits inside Kubernetes' common 30-second termination
#   grace period and leaves time for the rest of application cleanup.
#
# WHEN TO TUNE IT
#   Keep it below your orchestrator's termination grace period. A service with
#   a shorter grace period must lower this value; a batch worker may raise it.
#
# SAFE RANGE
#   5.0 to 300.0 seconds, but never above your platform's kill timeout.
SHUTDOWN_FLUSH_TIMEOUT_SECONDS: float = 25.0


# ============================================================
# STEP 4: HOW LONG TO WAIT FOR LOCAL QUEUE CAPACITY
# ============================================================

# WHAT IT CONTROLS
#   When the local queue is full, the client raises BufferError. Before giving
#   up on that message, send() serves pending client events and retries for up
#   to this long, as recommended by the confluent-kafka API.
#
# WHY THIS DEFAULT (2.0)
#   Long enough to ride out a brief burst where the application briefly
#   outruns the network, short enough that a genuinely stuck broker cannot
#   stall the caller. The wait is bounded on purpose: an unbounded retry loop
#   would convert back-pressure into a hang with no diagnostics.
#
# WHEN TO TUNE IT
#   Lower it (even to 0.0) in latency-critical paths that must shed load
#   immediately rather than wait. Raise it for batch jobs where dropping a
#   message is worse than pausing. It is also a constructor argument on
#   FireAndForgetProducer, so it can be set per instance and in tests.
#
# SAFE RANGE
#   0.0 to 30.0 seconds.
DEFAULT_QUEUE_CAPACITY_WAIT_SECONDS: float = 2.0


# ============================================================
# STEP 5: HOW LONG EACH BACK-PRESSURE POLL MAY WAIT
# ============================================================

# Polling in short slices lets the Kafka background thread make progress while
# keeping the caller responsive. The final slice is shortened when less than
# this interval remains in the total queue-capacity budget.
QUEUE_CAPACITY_POLL_INTERVAL_SECONDS: float = 0.1
