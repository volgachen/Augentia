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
from app.core.session_share import is_shared
from app.db.deps import get_db
from app.db.interface import IAgentDatabase
from app.routers.sessions import transcript_messages

logger = logging.getLogger("augentia.share")
router = APIRouter(prefix="/share", tags=["share"])

_NOT_SHARED = "shared session not found"


async def _shared_messages(session_id: str, db: IAgentDatabase):
    if not is_shared(session_id):
        raise HTTPException(status_code=404, detail=_NOT_SHARED)
    try:
        session = await db.get_session(session_id)
        messages = await db.get_messages(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=_NOT_SHARED)
    return session, transcript_messages(messages, include_compression=True)


@router.get("/{session_id}")
async def get_shared_session(session_id: str, db: IAgentDatabase = Depends(get_db)):
    session, messages = await _shared_messages(session_id, db)
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
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "metadata": m.metadata,
                "created_at": m.created_at,
            }
            for m in messages
        ],
    }


def _referenced_attachments(messages) -> set[str]:
    ids: set[str] = set()
    for m in messages:
        for image in (m.metadata or {}).get("images") or []:
            ref = image.get("ref") if isinstance(image, dict) else image
            if isinstance(ref, str) and ref.startswith(ATTACHMENT_PREFIX):
                ids.add(ref[len(ATTACHMENT_PREFIX):])
    return ids


@router.get("/{session_id}/attachments/{attachment_id}")
async def get_shared_attachment(
    session_id: str,
    attachment_id: str,
    db: IAgentDatabase = Depends(get_db),
):
    # Only images that appear in the shared transcript: the attachment store is
    # global and content-addressed, so serving arbitrary ids would leak images
    # from unshared sessions to anyone who knows (or guesses) a hash.
    _, messages = await _shared_messages(session_id, db)
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
