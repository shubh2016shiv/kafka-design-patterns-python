"""The final report returned by CallbackConfirmedProducer.finish()."""

# dataclass writes the boring parts of a small "bag of values" class for us.
from dataclasses import dataclass


# frozen=True: the report cannot be edited after it is created, so nobody can
# accidentally change the numbers after they were written.
@dataclass(frozen=True)
class DeliverySummary:
    """Final counts for one producer. Read `all_confirmed` for the verdict."""

    # How many events were accepted into the local waiting list.
    queued_count: int
    # How many of them Kafka confirmed it stored ("good news" phone calls).
    delivered_count: int
    # How many of them Kafka finally gave up on ("bad news" phone calls).
    failed_count: int
    # Queued, but no phone call at all before time ran out. Result UNKNOWN, not success.
    awaiting_callback_count: int
    # How many messages the client said were still unfinished when waiting stopped.
    unfinished_message_count: int
    # (event_id, reason) for the latest failures. Never holds the message contents.
    recent_delivery_failures: tuple = ()

    @property
    def all_confirmed(self) -> bool:
        """True only if nothing failed and nothing is still unresolved.

        Do NOT use "unfinished_message_count == 0" alone as a success test: a failed
        message also leaves the waiting list, so that number can be 0 after failures.
        """
        # All three must be zero. Any failure, or any unknown, means "not confirmed".
        return (
            self.failed_count == 0
            and self.awaiting_callback_count == 0
            and self.unfinished_message_count == 0
        )
