import os
from app.adapters.tools.base import BaseTool, ToolImage, ToolOutput
from app.adapters.tools.registry import register_tool
from app.core.attachments import max_attachment_bytes, sniff_image_mime

# No tokenizer dependency in the project, so approximate tokens from characters
# (~4 chars/token is the usual rule of thumb for English/code).
_MAX_TOKENS = 16384
_CHARS_PER_TOKEN = 4
_MAX_CHARS = _MAX_TOKENS * _CHARS_PER_TOKEN
# Enough bytes to recognise every supported image format's magic number.
_SNIFF_BYTES = 16


@register_tool
class ReadTool(BaseTool):
    name = "read"
    requires_approval = False
    description = (
        "Read a file. Text files return their contents with 1-based line numbers "
        "(format: '<lineno>\\t<content>'); use offset/limit to read a slice. "
        f"Output is truncated past ~{_MAX_TOKENS} tokens. Image files (png, jpeg, "
        "webp, gif) are returned as the image itself so you can look at it "
        "(offset/limit do not apply)."
    )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path of the file to read.",
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "1-based line number to start reading from "
                        "(default: start of file)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read (default: all).",
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        path: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> "str | ToolOutput":
        path = self.resolve_path(path)
        if not os.path.isfile(path):
            return f"[Error] File not found: {path}"

        image = self._read_image(path)
        if image is not None:
            return image

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError as e:
            return f"[Error] Could not read {path}: {e}"

        start = (offset - 1) if offset and offset > 0 else 0
        end = (start + limit) if limit and limit > 0 else len(lines)
        selected = lines[start:end]

        rendered: list[str] = []
        chars = 0
        truncated = False
        for i, content in enumerate(selected):
            row = f"{start + i + 1}\t{content}"
            if chars + len(row) + 1 > _MAX_CHARS:
                truncated = True
                break
            rendered.append(row)
            chars += len(row) + 1

        output = "\n".join(rendered)
        if truncated:
            output += f"\n[Truncated: output exceeded {_MAX_TOKENS} tokens]"
        return output if output else "(empty file)"

    @staticmethod
    def _read_image(path: str) -> "ToolOutput | str | None":
        """Return the image for a png/jpeg/webp/gif file, or None for other files.

        Detection uses the file's magic bytes, not its extension.
        """
        try:
            with open(path, "rb") as f:
                head = f.read(_SNIFF_BYTES)
        except OSError as e:
            return f"[Error] Could not read {path}: {e}"
        mime = sniff_image_mime(head)
        if mime is None:
            return None
        size = os.path.getsize(path)
        limit = max_attachment_bytes()
        if size > limit:
            return f"[Error] Image {path} is {size} bytes; the limit is {limit} bytes."
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            return f"[Error] Could not read {path}: {e}"
        return ToolOutput(
            text=f"[Image] {path} ({mime}, {size} bytes)",
            images=[ToolImage(data)],
        )
