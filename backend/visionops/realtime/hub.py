"""WebSocket fan-out for live pipeline state.

Design note (``docs/ARCHITECTURE.md`` §3.3): each topic holds a **single latest
value**. A client that cannot keep up misses intermediate *states* — which are
not durable information — rather than accumulating memory or latency. Events are
never dropped: they are durable in the database and the client re-syncs over
REST.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..observability.metrics import (
    METRIC_WS_CONNECTIONS,
    METRIC_WS_DROPPED,
    METRIC_WS_SENT,
    registry,
)

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 20.0
STALE_AFTER_SECONDS = 3.0


@dataclass(slots=True, eq=False)
class Subscription:
    """One connected client on one topic.

    ``eq=False`` keeps the default identity-based ``__eq__``/``__hash__``. A
    dataclass would otherwise synthesise ``__eq__`` and set ``__hash__ = None``,
    making instances unhashable — and these live in a ``set``, so every
    disconnect would raise and leak the subscriber.
    """

    topic: str
    send: Callable[[dict[str, Any]], Any]
    queue_size: int = 2
    _latest: dict[str, Any] | None = field(default=None, init=False)
    _wakeup: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _closed: bool = field(default=False, init=False)
    dropped: int = 0

    def offer(self, payload: dict[str, Any]) -> None:
        """Replace the pending payload. Never blocks; never queues deeply."""
        if self._closed:
            return
        if self._latest is not None:
            self.dropped += 1
            registry.counter(METRIC_WS_DROPPED, 1, reason="slow_client")
        self._latest = payload
        self._wakeup.set()

    async def run(self) -> None:
        """Drain the subscription until it is closed."""
        try:
            while not self._closed:
                if not self._wakeup.is_set():
                    try:
                        await asyncio.wait_for(self._wakeup.wait(), timeout=HEARTBEAT_SECONDS)
                    except TimeoutError:
                        await self.send({"type": "ping", "server_time": time.time()})
                        continue
                self._wakeup.clear()
                payload = self._latest
                self._latest = None
                if payload is None:
                    continue
                try:
                    await self.send(payload)
                    registry.counter(METRIC_WS_SENT, 1, type=str(payload.get("type", "unknown")))
                except Exception:
                    logger.debug("subscriber send failed on topic %s", self.topic)
                    break
        finally:
            self._closed = True

    def close(self) -> None:
        self._closed = True
        self._wakeup.set()


class RealtimeHub:
    """Topic-based fan-out with a bounded per-subscriber buffer."""

    def __init__(self) -> None:
        self._topics: dict[str, set[Subscription]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    # -- subscriptions ------------------------------------------------------

    async def subscribe(
        self, topic: str, send: Callable[[dict[str, Any]], Any], *, queue_size: int = 2
    ) -> Subscription:
        subscription = Subscription(topic=topic, send=send, queue_size=queue_size)
        async with self._lock:
            self._topics.setdefault(topic, set()).add(subscription)
        task = asyncio.create_task(subscription.run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        registry.gauge(METRIC_WS_CONNECTIONS, self.subscriber_count)
        return subscription

    async def unsubscribe(self, subscription: Subscription) -> None:
        subscription.close()
        async with self._lock:
            subscribers = self._topics.get(subscription.topic)
            if subscribers is not None:
                subscribers.discard(subscription)
                if not subscribers:
                    del self._topics[subscription.topic]
        registry.gauge(METRIC_WS_CONNECTIONS, self.subscriber_count)

    # -- publishing ---------------------------------------------------------

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Push a state payload to every subscriber of ``topic``.

        Called from the pipeline's worker thread, so it must stay non-blocking.
        """
        subscribers = self._topics.get(topic)
        if not subscribers:
            return
        for subscription in list(subscribers):
            subscription.offer(payload)

    def publish_event(self, topic: str, payload: dict[str, Any]) -> None:
        self.publish(topic, payload)

    # -- introspection ------------------------------------------------------

    @property
    def subscriber_count(self) -> int:
        return sum(len(s) for s in self._topics.values())

    def topics(self) -> list[str]:
        return sorted(self._topics)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        async with self._lock:
            self._topics.clear()


_hub: RealtimeHub | None = None


def get_hub() -> RealtimeHub:
    global _hub
    if _hub is None:
        _hub = RealtimeHub()
    return _hub


def reset_hub() -> None:
    """Drop the process-wide hub (tests)."""
    global _hub
    _hub = None


def session_topic(session_id: str) -> str:
    return f"session:{session_id}"


__all__ = [
    "HEARTBEAT_SECONDS",
    "STALE_AFTER_SECONDS",
    "RealtimeHub",
    "Subscription",
    "get_hub",
    "reset_hub",
    "session_topic",
]
