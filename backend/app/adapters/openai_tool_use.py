import asyncio
import json
import logging
import os
from typing import AsyncGenerator
from openai import AsyncOpenAI
from app.adapters.base import BaseAgentAdapter, StreamEvent, StreamEventType
from app.models.domain import Message, MessageRole
import app.adapters.tools  # noqa: F401 — triggers tool registration
from app.adapters.tools.registry import load_tools
from app.adapters.tools.base import BaseTool
from app.adapters.tools.permissions import decide_tool_permission

logger = logging.getLogger("augentia.adapter.tool_use")

# Tool results can be large (e.g. a fetched web page). The full result still
# goes to the LLM context (_messages); only the persisted/broadcast copy in the
# TOOL_RESULT event is truncated.
_TOOL_RESULT_MAX = 8000


class OpenAIToolUseAdapter(BaseAgentAdapter):
    def __init__(
        self,
        tool_names: list[str],
        model: str = "gpt-4o",
        base_url: str | None = None,
        api_key: str | None = None,
        session_id: str | None = None,
        working_dir: str | None = None,
        config: dict | None = None,
    ) -> None:
        super().__init__(session_id)
        self._tool_names = tool_names
        self._config = config or {}
        # Per-agent tool approval overrides: {tool_name: requires_approval}. Merged
        # over each tool's class default at start() to build self._requires_approval.
        self._approval_overrides: dict[str, bool] = dict(self._config.get("tool_approvals", {}))
        # tool_name -> whether a human confirm is required; populated in start()
        # once the tools are loaded (so we know each tool's class-level default).
        self._requires_approval: dict[str, bool] = {}
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        # Pass-through to filesystem tools so bash/read/write/edit run in the
        # session's working_dir instead of the backend process's cwd. The
        # tools can't rely on the process cwd; the adapter threads the
        # session's working dir into each tool explicitly.
        self._working_dir = working_dir
        self._tools: list[BaseTool] = []
        self._messages: list[dict] = []
        self._pending: str | None = None
        # call_id -> (Future[bool], supplementary_msg). The Future is resolved by
        # resolve_decision when a human approves (True) or denies (False) a gated
        # tool call. supplementary_msg is set when the user provides additional
        # context with their decision.
        self._pending_confirms: dict[str, tuple[asyncio.Future[bool], str]] = {}
        self._alive = False
        self._client: AsyncOpenAI | None = None
        # Size of the conversation currently held in _messages, in tokens, as
        # reported by the model's usage on the last completion call of a turn
        # (prompt_tokens covers the full re-sent context; completion_tokens is
        # the reply appended to it). This is the number a future auto-compression
        # step will threshold on — not cumulative billed tokens, which exceed the
        # context size because the agentic loop re-sends _messages each iteration.
        self._context_tokens = 0

    async def start(self, system_prompt: str) -> None:
        base_url = self._base_url or os.getenv("OPENAI_BASE_URL")
        api_key = self._api_key or os.getenv("OPENAI_API_KEY")
        self._model = self._model if self._model != "gpt-4o" else os.getenv("OPENAI_MODEL", self._model)
        kwargs: dict = {}
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        self._client = AsyncOpenAI(**kwargs)
        self._tools = load_tools(self._tool_names)
        for t in self._tools:
            t.session_id = self.session_id
            t.working_dir = self._working_dir
        # Effective confirm policy = each tool's class default, overridden by the
        # agent's config.tool_approvals. Unknown override keys are ignored.
        self._requires_approval = {
            t.name: self._approval_overrides.get(t.name, t.requires_approval)
            for t in self._tools
        }
        if system_prompt:
            self._messages = [{"role": "system", "content": system_prompt}]
        self._alive = True
        logger.info(
            "started model=%s base_url=%s tools=%s",
            self._model, base_url, [t.name for t in self._tools],
        )

    async def send(self, message: str, images: list[dict] | None = None) -> None:
        if images:
            # Routers reject images for legacy-engine agents before they get
            # here; this guards any other caller.
            raise ValueError("the legacy engine does not support image input")
        self._pending = message

    async def restore_history(self, messages: list[Message]) -> None:
        """Rebuild conversation context after a backend restart, using OpenAI's
        native tool roles.

        New persisted rows keep OpenAI-native assistant/tool message JSON in the
        existing agent/tool roles, preserving assistant(content + tool_calls) and
        tool_call_id values. Older rows may still use split tool_call/tool records;
        those are repaired by adjacency with synthesized ids. The system prompt
        seeded in start() is preserved (SYSTEM rows aren't persisted).
        """
        restored: list[dict] = []
        i = 0
        n = len(messages)
        call_seq = 0
        while i < n:
            m = messages[i]
            if m.role == MessageRole.USER:
                restored.append({"role": "user", "content": m.content})
                i += 1
            elif m.role == MessageRole.AGENT:
                restored.append(self._parse_persisted_assistant_message(m.content))
                i += 1
            elif m.role == MessageRole.TOOL_CALL:
                name, arguments = self._parse_persisted_tool_call(m.content)
                call_id = f"restored_call_{call_seq}"
                call_seq += 1
                restored.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }],
                })
                # The matching result is the next row, unless the turn was
                # interrupted before it was persisted — then synthesize one so
                # the tool_call isn't left dangling (the API requires a response).
                result = "[result not recorded]"
                if i + 1 < n and messages[i + 1].role == MessageRole.TOOL:
                    result = self._parse_persisted_tool_result(messages[i + 1].content)
                    i += 1
                restored.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result,
                })
                i += 1
            elif m.role == MessageRole.TOOL:
                tool_message = self._parse_persisted_tool_message(m.content)
                if tool_message is not None:
                    restored.append(tool_message)
                else:
                    # Legacy orphan result with no preceding call — can't attach a
                    # tool role without a matching id, so keep it as plain assistant context.
                    restored.append({
                        "role": "assistant",
                        "content": f"[previous tool result] {m.content}",
                    })
                i += 1
            else:
                i += 1
        self._messages.extend(restored)
        logger.info(
            "restored %d history rows -> %d context messages (total=%d)",
            n, len(restored), len(self._messages),
        )

    @staticmethod
    def _parse_persisted_assistant_message(content: str) -> dict:
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            return {"role": "assistant", "content": content}
        if isinstance(obj, dict) and obj.get("role") == "assistant":
            return obj
        return {"role": "assistant", "content": content}

    @staticmethod
    def _parse_persisted_tool_message(content: str) -> dict | None:
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            return None
        if isinstance(obj, dict) and obj.get("role") == "tool" and obj.get("tool_call_id"):
            return obj
        return None

    @staticmethod
    def _parse_persisted_tool_call(content: str) -> tuple[str, str]:
        # TOOL_CALL rows store json.dumps({"name", "args"}); OpenAI wants the
        # arguments back as a JSON string.
        try:
            obj = json.loads(content)
            return obj.get("name", "unknown"), json.dumps(obj.get("args", {}))
        except (json.JSONDecodeError, TypeError, AttributeError):
            return "unknown", "{}"

    @staticmethod
    def _parse_persisted_tool_result(content: str) -> str:
        # TOOL rows store json.dumps({"name", "result"}); fall back to the raw
        # string if it isn't the expected shape.
        try:
            obj = json.loads(content)
            return obj.get("result", content) if isinstance(obj, dict) else content
        except json.JSONDecodeError:
            return content

    async def stream(self) -> AsyncGenerator[StreamEvent, None]:
        if self._pending is None or self._client is None:
            return

        self._messages.append({"role": "user", "content": self._pending})
        logger.debug("user turn: %r", self._pending)
        self._pending = None

        tool_schemas = [t.to_openai_schema() for t in self._tools]
        tool_map = {t.name: t for t in self._tools}
        loop_iter = 0
        # Usage from the most recent completion this turn. The last call's
        # prompt_tokens reflects the full context after all tool results were
        # appended, so it's the value we surface as the conversation footprint.
        last_prompt_tokens = 0
        last_completion_tokens = 0

        while True:
            loop_iter += 1
            call_kwargs: dict = {
                "model": self._model,
                "messages": self._messages,
            }
            if tool_schemas:
                call_kwargs["tools"] = tool_schemas

            logger.info("chat.completions iter=%d msg_count=%d", loop_iter, len(self._messages))
            try:
                response = await self._client.chat.completions.create(**call_kwargs)
            except Exception as e:
                logger.exception("chat.completions failed")
                yield StreamEvent(type=StreamEventType.ERROR, data=f"LLM call failed: {e}")
                return

            # A correctly-spec'd Chat Completions endpoint returns a ChatCompletion
            # object. Some OpenAI-compatible gateways (e.g. Codex/Responses-style
            # backends whose base_url is .../codex/v1) don't implement
            # /chat/completions and hand back a raw string body instead, which the
            # SDK passes through unparsed — `response` is then a str and indexing
            # .choices raises AttributeError. Fail loudly with a useful message.
            if not hasattr(response, "choices"):
                logger.error(
                    "endpoint did not return a ChatCompletion (got %s): %r",
                    type(response).__name__, response,
                )
                yield StreamEvent(
                    type=StreamEventType.ERROR,
                    data=(
                        "LLM endpoint did not return a Chat Completions response. "
                        "Check that OPENAI_BASE_URL points at an OpenAI-compatible "
                        "/chat/completions endpoint (not a Codex/Responses backend)."
                    ),
                )
                return

            usage = getattr(response, "usage", None)
            if usage is not None:
                # prompt_tokens = full context sent this iteration; completion =
                # the assistant message about to be appended. Overwrite (not add)
                # each iteration so the final values describe the last, largest call.
                last_prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                last_completion_tokens = getattr(usage, "completion_tokens", 0) or 0

            choice = response.choices[0]
            msg = choice.message
            assistant_message = msg.model_dump(exclude_unset=False)
            self._messages.append(assistant_message)
            logger.debug(
                "assistant reply: content_len=%s tool_calls=%d finish=%s",
                len(msg.content) if msg.content else 0,
                len(msg.tool_calls or []),
                choice.finish_reason,
            )

            yield StreamEvent(
                type=StreamEventType.ASSISTANT_MESSAGE,
                data=json.dumps(assistant_message, ensure_ascii=False),
            )

            if msg.content:
                yield StreamEvent(type=StreamEventType.TEXT, data=msg.content)

            if not msg.tool_calls:
                break

            # Track denials: if ALL tools are denied AND none have supplementary
            # messages, we skip the next LLM call
            all_denied = True
            any_has_message = False

            for tc in msg.tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments)
                logger.info("tool call: %s args=%s", fn_name, fn_args)

                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL,
                    data=json.dumps({"call_id": tc.id, "name": fn_name, "args": fn_args}),
                )

                # Permission gate: new-style config.tool_permissions can allow,
                # deny, or ask for filesystem tools (read/write/edit). Tools or
                # configs it does not cover fall back to the legacy boolean
                # requires_approval/tool_approvals behavior.
                denied = False
                denied_by_policy = False
                supplementary_msg = ""
                permission = decide_tool_permission(fn_name, fn_args, self._working_dir, self._config)
                if permission is not None:
                    logger.info(
                        "tool permission: tool=%s action=%s rule=%s reason=%s",
                        fn_name, permission.action, permission.rule_id, permission.reason,
                    )

                if permission is not None and permission.action == "deny":
                    denied = True
                    denied_by_policy = True
                else:
                    should_ask = (
                        permission.action == "ask"
                        if permission is not None
                        else self._requires_approval.get(fn_name, True)
                    )
                    if should_ask:
                        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
                        self._pending_confirms[tc.id] = (fut, "")
                        yield StreamEvent(
                            type=StreamEventType.TOOL_CONFIRM,
                            data=json.dumps({"call_id": tc.id, "name": fn_name, "args": fn_args}),
                        )
                        try:
                            approved = await fut
                            # Retrieve the supplementary message set by resolve_decision
                            _, supplementary_msg = self._pending_confirms.get(tc.id, (None, ""))
                        finally:
                            self._pending_confirms.pop(tc.id, None)
                        denied = not approved

                if denied:
                    if denied_by_policy:
                        reason = permission.reason if permission is not None else "policy denied"
                        result = f"Error: tool call denied by policy: {reason}"
                        logger.info("tool %s denied by policy: %s", fn_name, reason)
                    else:
                        result = "Error: user denied execution of this tool call."
                        logger.info("tool %s denied by user", fn_name)
                else:
                    all_denied = False
                    tool = tool_map.get(fn_name)
                    if tool is None:
                        result = f"Error: tool '{fn_name}' not found"
                        logger.warning("tool %s not in map (available=%s)", fn_name, list(tool_map))
                    else:
                        try:
                            result = await tool.execute(**fn_args)
                            logger.debug("tool %s result len=%d", fn_name, len(result))
                        except Exception as e:
                            logger.exception("tool %s raised", fn_name)
                            result = f"Error executing {fn_name}: {e}"

                tool_message = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
                self._messages.append(tool_message)
                yield StreamEvent(
                    type=StreamEventType.TOOL_MESSAGE,
                    data=json.dumps(tool_message, ensure_ascii=False),
                )

                result_view = (
                    result[:_TOOL_RESULT_MAX] + "\n...[truncated]"
                    if len(result) > _TOOL_RESULT_MAX
                    else result
                )
                yield StreamEvent(
                    type=StreamEventType.TOOL_RESULT,
                    data=json.dumps({"call_id": tc.id, "name": fn_name, "result": result_view}),
                )

                # If a supplementary message was provided, append it as a user message
                if supplementary_msg:
                    any_has_message = True
                    self._messages.append({
                        "role": "user",
                        "content": supplementary_msg,
                    })
                    yield StreamEvent(
                        type=StreamEventType.USER,
                        data=supplementary_msg,
                    )
                    logger.info("appended supplementary message len=%d", len(supplementary_msg))

            # If ALL tools were denied AND none have supplementary messages, stop the loop
            if all_denied and not any_has_message:
                logger.info("all tools denied without messages, skipping further LLM calls")
                break

        # context_tokens = the whole conversation as the model last measured it
        # (prompt = re-sent history, completion = final reply appended to it).
        self._context_tokens = last_prompt_tokens + last_completion_tokens
        logger.info(
            "turn complete iters=%d context_tokens=%d", loop_iter, self._context_tokens
        )
        yield StreamEvent(
            type=StreamEventType.USAGE,
            data=json.dumps({
                "context_tokens": self._context_tokens,
                "prompt_tokens": last_prompt_tokens,
                "completion_tokens": last_completion_tokens,
            }),
        )
        yield StreamEvent(type=StreamEventType.DONE, data="")

    async def resolve_decision(self, call_id: str, approved: bool, supplementary_msg: str = "") -> None:
        entry = self._pending_confirms.get(call_id)
        if entry is not None:
            fut, _ = entry
            if not fut.done():
                # Update the tuple with the supplementary message before resolving
                self._pending_confirms[call_id] = (fut, supplementary_msg)
                fut.set_result(approved)

    async def reload_config(self, config: dict) -> None:
        self._config = config or {}
        self._approval_overrides = dict(self._config.get("tool_approvals", {}))
        self._requires_approval = {
            t.name: self._approval_overrides.get(t.name, t.requires_approval)
            for t in self._tools
        }
        logger.info("reloaded session config tools=%s", list(self._requires_approval))

    async def stop(self) -> None:
        self._alive = False
        self._client = None
        # Unblock any stream awaiting a confirm so it doesn't leak a pending
        # Future when the session is torn down mid-decision.
        for fut, _ in self._pending_confirms.values():
            if not fut.done():
                fut.cancel()
        self._pending_confirms.clear()
        # Let stateful tools (e.g. node_repl) tear down long-lived subprocesses.
        for t in self._tools:
            try:
                await t.aclose()
            except Exception:
                logger.exception("tool aclose failed: %s", t.name)

    def current_system_prompt(self) -> str | None:
        for message in self._messages:
            if message.get("role") == "system":
                content = message.get("content")
                return content if isinstance(content, str) else None
        return None

    @property
    def context_tokens(self) -> int:
        """Token footprint of the conversation as of the last completed turn.

        This is what a future auto-compression step should threshold on: when it
        exceeds a budget, summarize/trim _messages and reset. 0 until the first
        turn completes (and after a restart, until the first post-rehydrate turn
        re-reports usage — we don't persist it since the API refreshes it for free).
        """
        return self._context_tokens

    @property
    def is_alive(self) -> bool:
        return self._alive
