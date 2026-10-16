"""
Public API for the `producers.fire_and_forget` package.

WHAT THIS PACKAGE IS
--------------------
A focused, teaching-quality implementation of one Kafka delivery mode:
fire-and-forget - send the message and never find out whether it arrived.

Start with `core.py`: its module docstring explains the pattern, including the
single line of code that establishes it.

MODULE MAP (read in this order)
-------------------------------
    constants.py  tunable defaults, each with why-this-value and when-to-tune
    types.py      the contracts: what a producer must do, what a send reports
    config.py     producer configuration, every key explained
    clients.py    the one place the real Kafka library is touched
    core.py       the pattern itself: FireAndForgetProducer
    demo.py       runnable tutorial: `python -m producers.fire_and_forget`

QUICK START
-----------
    from producers.fire_and_forget import FireAndForgetProducer

    with FireAndForgetProducer() as producer:
        producer.send("page-views", {"user_id": "u-1", "page": "/home"})

RELATED PACKAGES
----------------
    producers.callback_confirmed  same send, plus a delivery callback, so
                                  failures become visible instead of silent
    producers.singleton           a process-wide shared producer, with
                                  confirmation mode, local retry and health
                                  metrics. Those four concerns used to live
                                  in this package and made it hard to learn
                                  from; they belong to producer lifecycle and
                                  operations, not to a delivery mode.
"""

# Import the real-client factory so applications can build the Kafka adapter
# without needing to know which internal module owns that integration.
from .clients import default_producer_factory

# Import the two configuration helpers that callers use at application startup.
from .config import build_producer_config, read_bootstrap_servers

# Import the documented timing defaults so an application can reuse or override
# the same values without copying unexplained numbers into its own code.
from .constants import (
    DEFAULT_FLUSH_TIMEOUT_SECONDS,
    DEFAULT_POLL_TIMEOUT_SECONDS,
    DEFAULT_QUEUE_CAPACITY_WAIT_SECONDS,
    QUEUE_CAPACITY_POLL_INTERVAL_SECONDS,
    SHUTDOWN_FLUSH_TIMEOUT_SECONDS,
)

# Import the main producer class; this is the package's primary public feature.
from .core import FireAndForgetProducer

# Import payload types, dependency-injection contracts, and send result models
# so callers can type their integration without importing private modules.
from .types import (
    JsonObject,
    JsonValue,
    KafkaProducerLike,
    ProducerFactory,
    SendFailureReason,
    SendResult,
)

# ``__all__`` is the official public API. Tools and readers can use this list to
# distinguish supported names from internal implementation details.
__all__ = [
    # The pattern
    "FireAndForgetProducer",  # Queues messages without delivery callbacks.
    # Results and contracts
    "SendResult",  # Reports whether the local queue accepted one message.
    "SendFailureReason",  # Gives code a stable local rejection category.
    "KafkaProducerLike",  # Describes the three client methods core.py needs.
    "ProducerFactory",  # Describes a function that builds a producer client.
    "JsonObject",  # Describes a JSON object accepted by send().
    "JsonValue",  # Describes any value that JSON can represent.
    # Configuration
    "build_producer_config",  # Creates reviewed Kafka defaults.
    "read_bootstrap_servers",  # Reads broker addresses from the environment.
    # Client construction
    "default_producer_factory",  # Creates the real confluent-kafka client.
    # Tunable defaults
    "DEFAULT_FLUSH_TIMEOUT_SECONDS",  # Normal explicit flush wait limit.
    "DEFAULT_POLL_TIMEOUT_SECONDS",  # Normal event-poll wait limit.
    "DEFAULT_QUEUE_CAPACITY_WAIT_SECONDS",  # Total queue-full wait limit.
    "QUEUE_CAPACITY_POLL_INTERVAL_SECONDS",  # One queue-full poll slice.
    "SHUTDOWN_FLUSH_TIMEOUT_SECONDS",  # Final shutdown flush wait limit.
]
