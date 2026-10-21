from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING, AsyncGenerator
from pydantic import BaseModel

if TYPE_CHECKING:
    from app.models.domain import Message


class StreamEventType(str, Enum):
    USER = "user"
    TEXT = "text"
    # Internal persistence event: data is the raw OpenAI assistant message JSON.
    # The runner stores it but does not broadcast it to UI subscribers.
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    # Internal persistence event: data is the raw OpenAI tool message JSON.
    TOOL_MESSAGE = "tool_message"
    # Emitted before a tool that isn't auto-approved runs; the adapter blocks on
    # a human decision routed back via resolve_decision. data is JSON
    # {call_id, name, args}.
    TOOL_CONFIRM = "tool_confirm"
    TOOL_RESULT = "tool_result"
    # Emitted once per turn (just before DONE) with the current conversation's
    # token footprint so the UI can show context usage and a future
    # auto-compression step can decide when to summarize. data is JSON
    # {context_tokens, prompt_tokens, completion_tokens}.
    USAGE = "usage"
    STATUS = "status"
    ERROR = "error"
    DONE = "done"


class StreamEvent(BaseModel):
    type: StreamEventType
    data: str
    message_id: str | None = None
    # Image references ([{"ref": "att:<sha256>", "detail"?: ...}]) on USER
    # events (images attached to the turn) and TOOL_RESULT events (images the
    # tool returned). None for every other event.
    images: list[dict] | None = None


class BaseAgentAdapter(ABC):
    session_id: str | None = None

    def __init__(self, session_id: str | None = None) -> None:
        # session_id is fixed at construction so tools that read it (e.g. the
        # subagent tool, which needs it as parent_session_id) see the real id
        # before start() runs — not None patched in later.
        self.session_id = session_id

    @abstractmethod
    async def start(self, system_prompt: str) -> None:
        """启动 Agent，建立底层进程或连接。"""

    @abstractmethod
    async def send(self, message: str, images: list[dict] | None = None) -> None:
        """向 Agent 发送用户消息；images 为本轮附带的图片引用（仅支持图片的引擎接受）。"""

    @abstractmethod
    async def stream(self) -> AsyncGenerator[StreamEvent, None]:
        """异步生成器，持续 yield StreamEvent 直到本轮结束（DONE）或出错。"""

    @abstractmethod
    async def stop(self) -> None:
        """终止 Agent，释放所有资源。"""

    async def restore_history(self, messages: "list[Message]") -> None:
        """Rebuild in-memory conversation state from persisted messages.

        Called when a session is rehydrated after a backend restart (the
        in-memory runner/adapter is gone but the DB rows survive). Default is a
        no-op; adapters that keep conversation history in process memory (e.g.
        OpenAI tool-use) override this so a resumed session isn't amnesiac.
        """
        return

    async def resolve_decision(self, call_id: str, approved: bool, supplementary_msg: str = "") -> None:
        """Resolve a pending tool-confirm request (see TOOL_CONFIRM).

        Called by the runner when a human approves/denies a tool call the
        adapter is blocked on. If supplementary_msg is provided, the adapter
        should append it as a user message after the tool result. If denied
        without a message, the adapter should skip the next LLM call.
        Default is a no-op — adapters that don't gate tools simply
        ignore it.
        """
        return

    async def reload_config(self, config: dict) -> None:
        """Apply updated per-session runtime config without restarting the adapter.

        Default is a no-op; adapters that use config during tool execution can
        override it so UI changes take effect for the current live session.
        """
        return

    def current_system_prompt(self) -> str | None:
        """Return the live in-memory system prompt, when the adapter exposes one."""
        return None

    @property
    @abstractmethod
    def is_alive(self) -> bool:
        """Agent 进程/连接是否仍在运行。"""
