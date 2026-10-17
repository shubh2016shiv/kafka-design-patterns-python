"""
Content-Based Router — tunable default values (one place to find and change them).

What this file is for:
    Every "magic number" and fixed string the router uses lives here, with a
    short note on what it controls and when you might change it. Keeping them in
    one file means an operator can tune behavior without reading the logic code.

Plain-language terms used below:
    topic        a named stream of messages inside Kafka (like a mailbox name)
    prefix       text we put in front of every topic name so related topics
                 group together, e.g. "ecommerce.orders", "ecommerce.payments"
    local queue  memory inside the producer that briefly holds messages that
                 have not been sent to Kafka yet
    flush        wait for the producer to finish sending everything it is holding
    header       a small label attached to a message that consumers can read
                 without opening the full message body
"""

# ============================================================================
# STEP 1: Topic naming
# ============================================================================

# We never let the outside world hand us a raw topic name. Instead we build the
# name ourselves from this prefix plus a fixed suffix (".orders", ".payments").
# A single, known prefix keeps all of this app's topics grouped and prevents
# accidental writes to unrelated topics.
#
# Change this when: you run more than one app on the same Kafka cluster and want
# each app's topics kept separate (e.g. "billing", "shipping").
DEFAULT_TOPIC_PREFIX: str = "ecommerce"


# ============================================================================
# STEP 2: Waiting limits (so the app never hangs forever)
# ============================================================================

# When the producer's local queue is temporarily full, publishing one message
# can fail with a "queue full" signal. We retry for at most this many seconds,
# then give up with a clear timeout error instead of looping forever.
#
# Change this when: a slow or overloaded broker needs more breathing room (raise
# it), or a web request must fail fast and shed load (lower it).
DEFAULT_QUEUE_WAIT_SECONDS: float = 3.0

# While we wait for the full queue to drain, we ask the producer to do a little
# work in short slices of this length. Short slices keep the app responsive and
# let the overall deadline above stay in charge of when we finally give up.
QUEUE_FULL_POLL_INTERVAL_SECONDS: float = 0.2

# At shutdown the application asks the producer to send everything it still
# holds. This is the longest we are willing to wait for that final drain.
# 30 seconds matches the Kafka client's own default request timeout.
#
# Change this when: integration tests want a short wait (e.g. 5 seconds), or a
# busy production service needs more headroom for a transient slowdown.
DEFAULT_FLUSH_TIMEOUT_SECONDS: float = 30.0


# ============================================================================
# STEP 3: The business rules the router understands
# ============================================================================

# The only event types this router knows how to route. Anything else is
# rejected on purpose so a typo or a brand-new event never slips through to a
# random topic. Add a new type here (and a matching rule in routing.py) when the
# business genuinely needs it.
SUPPORTED_EVENT_TYPES: tuple[str, ...] = (
    "order.created",
    "payment.updated",
    "inventory.changed",
)

# A payment event must carry one of these statuses. Any other value is a
# validation error, not something we quietly route somewhere.
VALID_PAYMENT_STATUSES: frozenset[str] = frozenset({"PENDING", "SUCCEEDED", "FAILED"})


# ============================================================================
# STEP 4: Message header labels
# ============================================================================

# We stamp these two labels onto every published message. Consumers and tracing
# tools can read them without decoding the whole message body, which makes it
# easy to follow one event across systems or spot a mis-routed message.
HEADER_KEY_EVENT_ID: str = "event_id"
HEADER_KEY_EVENT_TYPE: str = "event_type"
