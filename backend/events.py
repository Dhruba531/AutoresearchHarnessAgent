"""In-process pub/sub that feeds the live-log and search-trace WebSockets.

Background tasks publish to a topic ("run:12", "campaign:3"); each connected
socket holds a queue. Slow consumers drop frames instead of blocking the
publisher — the database keeps the full record either way.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class Hub:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, topic: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs[topic].add(q)
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue) -> None:
        self._subs[topic].discard(q)
        if not self._subs[topic]:
            self._subs.pop(topic, None)

    def publish(self, topic: str, event: dict[str, Any]) -> None:
        for q in list(self._subs.get(topic, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


hub = Hub()

# Background tasks by key, so cancel/stop can reach them.
tasks: dict[str, asyncio.Task] = {}


def spawn(key: str, coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    tasks[key] = task
    task.add_done_callback(lambda _t: tasks.pop(key, None))
    return task
