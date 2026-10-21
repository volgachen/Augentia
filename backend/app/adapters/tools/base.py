from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import os


@dataclass(frozen=True)
class ToolImage:
    """Raw image bytes produced by a tool (png/jpeg/webp/gif)."""

    data: bytes
    # Optional per-image detail hint for the model: "auto", "low" or "high".
    detail: str | None = None


@dataclass(frozen=True)
class ToolOutput:
    """A tool result carrying images alongside its text.

    Tools return a plain ``str`` normally; return ``ToolOutput`` when the
    result includes images (screenshots, image files). The engine stores the
    bytes in the attachment store and sends them to the model as part of the
    tool result. Engines or agents that can't take images keep only the text,
    with a note saying how many images were dropped.
    """

    text: str
    images: list[ToolImage] = field(default_factory=list)

    def text_without_images(self, reason: str) -> str:
        if not self.images:
            return self.text
        return f"{self.text}\n[{len(self.images)} image(s) omitted: {reason}]"


class BaseTool(ABC):
    name: str
    description: str
    # Default confirm policy for this tool: True = a human must approve each call
    # (safe default). Low-risk tools (read-only, task bookkeeping) override this to
    # False. An agent's config.tool_approvals can override per tool at runtime; this
    # class attribute is the fallback when the agent doesn't mention the tool.
    requires_approval: bool = True
    session_id: str | None = None
    # The session's working_dir, set by the adapter at start() time. Tools that
    # touch the filesystem (bash, read, write, edit) use this as the cwd / base
    # for relative paths so the agent operates inside its own session dir, not
    # the backend process's cwd. None means "no working dir configured" — fall
    # back to current behavior.
    working_dir: str | None = None

    def resolve_path(self, path: str) -> str:
        """Resolve a path against the session's working_dir.

        Absolute paths are returned unchanged so the agent can still address
        files outside the session dir explicitly. Relative paths are joined
        against working_dir when one is set; otherwise they're left as-is and
        resolved against the backend process cwd (legacy behavior).
        """
        if os.path.isabs(path):
            return path
        if self.working_dir:
            return os.path.normpath(os.path.join(self.working_dir, path))
        return path

    @abstractmethod
    def parameters_schema(self) -> dict:
        """Return JSON Schema for the tool's parameters."""

    @abstractmethod
    async def execute(self, **kwargs) -> "str | ToolOutput":
        """Run the tool and return a string result, or ToolOutput when it carries images."""

    async def aclose(self) -> None:
        """Release any per-session resources (long-lived subprocesses, etc.).

        Called by the adapter's stop(). Default is a no-op; stateful tools like
        node_repl override it to tear down their subprocess.
        """
        return None

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema(),
            },
        }
