"""In-process event bus for plugins (README §5.1 EventsAPI).

Publishing never waits for subscribers: each subscriber has a bounded queue
(1000 events, oldest dropped when full) drained by its own worker task, so a
slow or stuck plugin cannot hold up the code that published the event (the
database layer publishes ``message.created`` inside ``add_message``).

Subscriptions are typed and owned (``subscribe_to(event_type, handler,
owner=plugin_id)``); the plugin host removes a plugin's subscriptions when
it unloads.

Event types: ``message.created``, ``session.created``, ``session.deleted``.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field


logger = logging.getLogger("augentia.plugin_events")

QUEUE_SIZE = 1000
EVENT_TYPES = ("message.created", "session.created", "session.deleted")


class PluginEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "augentia"
    data: dict[str, Any]


Handler = Callable[[PluginEvent], Awaitable[None] | None]


class _Subscription:
    def __init__(self, handler: Handler, event_type: str, owner: str) -> None:
        self.handler = handler
        self.event_type = event_type
        self.owner = owner
        self.queue: asyncio.Queue[PluginEvent] | None = None
        self.worker: asyncio.Task | None = None
        self.dropped = 0
        self.closed = False

    def wants(self, event: PluginEvent) -> bool:
        return self.event_type == event.type

    def put(self, event: PluginEvent) -> None:
        loop = asyncio.get_running_loop()
        if self.worker is None or self.worker.done() or self.worker.get_loop() is not loop:
            # First event, or the previous loop is gone (tests run several loops).
            self.queue = asyncio.Queue(maxsize=QUEUE_SIZE)
            self.worker = loop.create_task(self._drain(), name=f"events:{self.owner}")
        assert self.queue is not None
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
            logger.warning("event queue full for %s; dropped the oldest event (%d so far)",
                           self.owner, self.dropped)
        self.queue.put_nowait(event)

    async def _drain(self) -> None:
        assert self.queue is not None
        queue = self.queue
        while not self.closed:
            event = await queue.get()
            try:
                result = self.handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("plugin event subscriber %s failed event=%s", self.owner, event.type)

    def close(self) -> None:
        self.closed = True
        if self.worker is not None and not self.worker.done():
            self.worker.cancel()


class PluginEventBus:
    def __init__(self) -> None:
        self._subscriptions: list[_Subscription] = []

    def subscribe_to(self, event_type: str, handler: Handler, *, owner: str) -> Callable[[], None]:
        """Receive events of one type; returns an unsubscribe function."""
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event_type!r}; known: {', '.join(EVENT_TYPES)}")
        sub = _Subscription(handler, event_type, owner)
        self._subscriptions.append(sub)

        def unsubscribe() -> None:
            if sub in self._subscriptions:
                self._subscriptions.remove(sub)
            sub.close()

        return unsubscribe

    def unsubscribe_owner(self, owner: str) -> None:
        for sub in [s for s in self._subscriptions if s.owner == owner]:
            self._subscriptions.remove(sub)
            sub.close()

    async def publish(self, event: PluginEvent) -> None:
        """Queue the event for every interested subscriber; returns immediately."""
        for sub in list(self._subscriptions):
            if sub.wants(event):
                try:
                    sub.put(event)
                except RuntimeError:
                    # No running loop (should not happen in the app).
                    logger.exception("could not queue event %s", event.type)

    async def drain(self, timeout: float = 2.0) -> None:
        """Wait until every queue is empty (tests)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if all(s.queue is None or s.queue.empty() for s in self._subscriptions):
                await asyncio.sleep(0.01)  # let the last handler finish
                return
            await asyncio.sleep(0.01)


_event_bus = PluginEventBus()


def get_plugin_event_bus() -> PluginEventBus:
    return _event_bus
