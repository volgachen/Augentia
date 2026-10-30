"""Which stored rows a compression summary replaced.

A summary is either a turn/range summary (a user row whose metadata names
cagent_name turn_summary / range_summary and lists replaced_message_ids) or a
whole-history compression_completed mark (every row at or before its
through_message_id boundary). When a summary is saved, the rows it replaced are
soft-deleted with metadata.compressed_by = <summary id>: the transcript then
shows only the summary, and the client fetches the originals on demand.

Framework marks (turn_end, compression_started) are never compressed: CAgent
needs a whole-history summary's turn_end boundary to stay live, and keeping
them all live keeps turn counting unchanged. An earlier compression_completed
mark is covered like any row, so summaries nest.
"""

import json

from app.models.domain import Message, MessageRole

SUMMARY_NAMES = ("turn_summary", "range_summary")
NAME_KEY = "cagent_name"
COMPRESSED_BY = "compressed_by"
# metadata.deleted_by on rows removed with the delete button ("user"), as
# opposed to rows a rewrite discarded or a summary replaced.
DELETED_BY = "deleted_by"


def mark_payload(message: Message) -> dict | None:
    if message.role != MessageRole.MARK:
        return None
    try:
        payload = json.loads(message.content)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def is_completed_mark(message: Message) -> bool:
    payload = mark_payload(message)
    return bool(payload and payload.get("name") == "compression_completed")


def is_summary(message: Message) -> bool:
    if message.role == MessageRole.USER:
        return (message.metadata or {}).get(NAME_KEY) in SUMMARY_NAMES
    return is_completed_mark(message)


def _compressible(message: Message) -> bool:
    return message.role != MessageRole.MARK or is_completed_mark(message)


def covered_ids(live: list[Message], summary: Message) -> list[str]:
    """Ids among `live` (ordered, not deleted) that `summary` replaced.

    A summary copied from another session (diverge) names ids that are not
    here, so it covers nothing.
    """
    if summary.role == MessageRole.USER:
        replaced = set((summary.metadata or {}).get("replaced_message_ids") or [])
        return [m.id for m in live if m.id in replaced and m.id != summary.id and _compressible(m)]
    payload = mark_payload(summary) or {}
    through = (payload.get("metadata") or {}).get("through_message_id")
    end = next((i for i, m in enumerate(live) if m.id == through), None)
    if end is None:
        return []
    return [m.id for m in live[:end + 1] if m.id != summary.id and _compressible(m)]


async def compress_covered(db, session_id: str, summary: Message) -> int:
    """Soft-delete the live rows `summary` replaced, tagging them compressed_by."""
    live = await db.get_messages(session_id)
    ids = covered_ids(live, summary)
    if not ids:
        return 0
    return await db.soft_delete_messages(session_id, ids, metadata={COMPRESSED_BY: summary.id})


def plain_copy_fields(message: Message) -> tuple[MessageRole, str, dict]:
    """(role, content, metadata) for a copy of `message` in another session.

    Copied rows get new ids, so a summary's references (replaced ids or its
    boundary) would point into the source session, and the rows it replaced
    are deleted there and not copied. The copy is therefore no longer a
    summary: its text is kept as an ordinary user message. Other rows are
    copied unchanged.
    """
    metadata = dict(message.metadata or {})
    if message.role == MessageRole.USER and metadata.get(NAME_KEY) in SUMMARY_NAMES:
        return MessageRole.USER, message.content, {
            k: v for k, v in metadata.items()
            if k not in (NAME_KEY, "replaced_message_ids", "through_message_id")
        }
    if is_completed_mark(message):
        payload = mark_payload(message) or {}
        return MessageRole.USER, str(payload.get("content") or ""), {}
    return message.role, message.content, metadata
