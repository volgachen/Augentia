from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import os


@dataclass(frozen=True)
class ToolImage:
    """Raw image bytes produced by a tool (png/jpeg/webp/gif)."""

    data: bytes
    # Optional per-image detail hint for the model: "auto", "low" or "high".
    detail: str | None = None
    # Character offset in ToolOutput.text where the image sits. None = after
    # all text. Usually set by ToolOutput.interleaved rather than by hand.
    at: int | None = None


# Left in the text where an interleaved image could not be delivered, so the
# model still sees that something was there.
IMAGE_OMITTED_MARKER = "[image omitted]"


@dataclass(frozen=True)
class ToolOutput:
    """A tool result carrying images alongside its text.

    Tools return a plain ``str`` normally; return ``ToolOutput`` when the
    result includes images (screenshots, image files). The engine stores the
    bytes in the attachment store and sends them to the model as part of the
    tool result. Engines or agents that can't take images keep only the text,
    with a note saying how many images were dropped.

    Images either follow all text (``ToolOutput(text, images=[...])``) or are
    interleaved with it (``ToolOutput.interleaved(["Page 1:", img1, "Page 2:",
    img2])``), in which case each image's ``at`` is its offset in ``text`` and
    the model sees text and images in that order.
    """

    text: str
    images: list[ToolImage] = field(default_factory=list)

    def __post_init__(self):
        cursor = 0
        for image in self.images:
            at = len(self.text) if image.at is None else image.at
            if isinstance(at, bool) or not isinstance(at, int) or not cursor <= at <= len(self.text):
                raise ValueError("ToolImage.at offsets must be ordered positions within the text")
            cursor = at

    @classmethod
    def interleaved(cls, parts: "list[str | ToolImage]") -> "ToolOutput":
        """Build from a list of text strings and ToolImage objects, kept in order."""
        text, images = "", []
        for part in parts:
            if isinstance(part, str):
                text += part
            elif isinstance(part, ToolImage):
                images.append(ToolImage(part.data, part.detail, at=len(text)))
            else:
                raise TypeError("Interleaved tool output may only contain str and ToolImage")
        return cls(text, images)

    @property
    def is_interleaved(self) -> bool:
        return any(image.at is not None for image in self.images)

    def segments(self) -> "list[str | ToolImage]":
        """The output as ordered parts: text strings and images at their offsets."""
        parts: list[str | ToolImage] = []
        cursor = 0
        for image in self.images:
            at = len(self.text) if image.at is None else image.at
            if self.text[cursor:at]:
                parts.append(self.text[cursor:at])
            parts.append(image)
            cursor = at
        if self.text[cursor:]:
            parts.append(self.text[cursor:])
        return parts

    def text_without_images(self, reason: str) -> str:
        if not self.images:
            return self.text
        if self.is_interleaved:
            # Keep the layout readable: mark where each image was.
            text = "".join(p if isinstance(p, str) else IMAGE_OMITTED_MARKER for p in self.segments())
        else:
            text = self.text
        return f"{text}\n[{len(self.images)} image(s) omitted: {reason}]"


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
