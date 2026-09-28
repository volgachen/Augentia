"""CAgent-backed tool-use adapter: same contract as OpenAIToolUseAdapter.

The agent loop runs on CAgent's ``run_turn`` (persist-first history, paired
tool results, cooperative interruption, turn-end marks). This adapter bridges
it onto Augentia's adapter/stream contract:

- ``stream()`` runs one ``run_turn`` as a background task and yields StreamEvents
  from a bounded queue (backpressure: the loop blocks until the consumer drains).
- The dynamic agent class (see cagent_agent) routes every tool call through
  ``gate_tool_call``: permission policy, human confirmation via TOOL_CONFIRM +
  a Future resolved by ``resolve_decision``, and supplementary instructions
  queued on ``ctx.pending_messages``.
- ``on_event`` (step_end) turns saved messages into the same event stream the
  legacy adapter emits: ASSISTANT_MESSAGE / TEXT / TOOL_MESSAGE / TOOL_RESULT.
  The assistant message is announced from the gate before its first tool call
  so the confirm panel appears in the same order as before.
- The adapter persists its own messages through AugentiaStore, so the runner
  skips its second-write persistence path (``persists_own_messages``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from openai import AsyncOpenAI

from app.adapters.base import BaseAgentAdapter, StreamEvent, StreamEventType
from app.adapters.cagent_agent import make_agent_class
from app.adapters.cagent_store import AugentiaStore, _wire_tool_calls
from app.adapters.tools.base import BaseTool  # noqa: F401 — type hint for tests
from app.adapters.tools.permissions import decide_tool_permission
from app.adapters.tools.registry import load_tools

logger = logging.getLogger("augentia.adapter.cagent")

_TOOL_RESULT_MAX = 8000
_EVENT_QUEUE_MAX = 256
_FROM_SESSION_RE = re.compile(r"^\[from-session:([^\]]+)\]\s")
_DEFAULT_MAX_STEPS = 50


def _truncate_result(result: str) -> str:
    if len(result) <= _TOOL_RESULT_MAX:
        return result
    return result[:_TOOL_RESULT_MAX] + "\n...[truncated]"


def _make_model(client: AsyncOpenAI, model_name: str):
    """Wrap an AsyncOpenAI client as a CAgent model callable."""

    async def model(*, messages, tools):
        wire_messages = []
        for message in messages:
            wire = {"role": message["role"], "content": message["content"]}
            if message["role"] == "tool":
                wire["tool_call_id"] = message["tool_call_id"]
            if message.get("tool_calls"):
                wire["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(
                                call.get("arguments") or {}, ensure_ascii=False
                            ),
                        },
                    }
                    for call in message["tool_calls"]
                ]
            wire_messages.append(wire)
        call_kwargs: dict = {"model": model_name, "messages": wire_messages}
        if tools:
            call_kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["parameters"],
                    },
                }
                for tool in tools
            ]
        response = await client.chat.completions.create(**call_kwargs)
        # Some OpenAI-compatible gateways hand back a raw string body instead
        # of a ChatCompletion; fail loudly (same guard as the legacy adapter).
        if not hasattr(response, "choices"):
            raise RuntimeError(
                "LLM endpoint did not return a Chat Completions response. Check "
                "that OPENAI_BASE_URL points at an OpenAI-compatible "
                "/chat/completions endpoint."
            )
        message = response.choices[0].message
        calls = []
        for tool_call in message.tool_calls or []:
            arguments = json.loads(tool_call.function.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a JSON object")
            calls.append({
                "id": tool_call.id,
                "name": tool_call.function.name,
                "arguments": arguments,
            })
        usage = getattr(response, "usage", None)
        return {
            "content": message.content or "",
            "tool_calls": calls,
            "usage": usage.model_dump() if usage is not None else None,
        }

    return model


class CAgentAdapter(BaseAgentAdapter):
    # The adapter (via AugentiaStore) is the single writer of conversation
    # rows; the runner must not double-persist from the event stream.
    persists_own_messages = True

    def __init__(
        self,
        db,
        tool_names: list[str],
        model: str = "gpt-4o",
        base_url: str | None = None,
        api_key: str | None = None,
        session_id: str | None = None,
        working_dir: str | None = None,
        config: dict | None = None,
        model_callable=None,
    ) -> None:
        super().__init__(session_id)
        self._db = db
        self._tool_names = tool_names
        self._config = config or {}
        self._approval_overrides: dict[str, bool] = dict(self._config.get("tool_approvals", {}))
        self._requires_approval: dict[str, bool] = {}
        self._model_name = model
        self._base_url = base_url
        self._api_key = api_key
        # Test injection point; None builds the AsyncOpenAI-backed model.
        self._model_callable = model_callable
        self._working_dir = working_dir
        self._tools: list = []
        self._client: AsyncOpenAI | None = None
        self._model = None
        self._store: AugentiaStore | None = None
        self._agent_cls = None
        self._system_prompt: str | None = None
        self._pending: str | None = None
        self._pending_from_session: str | None = None
        self._pending_confirms: dict[str, tuple[asyncio.Future, str]] = {}
        self._events: asyncio.Queue | None = None
        self._turn_task: asyncio.Task | None = None
        self._announced_assistant_id: str | None = None
        self._call_info: dict[str, dict] = {}
        self._context_tokens = 0
        self._alive = False

    # ---- properties used by the generated agent class ----

    @property
    def tools(self) -> list:
        return self._tools

    # ---- BaseAgentAdapter contract ----

    async def start(self, system_prompt: str) -> None:
        base_url = self._base_url or os.getenv("OPENAI_BASE_URL")
        api_key = self._api_key or os.getenv("OPENAI_API_KEY")
        self._model_name = (
            self._model_name if self._model_name != "gpt-4o"
            else os.getenv("OPENAI_MODEL", self._model_name)
        )
        kwargs: dict = {}
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        self._client = AsyncOpenAI(**kwargs)
        self._tools = load_tools(self._tool_names)
        for tool in self._tools:
            tool.session_id = self.session_id
            tool.working_dir = self._working_dir
        self._requires_approval = {
            tool.name: self._approval_overrides.get(tool.name, tool.requires_approval)
            for tool in self._tools
        }
        self._system_prompt = system_prompt
        self._store = AugentiaStore(self._db, self.session_id)
        self._agent_cls = make_agent_class(
            self,
            name="Augentia Agent",
            system_prompt=system_prompt,
            max_steps=int(self._config.get("max_steps", _DEFAULT_MAX_STEPS)),
            compression_token_threshold=self._config.get("compression_token_threshold"),
            compression_turn_threshold=self._config.get("compression_turn_threshold"),
        )
        self._model = (
            self._model_callable
            if self._model_callable is not None
            else _make_model(self._client, self._model_name)
        )
        self._alive = True
        logger.info(
            "started cagent engine model=%s base_url=%s tools=%s",
            self._model_name, base_url, [t.name for t in self._tools],
        )

    async def send(self, message: str) -> None:
        self._pending = message
        match = _FROM_SESSION_RE.match(message)
        if match:
            self._pending_from_session = match.group(1)

    async def stream(self):
        if self._pending is None or self._agent_cls is None:
            return
        content, self._pending = self._pending, None
        if not content.strip():
            return
        self._announced_assistant_id = None
        self._call_info = {}
        self._store.pending_from_session = self._pending_from_session
        self._pending_from_session = None
        self._events = asyncio.Queue(maxsize=_EVENT_QUEUE_MAX)
        turn_task = asyncio.create_task(self._run_turn_task(content))
        self._turn_task = turn_task
        try:
            while True:
                event = await self._events.get()
                yield event
                if event.type == StreamEventType.DONE:
                    break
        finally:
            self._turn_task = None
            if not turn_task.done():
                turn_task.cancel()
            try:
                await turn_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("cagent turn task crashed session=%s", self.session_id)

    async def resolve_decision(self, call_id: str, approved: bool, supplementary_msg: str = "") -> None:
        entry = self._pending_confirms.get(call_id)
        if entry is not None:
            future, _ = entry
            if not future.done():
                self._pending_confirms[call_id] = (future, supplementary_msg)
                future.set_result(approved)

    async def reload_config(self, config: dict) -> None:
        self._config = config or {}
        self._approval_overrides = dict(self._config.get("tool_approvals", {}))
        self._requires_approval = {
            tool.name: self._approval_overrides.get(tool.name, tool.requires_approval)
            for tool in self._tools
        }
        # max_steps / compression thresholds are bound into the generated agent
        # class at start(); changing them takes effect on the next session.
        logger.info("reloaded session config tools=%s", list(self._requires_approval))

    async def stop(self) -> None:
        self._alive = False
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("cagent turn task crashed during stop session=%s", self.session_id)
        self._turn_task = None
        for future, _ in self._pending_confirms.values():
            if not future.done():
                future.cancel()
        self._pending_confirms.clear()
        for tool in self._tools:
            try:
                await tool.aclose()
            except Exception:
                logger.exception("tool aclose failed: %s", tool.name)
        self._client = None

    def current_system_prompt(self) -> str | None:
        return self._system_prompt

    @property
    def context_tokens(self) -> int:
        return self._context_tokens

    @property
    def is_alive(self) -> bool:
        return self._alive

    # ---- turn execution ----

    async def _run_turn_task(self, content: str) -> None:
        try:
            ctx = await self._agent_cls.run_turn(
                content,
                session_id=self.session_id,
                store=self._store,
                model=self._model,
                on_event=self._on_event,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("cagent turn failed session=%s", self.session_id)
            # ERROR must be followed by DONE or the stream consumer never
            # finishes and the session sticks in RUNNING.
            await self._emit(StreamEventType.ERROR, f"CAgent turn failed: {exc}")
            await self._emit(StreamEventType.DONE, "")
            return
        usage = ctx.usage[-1] if ctx.usage else None
        prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        completion_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
        if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
            context_tokens = prompt_tokens + completion_tokens
        else:
            context_tokens = ctx.num_tokens or 0
        self._context_tokens = context_tokens
        await self._emit(StreamEventType.USAGE, {
            "context_tokens": context_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        })
        await self._emit(StreamEventType.DONE, "")

    # ---- gating (called by the generated agent class) ----

    async def gate_tool_call(self, store, ctx, tool_call) -> str | None:
        call_id = tool_call["id"]
        name = tool_call["name"]
        arguments = tool_call.get("arguments") or {}

        # Announce the assistant message before its first tool call so the
        # confirm panel appears in the same order the legacy adapter produced.
        assistant = self._find_call_assistant(ctx, call_id)
        if assistant is not None and assistant["id"] != self._announced_assistant_id:
            await self._announce_assistant(assistant)

        permission = decide_tool_permission(name, arguments, self._working_dir, self._config)
        if permission is not None and permission.action == "deny":
            logger.info("tool %s denied by policy: %s", name, permission.reason)
            return f"denied by policy: {permission.reason}"
        should_ask = (
            permission.action == "ask"
            if permission is not None
            else self._requires_approval.get(name, True)
        )
        if not should_ask:
            return None

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_confirms[call_id] = (future, "")
        await self._emit(StreamEventType.TOOL_CONFIRM, {
            "call_id": call_id, "name": name, "args": arguments,
        })
        try:
            approved = await future
            supplementary = self._pending_confirms.get(call_id, (None, ""))[1]
        except asyncio.CancelledError:
            # The turn was cancelled while waiting for the human. Flip the
            # framework status so the executor pairs the remaining calls and
            # stops the turn cooperatively instead of leaving dangling history.
            await store.set_status(ctx.session_id, "INTERRUPTED")
            return "等待人工确认时本轮被取消，未执行"
        finally:
            self._pending_confirms.pop(call_id, None)

        if supplementary:
            # Delivered as an extra user message after this batch's tool
            # results, before the next model step (CAgent pending_messages).
            ctx.pending_messages.append(supplementary)
            await self._emit(StreamEventType.USER, supplementary)
            logger.info("queued supplementary message len=%d", len(supplementary))
        return None if approved else "用户拒绝执行该工具调用"

    def _find_call_assistant(self, ctx, call_id: str) -> dict | None:
        for message in reversed(ctx.messages):
            if message["role"] == "assistant":
                if any(call["id"] == call_id for call in message.get("tool_calls") or []):
                    return message
        return None

    async def _announce_assistant(self, message: dict) -> None:
        self._announced_assistant_id = message["id"]
        for call in message.get("tool_calls") or []:
            self._call_info[call["id"]] = {
                "name": call["name"], "args": call.get("arguments") or {},
            }
        wire: dict = {
            "role": "assistant",
            "content": message.get("content") or "",
            "refusal": None,
        }
        if message.get("tool_calls"):
            wire["tool_calls"] = _wire_tool_calls(message["tool_calls"])
        await self._emit(StreamEventType.ASSISTANT_MESSAGE, json.dumps(wire, ensure_ascii=False))
        if message.get("content"):
            await self._emit(StreamEventType.TEXT, message["content"])

    # ---- event bridge ----

    async def _on_event(self, event: dict) -> None:
        if event.get("event") != "step_end":
            return
        for message in event.get("messages", []):
            role = message.get("role")
            if role == "assistant":
                if message.get("id") != self._announced_assistant_id:
                    await self._announce_assistant(message)
            elif role == "tool":
                call_id = message.get("tool_call_id")
                info = self._call_info.get(call_id, {})
                await self._emit(StreamEventType.TOOL_MESSAGE, json.dumps({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": message.get("content", ""),
                }, ensure_ascii=False))
                await self._emit(StreamEventType.TOOL_RESULT, {
                    "call_id": call_id,
                    "name": info.get("name", "unknown"),
                    "result": _truncate_result(message.get("content", "")),
                })

    async def _emit(self, event_type: StreamEventType, data) -> None:
        if not isinstance(data, str):
            data = json.dumps(data, ensure_ascii=False)
        # Bounded queue: the agent loop blocks until the consumer drains, which
        # keeps a long turn from outrunning the runner's event relay.
        await self._events.put(StreamEvent(type=event_type, data=data))
