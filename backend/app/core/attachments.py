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
import io
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.models.domain import AgentTemplate

ATTACHMENT_PREFIX = "att:"
_ID_RE = re.compile(r"^[0-9a-f]{64}$")
logger = logging.getLogger(__name__)

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
# Limits for images sent to the model (originals on disk are not modified).
# Anthropic caps each image at 5 MB (5242880 bytes, base64 length) and
# downscales anything over ~1568px anyway, so 2000px loses nothing useful.
# We cap at 1 MB (base64 length) to keep requests with many images small.
_MODEL_IMAGE_MAX_BYTES = 1_000_000
_MODEL_IMAGE_MAX_SIDE = 2000
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


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def model_image_max_bytes() -> int:
    """Max size of one image *as sent to the model* (base64-encoded length).

    Default 1 MB; Anthropic (incl. Bedrock) hard-rejects images over 5 MB.
    """
    return _env_int("AUGENTIA_MODEL_IMAGE_MAX_BYTES", _MODEL_IMAGE_MAX_BYTES)


def model_image_max_side() -> int:
    """Longest edge (px) of an image sent to the model; 0 disables resizing."""
    return _env_int("AUGENTIA_MODEL_IMAGE_MAX_SIDE", _MODEL_IMAGE_MAX_SIDE)


def _b64_len(n: int) -> int:
    return 4 * ((n + 2) // 3)


def _shrink_for_model(data: bytes, mime: str, max_bytes: int, max_side: int) -> tuple[bytes, str]:
    """Downscale / re-encode an image so it fits the model's limits.

    Returns the original bytes untouched when already within limits.
    """
    from PIL import Image as PILImage, ImageOps  # local import: only needed here

    with PILImage.open(io.BytesIO(data)) as img:
        too_big = _b64_len(len(data)) > max_bytes
        too_wide = max_side > 0 and max(img.size) > max_side
        if not too_big and not too_wide:
            return data, mime
        img.seek(0)  # animated GIF/WebP: first frame only
        has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
        # Re-encoding drops EXIF, so bake the orientation into the pixels first
        # (otherwise phone photos taken upright reach the model sideways).
        frame = ImageOps.exif_transpose(img).convert("RGBA" if has_alpha else "RGB")

    if too_wide:
        frame.thumbnail((max_side, max_side), PILImage.LANCZOS)

    def encode(im, fmt: str, **kw) -> bytes:
        buf = io.BytesIO()
        im.save(buf, format=fmt, **kw)
        return buf.getvalue()

    # Keep PNG for transparent images when that already fits.
    if has_alpha:
        out = encode(frame, "PNG", optimize=True)
        if _b64_len(len(out)) <= max_bytes:
            return out, "image/png"
        background = PILImage.new("RGB", frame.size, (255, 255, 255))
        background.paste(frame, mask=frame.getchannel("A"))
        frame = background

    # JPEG with decreasing quality, then decreasing resolution.
    while True:
        for quality in (90, 80, 70, 60, 50):
            out = encode(frame, "JPEG", quality=quality, optimize=True)
            if _b64_len(len(out)) <= max_bytes:
                return out, "image/jpeg"
        if max(frame.size) <= 256:
            return out, "image/jpeg"  # give up shrinking further
        frame = frame.resize((max(1, frame.width * 3 // 4), max(1, frame.height * 3 // 4)), PILImage.LANCZOS)


def model_image_bytes(attachment: Attachment) -> tuple[bytes, str]:
    """Bytes + mime of ``attachment`` sized for the model; cached on disk."""
    max_bytes = model_image_max_bytes()
    max_side = model_image_max_side()
    data = attachment.path.read_bytes()
    fits = _b64_len(len(data)) <= max_bytes
    if fits and max_side <= 0:
        return data, attachment.mime
    cache_dir = attachments_dir() / "model"
    for ext, mime in (("jpg", "image/jpeg"), ("png", "image/png")):
        cached = cache_dir / f"{attachment.id}.{max_bytes}.{max_side}.{ext}"
        if cached.is_file():
            return cached.read_bytes(), mime
    try:
        out, mime = _shrink_for_model(data, attachment.mime, max_bytes, max_side)
    except Exception:
        logger.exception("could not shrink image %s for the model", attachment.id)
        return data, attachment.mime
    if out is not data:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / f"{attachment.id}.{max_bytes}.{max_side}.{_EXT_BY_MIME[mime]}"
        tmp = cached.with_suffix(cached.suffix + ".tmp")
        tmp.write_bytes(out)
        tmp.replace(cached)
    return out, mime


def data_url(ref: str) -> str:
    """Resolve an ``att:`` reference into a data URL the model can read.

    Oversized images are downscaled/re-encoded (see ``model_image_bytes``);
    the stored original is left untouched.
    """
    attachment = attachment_from_ref(ref)
    data, mime = model_image_bytes(attachment)
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


# ---- turn-level validation ----

def agent_supports_images(agent: AgentTemplate) -> bool:
    return bool((agent.config or {}).get("vision"))


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
