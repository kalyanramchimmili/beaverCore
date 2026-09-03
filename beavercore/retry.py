"""Retry policy for :class:`beavercore.Client` — exponential backoff with jitter."""

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """Immutable retry configuration.

    :param max_attempts: total attempts including the first.
    :param backoff_base: seconds — multiplier for ``2 ** attempt``.
    :param backoff_cap: seconds — upper bound on any single sleep.
    :param jitter: 0 = deterministic backoff, 1 = up to a full ``backoff_base``
        of extra jitter on top of the exponential term.
    """

    max_attempts: int = 3
    backoff_base: float = 0.5
    backoff_cap: float = 30.0
    jitter: float = 0.5

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.backoff_base < 0 or self.backoff_cap < 0 or self.jitter < 0:
            raise ValueError("backoff_base, backoff_cap, jitter must be >= 0")

    def delay_for(self, attempt: int) -> float:
        base = min(self.backoff_base * (2**attempt), self.backoff_cap)
        return base + random.uniform(0, self.jitter * self.backoff_base)
