"""Public read-only view of sessions their owner chose to share.

Everything here is exempt from the authentication middleware (see main.py),
so it must only ever expose sessions with sharing turned on, and only the
transcript: no working directory, system prompt, config or write access.
A session that is missing, deleted or not shared yields the same 404 so the
endpoint cannot be used to probe which session ids exist.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Response

from app.core.attachments import ATTACHMENT_PREFIX, get_attachment
from app.core.compressed import COMPRESSED_BY, DELETED_BY, is_completed_mark
from app.core.session_share import is_shared
from app.db.deps import get_db
from app.db.interface import IAgentDatabase
from app.models.domain import MessageRole
from app.routers.sessions import compressed_messages, live_or_user_deleted, transcript_messages

logger = logging.getLogger("augentia.share")
router = APIRouter(prefix="/share", tags=["share"])

_NOT_SHARED = "shared session not found"


async def _shared_messages(session_id: str, db: IAgentDatabase, *, include_deleted: bool = False):
    if not is_shared(session_id):
        raise HTTPException(status_code=404, detail=_NOT_SHARED)
    try:
        session = await db.get_session(session_id)
        messages = await db.get_messages(session_id, include_deleted=include_deleted)
    except KeyError:
        raise HTTPException(status_code=404, detail=_NOT_SHARED)
    if include_deleted:
        messages = live_or_user_deleted(messages)
    return session, transcript_messages(messages, include_compression=True)


@router.get("/{session_id}")
async def get_shared_session(
    session_id: str,
    include_deleted: bool = False,
    db: IAgentDatabase = Depends(get_db),
):
    """include_deleted adds rows removed with the delete button (the toggle on
    the share page); rows a rewrite discarded are never exposed here."""
    session, messages = await _shared_messages(session_id, db, include_deleted=include_deleted)
    try:
        agent_name = (await db.get_agent(session.agent_id)).name
    except KeyError:
        agent_name = None
    return {
        "session": {
            "id": session.id,
            "title": session.title,
            "agent_name": agent_name,
            "status": session.status,
            "created_at": session.created_at,
            "last_message_at": session.last_message_at,
        },
        "messages": [_public_message(m) for m in messages],
    }


def _referenced_attachments(messages) -> set[str]:
    ids: set[str] = set()
    for m in messages:
        for image in (m.metadata or {}).get("images") or []:
            ref = image.get("ref") if isinstance(image, dict) else image
            if isinstance(ref, str) and ref.startswith(ATTACHMENT_PREFIX):
                ids.add(ref[len(ATTACHMENT_PREFIX):])
    return ids


def _public_message(m) -> dict:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "metadata": m.metadata,
        "created_at": m.created_at,
    }


@router.get("/{session_id}/messages/{summary_id}/compressed")
async def get_shared_compressed(session_id: str, summary_id: str, db: IAgentDatabase = Depends(get_db)):
    """Originals a summary replaced, for expanding it on the share page."""
    await _shared_messages(session_id, db)
    try:
        rows = await compressed_messages(db, session_id, summary_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="summary not found")
    return [_public_message(m) for m in rows]


@router.get("/{session_id}/locate/{message_id}")
async def locate_shared_message(session_id: str, message_id: str, db: IAgentDatabase = Depends(get_db)):
    """Where `message_id` sits on the share page, for links to one message.

    expand: summary ids to expand, outermost first, so the row becomes
    visible (a compressed original lives under its summary, which may itself
    be compressed). role lets the page reveal tool rows it may be hiding.
    deleted: the row, or the summary it ends up under, was
    removed with the delete button, so it only shows with include_deleted.
    Rows the page never shows (framework marks, rows a rewrite discarded)
    yield the same 404 as a missing id.
    """
    await _shared_messages(session_id, db)
    rows = {m.id: m for m in await db.get_messages(session_id, include_deleted=True)}
    not_found = HTTPException(status_code=404, detail="message not found")
    current = rows.get(message_id)
    if current is None or (current.role == MessageRole.MARK and not is_completed_mark(current)):
        raise not_found
    role = current.role
    expand: list[str] = []
    deleted = False
    while current.deleted_at is not None:
        metadata = current.metadata or {}
        if metadata.get(DELETED_BY) == "user":
            deleted = True
            break
        summary_id = metadata.get(COMPRESSED_BY)
        if not summary_id or summary_id not in rows or summary_id in expand:
            raise not_found
        expand.append(summary_id)
        current = rows[summary_id]
    return {"message_id": message_id, "role": role, "expand": expand[::-1], "deleted": deleted}


@router.get("/{session_id}/attachments/{attachment_id}")
async def get_shared_attachment(
    session_id: str,
    attachment_id: str,
    db: IAgentDatabase = Depends(get_db),
):
    # Only images that appear in the shared transcript: the attachment store is
    # global and content-addressed, so serving arbitrary ids would leak images
    # from unshared sessions to anyone who knows (or guesses) a hash.
    await _shared_messages(session_id, db)
    # Compressed originals and button-deleted rows can both be shown on the
    # share page, so their images count as part of the shared transcript too.
    # Rows a rewrite discarded cannot, so theirs do not.
    messages = await db.get_messages(session_id, include_deleted=True)
    messages = [
        m for m in messages
        if m.deleted_at is None
        or (m.metadata or {}).get("compressed_by")
        or (m.metadata or {}).get("deleted_by") == "user"
    ]
    if attachment_id not in _referenced_attachments(messages):
        raise HTTPException(status_code=404, detail="attachment not found")
    try:
        attachment = get_attachment(attachment_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="attachment not found")
    return Response(
        content=attachment.path.read_bytes(),
        media_type=attachment.mime,
        # Not "immutable": access ends when the owner stops sharing.
        headers={"Cache-Control": "private, max-age=300"},
    )
