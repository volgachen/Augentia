import asyncio
import json
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import AsyncIterator

from app.adapters.base import BaseAgentAdapter, StreamEvent, StreamEventType
from app.db.interface import IAgentDatabase
from app.models.domain import MessageRole, SessionStatus

logger = logging.getLogger("augentia.runner")

_SUBSCRIBER_QUEUE_MAX = 256


@dataclass
class _InboxItem:
    content: str
    from_session_id: str | None = None


class SessionRunner:
    """Owns a single adapter and serializes turns through it.

    The adapter contract is single-consumer (`send` then iterate `stream`),
    so we can't let WS handlers and HTTP handlers both drive it. The runner
    is the one consumer; everyone else is a producer (`submit`) or a
    subscriber (`subscribe`). This is what makes HTTP-initiated turns
    visible to dashboard WS clients.
    """

    def __init__(
        self,
        session_id: str,
        adapter: BaseAgentAdapter,
        db: IAgentDatabase,
    ) -> None:
        self._session_id = session_id
        self._adapter = adapter
        self._db = db
        self._inbox: asyncio.Queue[_InboxItem] = asyncio.Queue()
        self._subscribers: set[asyncio.Queue[StreamEvent | None]] = set()
        self._task: asyncio.Task | None = None
        self._turn_task: asyncio.Task | None = None
        # TOOL_CONFIRM events are broadcast-only (not persisted), so a client
        # that connects/reconnects while a tool is awaiting approval would never
        # see the confirm panel. Cache the currently-pending confirm events here
        # (keyed by call_id) and replay them to each new subscriber; cleared once
        # the human decides or the turn moves past the gate.
        self._pending_confirms: dict[str, StreamEvent] = {}

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name=f"runner:{self._session_id}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("runner task crashed during stop session=%s", self._session_id)
            self._task = None

        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._subscribers.clear()
        self._pending_confirms.clear()

        try:
            await self._adapter.stop()
        except Exception:
            logger.exception("adapter.stop raised session=%s", self._session_id)

    async def submit(self, content: str, from_session_id: str | None = None) -> None:
        await self._inbox.put(_InboxItem(content=content, from_session_id=from_session_id))

    async def cancel_turn(self) -> None:
        turn_task = self._turn_task
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
            with suppress(asyncio.CancelledError):
                await turn_task
        self._pending_confirms.clear()

    async def resolve_decision(self, call_id: str, approved: bool, supplementary_msg: str = "") -> None:
        # Routed from a WS client's approve/deny frame. This resolves the adapter's
        # Future first. If a supplementary message is provided, we queue it as a
        # follow-up user message after the tool result. If denied without a message,
        # we signal to skip the next LLM call.
        self._pending_confirms.pop(call_id, None)
        await self._adapter.resolve_decision(call_id, approved, supplementary_msg)

    async def reload_config(self, config: dict) -> None:
        await self._adapter.reload_config(config)

    def current_system_prompt(self) -> str | None:
        return self._adapter.current_system_prompt()

    def _remember_confirm(self, event: StreamEvent) -> None:
        # event.data is JSON: {"call_id", "name", "args"}. Key the cache by
        # call_id so a resolved/expired confirm can be dropped individually.
        try:
            call_id = json.loads(event.data).get("call_id")
        except (ValueError, TypeError):
            call_id = None
        if call_id:
            self._pending_confirms[call_id] = event

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[StreamEvent]]:
        q: asyncio.Queue[StreamEvent | None] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        self._subscribers.add(q)
        # Replay any pending confirm so a (re)connecting client can render the
        # approval panel immediately instead of only seeing WAITING_CONFIRM.
        for ev in self._pending_confirms.values():
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass
        try:
            yield self._iter(q)
        finally:
            self._subscribers.discard(q)

    async def _iter(self, q: asyncio.Queue[StreamEvent | None]) -> AsyncIterator[StreamEvent]:
        while True:
            ev = await q.get()
            if ev is None:
                return
            yield ev

    def _broadcast(self, event: StreamEvent) -> None:
        # TOOL_MESSAGE is an internal persistence event for OpenAI-native tool
        # result messages. ASSISTANT_MESSAGE is now also sent to UI subscribers so
        # the frontend receives content and tool_calls as one assistant payload,
        # matching restored websocket history after a backend restart.
        if event.type == StreamEventType.TOOL_MESSAGE:
            return
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "subscriber queue full session=%s dropping %s",
                    self._session_id, event.type,
                )

    async def _loop(self) -> None:
        while True:
            item = await self._inbox.get()
            await self._db.update_session_status(self._session_id, SessionStatus.RUNNING)
            self._turn_task = asyncio.create_task(
                self._run_turn(item), name=f"turn:{self._session_id}"
            )
            try:
                await self._turn_task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                await self._db.update_session_status(
                    self._session_id, SessionStatus.WAITING_USER
                )
                self._broadcast(StreamEvent(type=StreamEventType.DONE, data="cancelled"))
            except Exception as e:
                logger.exception("runner turn crashed session=%s", self._session_id)
                self._broadcast(StreamEvent(type=StreamEventType.ERROR, data=str(e)))
                try:
                    await self._db.update_session_status(self._session_id, SessionStatus.ERROR)
                except Exception:
                    logger.exception("failed to mark session ERROR session=%s", self._session_id)
            finally:
                self._turn_task = None

    async def _run_turn(self, item: _InboxItem) -> None:
        # Persist the raw content + sender separately; the agent stdin gets a
        # prefixed view so it can route its reply.
        user_message = await self._db.add_message(
            self._session_id,
            MessageRole.USER,
            item.content,
            from_session_id=item.from_session_id,
        )
        delivered = (
            f"[from-session:{item.from_session_id}] {item.content}"
            if item.from_session_id
            else item.content
        )
        self._broadcast(StreamEvent(type=StreamEventType.USER, data=delivered, message_id=user_message.id))

        agent_buf: list[str] = []
        saw_native_messages = False
        errored = False
        awaiting_confirm = False
        await self._adapter.send(delivered)
        async for event in self._adapter.stream():
            should_broadcast = True
            # Reflect a pending confirm in the session status so the
            # dashboard shows "waiting for approval"; flip back to RUNNING
            # once the gate clears (the tool result or next event arrives).
            if event.type == StreamEventType.TOOL_CONFIRM:
                awaiting_confirm = True
                self._remember_confirm(event)
                await self._db.update_session_status(
                    self._session_id, SessionStatus.WAITING_CONFIRM
                )
            elif awaiting_confirm:
                awaiting_confirm = False
                # The confirm has cleared (user made a decision or tool executed).
                # Don't clear _pending_confirms here — resolve_decision already
                # removed the entry when the user approved/denied. If we clear here,
                # a disconnect before TOOL_RESULT arrives would prevent replay on
                # reconnect.
                await self._db.update_session_status(
                    self._session_id, SessionStatus.RUNNING
                )
            if event.type == StreamEventType.ASSISTANT_MESSAGE:
                saw_native_messages = True
                message = await self._db.add_message(
                    self._session_id, MessageRole.AGENT, event.data
                )
                event.message_id = message.id
                agent_buf.clear()
            elif event.type == StreamEventType.TOOL_MESSAGE:
                saw_native_messages = True
                message = await self._db.add_message(
                    self._session_id, MessageRole.TOOL, event.data
                )
                event.message_id = message.id
            elif event.type == StreamEventType.TEXT:
                if saw_native_messages:
                    should_broadcast = False
                else:
                    agent_buf.append(event.data)
            elif event.type == StreamEventType.TOOL_CALL:
                if saw_native_messages:
                    should_broadcast = False
                else:
                    message = await self._db.add_message(
                        self._session_id, MessageRole.TOOL_CALL, event.data
                    )
                    event.message_id = message.id
            elif event.type == StreamEventType.TOOL_RESULT:
                if not saw_native_messages:
                    message = await self._db.add_message(
                        self._session_id, MessageRole.TOOL, event.data
                    )
                    event.message_id = message.id
            elif event.type == StreamEventType.ERROR:
                errored = True
            if should_broadcast:
                self._broadcast(event)

        if agent_buf and not saw_native_messages:
            await self._db.add_message(
                self._session_id, MessageRole.AGENT, "\n".join(agent_buf)
            )
        if errored:
            await self._db.update_session_status(self._session_id, SessionStatus.ERROR)
        else:
            await self._db.update_session_status(self._session_id, SessionStatus.WAITING_USER)
