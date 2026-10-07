"""In-process token-bucket rate limiting.

Adequate for a single node. Phase 6 moves the buckets to Redis so limits hold
across processes; the interface stays the same.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


#: How often the idle sweep runs, and how long a bucket may sit untouched
#: before it is forgotten. Generous enough that a normal client never notices
#: its bucket being recreated (which would hand it a fresh burst allowance).
PRUNE_INTERVAL_SECONDS = 60.0
IDLE_BUCKET_SECONDS = 3600.0


class RateLimiter:
    """Token bucket keyed by an arbitrary string (an API-key fingerprint).

    Refill is continuous (not per-window), so a client is rewarded for steady
    traffic and penalised for bursts beyond ``burst`` times the rate.
    """

    PRUNE_INTERVAL_SECONDS = PRUNE_INTERVAL_SECONDS
    IDLE_BUCKET_SECONDS = IDLE_BUCKET_SECONDS

    def __init__(self, rate_per_second: float, burst: float | None = None) -> None:
        self.rate = max(0.001, rate_per_second)
        self.burst = burst if burst is not None else self.rate * 3.0
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._last_prune = time.monotonic()

    def allow(self, key: str, cost: float = 1.0) -> bool:
        """Consume ``cost`` tokens. Returns ``False`` when the limit is hit."""
        now = time.monotonic()
        with self._lock:
            self._prune_if_due(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.burst, updated_at=now)
                self._buckets[key] = bucket
            else:
                elapsed = now - bucket.updated_at
                if elapsed > 0:
                    bucket.tokens = min(self.burst, bucket.tokens + elapsed * self.rate)
                bucket.updated_at = now
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True
            return False

    def _prune_if_due(self, now: float) -> None:
        """Sweep idle buckets at most once a minute, on the request path.

        Buckets are created for *every* key the limiter is shown, including
        garbage from anonymous callers, so without this the map is an unbounded
        allocation an unauthenticated client can drive. Caller holds the lock.
        """
        if now - self._last_prune < self.PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        cutoff = now - self.IDLE_BUCKET_SECONDS
        for key in [k for k, b in self._buckets.items() if b.updated_at < cutoff]:
            del self._buckets[key]

    def retry_after(self, key: str) -> int:
        """Seconds until one token is available for ``key`` (at least 1).

        Applies the same refill ``allow`` would, measured from the bucket's last
        update: a client that backed off for a while must not be told to wait
        longer than necessary.
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return 1
            elapsed = max(0.0, now - bucket.updated_at)
            tokens = min(self.burst, bucket.tokens + elapsed * self.rate)
            deficit = 1.0 - tokens
            if deficit <= 0:
                return 1
            return max(1, int(deficit / self.rate + 0.999))

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    def prune(self, max_age_seconds: float = 3600.0) -> None:
        """Drop buckets that have been idle long enough to be irrelevant."""
        cutoff = time.monotonic() - max_age_seconds
        with self._lock:
            for key in [k for k, b in self._buckets.items() if b.updated_at < cutoff]:
                del self._buckets[key]


__all__ = ["IDLE_BUCKET_SECONDS", "PRUNE_INTERVAL_SECONDS", "RateLimiter"]
