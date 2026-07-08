"""A tiny async pub/sub bus.

Producers call `emit(...)` synchronously (safe from sync hook callbacks and the
state store). Consumers — the REPL printer now, the dashboard WebSocket later —
`subscribe()` to get an asyncio.Queue and await events off it.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any


@dataclasses.dataclass
class Event:
    type: str
    data: dict[str, Any]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    def emit(self, event_type: str, **data: Any) -> None:
        """Fan an event out to every subscriber. Non-blocking; never raises."""
        event = Event(event_type, data)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # unbounded queues, but stay defensive
                pass
