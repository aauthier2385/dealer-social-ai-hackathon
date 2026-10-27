"""Small in-memory sliding-window limiter. It protects the Meta credits from runaway use.

The service runs as one process, so in-memory state is enough. The limits reset on restart.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow_all(self, checks: list[tuple[str, int, float]]) -> tuple[bool, int]:
        """All-or-nothing: records a hit on every (key, limit, window) only if all have room.

        Returns (allowed, seconds_until_retry).
        """
        now = self._clock()
        with self._lock:
            if len(self._hits) > 10_000:  # forget idle keys so memory stays bounded
                for stale in [k for k, v in self._hits.items() if not v or v[-1] <= now - 86400]:
                    del self._hits[stale]
            for key, limit, window in checks:
                hits = self._hits.setdefault(key, deque())
                while hits and hits[0] <= now - window:
                    hits.popleft()
                if len(hits) >= limit:
                    return False, max(1, math.ceil(hits[0] + window - now))
            for key, _, _ in checks:
                self._hits[key].append(now)
            return True, 0
