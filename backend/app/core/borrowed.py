"""History a branch borrows from the session it was branched from.

A branch made by diverge copies nothing. It records the source session and a
cutoff (branched_cutoff_at, the created_at of the first source row it does not
take): its history is the source's rows created before the cutoff, then its
own rows. Borrowed rows keep their ids, carry metadata.borrowed_from, and are
read-only from the branch (no delete, rewrite or compression there).

They are shown as they were when the branch was made (the branch's
created_at, the snapshot). Rows are never edited after they are written:
only deleted_at is set and a reason merged into metadata (compressed_by /
deleted_by). So the state at the snapshot can be rebuilt (as_of):

- deleted after the snapshot (a later delete, rewrite or compression in the
  source, or the source session deleted): live again, reason dropped;
- deleted before it: stays deleted. One exception: a row compressed under a
  summary that is not among the borrowed rows at all (written at or after
  the cutoff) would vanish with nothing standing in for it, so it is live.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.domain import Message

# metadata key on borrowed rows: the session that owns the row.
BORROWED_FROM = "borrowed_from"
_DELETE_REASONS = ("compressed_by", "deleted_by")


def aware(stamp: datetime | None) -> datetime | None:
    """MySQL DATETIME columns read back naive; they hold UTC."""
    if stamp is None or stamp.tzinfo is not None:
        return stamp
    return stamp.replace(tzinfo=timezone.utc)


def is_borrowed(message: Message) -> bool:
    return bool((message.metadata or {}).get(BORROWED_FROM))


def _revive(message: Message) -> None:
    message.deleted_at = None
    message.metadata = {
        k: v for k, v in (message.metadata or {}).items() if k not in _DELETE_REASONS}


def as_of(rows: list[Message], snapshot: datetime) -> list[Message]:
    """One session's rows from before a cutoff (deleted ones included), as they
    were at `snapshot`. Mutates and returns the given rows; pass copies."""
    snapshot = aware(snapshot)
    present = {m.id for m in rows}
    for m in rows:
        deleted = aware(m.deleted_at)
        if deleted is None:
            continue
        summary_id = (m.metadata or {}).get("compressed_by")
        if deleted > snapshot or (summary_id and summary_id not in present):
            _revive(m)
    return rows
