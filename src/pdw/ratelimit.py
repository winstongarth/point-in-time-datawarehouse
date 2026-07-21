from __future__ import annotations

import time


class TokenBucket:
    """A blocking token-bucket rate limiter.

    EDGAR caps requests at 10/s; this is what keeps an
    adapter under that limit regardless of how fast its caller loops,
    without hardcoding the limit into the adapter itself.
    """

    def __init__(self, rate_per_second: float) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._rate = rate_per_second
        self._capacity = max(1.0, rate_per_second)
        self._tokens = self._capacity
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def acquire(self) -> None:
        """Block until a token is available, then consume it."""
        while True:
            self._refill()
            if self._tokens >= 1:
                self._tokens -= 1
                return
            time.sleep((1 - self._tokens) / self._rate)
