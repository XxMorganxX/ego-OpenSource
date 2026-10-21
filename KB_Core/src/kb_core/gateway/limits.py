"""Per-tenant token bucket for the write endpoints."""
from __future__ import annotations

import time


class TokenBucket:
    def __init__(self, capacity: int, period_s: float, clock=time.monotonic) -> None:
        self.capacity = capacity
        self.rate = capacity / period_s
        self.tokens = float(capacity)
        self._clock = clock
        self._last = clock()

    def take(self, n: int = 1) -> bool:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
        self._last = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False
