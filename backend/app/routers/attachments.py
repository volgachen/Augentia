"""Image attachment upload/download.

Uploads send the raw image bytes as the request body (``Content-Type:
image/*``), so no multipart dependency is needed. The returned ``ref`` is what
clients put into a message's ``images`` list. Both endpoints sit under
/api/v1 and are covered by the global authentication middleware.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, Response

from app.core.attachments import (
    AttachmentError,
    get_attachment,
    max_attachment_bytes,
    save_attachment,
)

logger = logging.getLogger("augentia.attachments")
router = APIRouter(prefix="/attachments", tags=["attachments"])


@router.post("", status_code=201)
async def upload_attachment(request: Request):
    limit = max_attachment_bytes()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"image exceeds the {limit} byte limit")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail=f"image exceeds the {limit} byte limit")
    try:
        attachment = save_attachment(bytes(body))
    except AttachmentError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("stored attachment id=%s mime=%s size=%d", attachment.id, attachment.mime, attachment.size)
    return attachment.public()


@router.get("/{attachment_id}")
async def download_attachment(attachment_id: str):
    try:
        attachment = get_attachment(attachment_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="attachment not found")
    return Response(
        content=attachment.path.read_bytes(),
        media_type=attachment.mime,
        # Content-addressed: the bytes behind an id never change.
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )
