"""Time-ordered UUIDs (UUIDv7 layout) for every domain identifier.

Why not auto-increment and not UUIDv4:

* auto-increment cannot be generated independently on two nodes, and leaks volume
* UUIDv4 sorts randomly, which fragments indexes at scale
* UUIDv7 sorts by time, so index insertion order matches time order, and two
  nodes generating ids never collide — which is what makes future edge→cloud
  event sync idempotent for free (ADR-024).

Layout: 48 bits of Unix milliseconds, version nibble 7, 74 random bits.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable

_last_ms = -1
_sequence = 0
_sequence_lock_env = os.environ.get("VISIONOPS_ID_FACTORY_DEFAULT", "")


def new_id() -> str:
    """Generate a time-ordered UUIDv7 as a string."""
    global _last_ms, _sequence

    now_ms = time.time_ns() // 1_000_000
    if now_ms == _last_ms:
        _sequence += 1
        # Stay inside the 12-bit random field: beyond 4096 ids in one
        # millisecond we fall through to a fresh millisecond timestamp.
        if _sequence > 0xFFF:
            now_ms += 1
            _last_ms = now_ms
            _sequence = 0
    elif now_ms < _last_ms:
        # Clock went backwards: keep ids monotonic anyway.
        now_ms = _last_ms
        _sequence += 1
    else:
        _last_ms = now_ms
        _sequence = 0

    rand = int.from_bytes(os.urandom(8), "big")
    value = (now_ms & 0xFFFF_FFFF_FFFF) << 80
    value |= 0x7 << 76
    value |= (_sequence & 0xFFF) << 64
    value |= 0x2 << 62
    value |= rand & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


def is_time_ordered(value: str) -> bool:
    """Whether a UUID string uses the version-7 layout."""
    try:
        return uuid.UUID(value).version == 7
    except (ValueError, AttributeError):
        return False


def timestamp_ms(value: str) -> int:
    """Extract the embedded millisecond timestamp from a version-7 UUID."""
    parsed = uuid.UUID(value)
    if parsed.version != 7:
        raise ValueError(f"{value} is not a version-7 UUID")
    return parsed.int >> 80


# -- test injection ---------------------------------------------------------

_id_factory: Callable[[], str] = new_id


def set_id_factory(factory: Callable[[], str] | None) -> None:
    """Replace the id generator (tests use this for deterministic assertions)."""
    global _id_factory
    _id_factory = factory if factory is not None else new_id


def get_id_factory() -> Callable[[], str]:
    return _id_factory


def new_id_or(value: str | None) -> str:
    """Return ``value`` when set, else a fresh id (lets callers pre-assign ids)."""
    return value or _id_factory()


def reset_ids() -> None:
    """Restore the default factory and clear monotonicity state (tests only)."""
    global _id_factory, _last_ms, _sequence
    _id_factory = new_id
    _last_ms = -1
    _sequence = 0
    _ = _sequence_lock_env


__all__ = [
    "get_id_factory",
    "is_time_ordered",
    "new_id",
    "new_id_or",
    "reset_ids",
    "set_id_factory",
    "timestamp_ms",
]
