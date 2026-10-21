"""Local attachment store for image input.

Image bytes live on disk under ``AUGENTIA_HOME/attachments/``, named by their
sha256 so identical uploads deduplicate. Conversation history only ever stores
references (``att:<sha256>``); the CAgent engine resolves a reference to a
``data:`` URL right before each model call.

The format is decided from the file's magic bytes, never from the client's
Content-Type or filename. Only PNG, JPEG, WebP and GIF are accepted.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.models.domain import AgentTemplate

ATTACHMENT_PREFIX = "att:"
_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DETAILS = ("auto", "low", "high")
# Upper bound on images per user turn; keeps one message from fanning out
# into an enormous model request.
MAX_IMAGES_PER_TURN = 10

_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_MIME_BY_EXT = {ext: mime for mime, ext in _EXT_BY_MIME.items()}


class AttachmentError(ValueError):
    """Rejected upload or unusable image reference; safe to show to the client."""


@dataclass(frozen=True)
class Attachment:
    id: str
    mime: str
    size: int
    path: Path

    @property
    def ref(self) -> str:
        return ATTACHMENT_PREFIX + self.id

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "ref": self.ref, "mime": self.mime, "size": self.size}


def attachments_dir() -> Path:
    return Path(get_settings().augentia_home).expanduser() / "attachments"


def max_attachment_bytes() -> int:
    raw = os.getenv("AUGENTIA_ATTACHMENT_MAX_BYTES")
    try:
        return int(raw) if raw else _DEFAULT_MAX_BYTES
    except ValueError:
        return _DEFAULT_MAX_BYTES


def sniff_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def save_attachment(data: bytes) -> Attachment:
    if not data:
        raise AttachmentError("empty upload")
    limit = max_attachment_bytes()
    if len(data) > limit:
        raise AttachmentError(f"image exceeds the {limit} byte limit")
    mime = sniff_image_mime(data)
    if mime is None:
        raise AttachmentError("unsupported image format (allowed: png, jpeg, webp, gif)")
    digest = hashlib.sha256(data).hexdigest()
    root = attachments_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{digest}.{_EXT_BY_MIME[mime]}"
    if not path.exists():
        # Write-then-rename so a crash never leaves a truncated file under the
        # final content-addressed name.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    return Attachment(id=digest, mime=mime, size=len(data), path=path)


def get_attachment(attachment_id: str) -> Attachment:
    """Look up a stored attachment by its sha256 id; KeyError when missing."""
    if not _ID_RE.match(attachment_id or ""):
        # Also the path-traversal guard: only bare hex ids reach the filesystem.
        raise KeyError(attachment_id)
    root = attachments_dir()
    for ext, mime in _MIME_BY_EXT.items():
        path = root / f"{attachment_id}.{ext}"
        if path.is_file():
            return Attachment(id=attachment_id, mime=mime, size=path.stat().st_size, path=path)
    raise KeyError(attachment_id)


def attachment_from_ref(ref: str) -> Attachment:
    if not isinstance(ref, str) or not ref.startswith(ATTACHMENT_PREFIX):
        raise AttachmentError(f"unsupported image reference: {ref!r}")
    try:
        return get_attachment(ref[len(ATTACHMENT_PREFIX):])
    except KeyError:
        raise AttachmentError(f"image not found: {ref}") from None


def data_url(ref: str) -> str:
    """Resolve an ``att:`` reference into a data URL the model can read."""
    attachment = attachment_from_ref(ref)
    encoded = base64.b64encode(attachment.path.read_bytes()).decode("ascii")
    return f"data:{attachment.mime};base64,{encoded}"


# ---- turn-level validation ----

def agent_engine(agent: AgentTemplate) -> str:
    return (agent.config or {}).get("engine", "cagent")


def agent_supports_images(agent: AgentTemplate) -> bool:
    return agent_engine(agent) == "cagent" and bool((agent.config or {}).get("vision"))


def normalize_turn_images(agent: AgentTemplate, images: Any) -> list[dict[str, str]]:
    """Validate client-supplied images for one user turn of ``agent``.

    Accepts a list of ``att:`` reference strings or ``{"ref", "detail"?}``
    objects. Returns stored-form references, applying the agent's
    ``config.image_detail`` default when an image doesn't set its own detail.
    Raises AttachmentError with a client-facing message.
    """
    if not images:
        return []
    if not isinstance(images, list):
        raise AttachmentError("images must be a list")
    if agent_engine(agent) != "cagent":
        raise AttachmentError("image input requires the cagent engine")
    if not (agent.config or {}).get("vision"):
        raise AttachmentError("this agent does not accept images (config.vision is off)")
    if len(images) > MAX_IMAGES_PER_TURN:
        raise AttachmentError(f"at most {MAX_IMAGES_PER_TURN} images per message")
    default_detail = (agent.config or {}).get("image_detail")
    result: list[dict[str, str]] = []
    for image in images:
        item = {"ref": image} if isinstance(image, str) else image
        if not isinstance(item, dict) or not set(item) <= {"ref", "detail"}:
            raise AttachmentError("each image must be a reference string or {ref, detail}")
        ref = item.get("ref")
        attachment_from_ref(ref)  # existence + format check
        normalized = {"ref": ref}
        detail = item.get("detail", default_detail)
        if detail is not None:
            if detail not in _DETAILS:
                raise AttachmentError("image detail must be auto, low or high")
            normalized["detail"] = detail
        result.append(normalized)
    return result
