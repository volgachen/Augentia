"""Join a fork-dispatch branch's turn back into the session it came from.

POST /sessions/{id}/fork-dispatch branches the session once per message
(borrowing its history, see app.core.borrowed) and runs each message in its
own branch, concurrently. When a branch's first turn ends, its result is
appended to the original session as one summary row plus a turn_end mark,
so the original continues as if that work were one completed turn:

- the summary row is a user row named branch_summary (metadata.cagent_name),
  carrying metadata.joined_from = {session_id, turn_id, summary_id, task}.
  Its content is what the original session's model sees: the task and the
  result, through config.branch_join_template.
- the branch's own rows are not copied. Expanding the summary in the UI
  reads them from the branch (the originals the branch's turn_summary
  replaced, or the whole turn when it has none).

What the result is: the branch's turn_summary for that turn when turn
compression ran, else its last model reply; a failed turn joins a note
saying so. A turn someone stopped (cancelled / interrupted) has nothing to
report: its summary row is still written, so the branch counts as joined and
a repeat finds it, but it is soft-deleted at once (metadata.dropped = the
stop reason) and gets no turn_end, so the target's model never sees it.

The join runs on the original session's runner queue, between its turns, so
it never lands inside a turn. Branches record join_into_session_id; once
joined, joined_message_id. A pending join (backend restarted in between) is
redone by join_pending_branches at startup. A join is idempotent: a summary
for the same branch turn already in the target is reused.
"""

from __future__ import annotations

import json
import logging
import uuid

from app.core.borrowed import is_borrowed
from app.core.compressed import NAME_KEY, mark_payload
from app.db.interface import IAgentDatabase
from app.models.domain import Message, MessageRole, Session

logger = logging.getLogger("augentia.branch_join")

BRANCH_SUMMARY = "branch_summary"
JOINED_FROM = "joined_from"

DEFAULT_TEMPLATE = (
    "（以下是并行分支处理的一项任务及其结果）\n"
    "任务：{task}\n"
    "结果：{result}"
)
FAILED_RESULT = "（这项任务没有完成：{reason}）"
# Stop reasons of a turn a person stopped: joined but kept out of context.
DROPPED_STOP_REASONS = ("cancelled", "interrupted")
DROPPED = "dropped"


def is_branch_summary(message: Message) -> bool:
    return (message.role == MessageRole.USER
            and (message.metadata or {}).get(NAME_KEY) == BRANCH_SUMMARY)


def _turn_end(message: Message) -> dict | None:
    payload = mark_payload(message)
    if payload and payload.get("name") == "turn_end":
        return payload
    return None


def _assistant_text(message: Message) -> str:
    try:
        payload = json.loads(message.content)
    except (ValueError, TypeError):
        return message.content or ""
    if isinstance(payload, dict):
        return str(payload.get("content") or "")
    return message.content or ""


def first_turn(rows: list[Message]) -> tuple[list[Message], dict | None]:
    """The branch's own rows of its first turn (through its turn_end, deleted
    ones included) and that turn_end payload; payload None while it runs."""
    own = [m for m in rows if not is_borrowed(m)]
    turn: list[Message] = []
    for m in own:
        turn.append(m)
        payload = _turn_end(m)
        if payload is not None:
            return turn, payload
    return turn, None


def turn_result(turn: list[Message], end: dict) -> tuple[str, str | None, bool]:
    """(result text, turn_summary id or None, ok) for a finished first turn."""
    stop = (end.get("metadata") or {}).get("stop_reason")
    if stop not in ("completed", "step_limit"):
        error = (end.get("metadata") or {}).get("error") or {}
        reason = error.get("message") or stop or "unknown"
        return FAILED_RESULT.format(reason=reason), None, False
    summary = next((m for m in turn if m.deleted_at is None and m.role == MessageRole.USER
                    and (m.metadata or {}).get(NAME_KEY) == "turn_summary"), None)
    if summary is not None:
        return summary.content, summary.id, True
    replies = [m for m in turn if m.role == MessageRole.AGENT and m.deleted_at is None
               and _assistant_text(m).strip()]
    if replies:
        return _assistant_text(replies[-1]), None, True
    return "（没有产出文字结果）", None, True


def turn_task(turn: list[Message]) -> str:
    """The user input that started the turn (the dispatched message)."""
    first = next((m for m in turn if m.role == MessageRole.USER
                  and not (m.metadata or {}).get(NAME_KEY)), None)
    return first.content if first is not None else ""


def render(template: str | None, task: str, result: str) -> str:
    template = template or DEFAULT_TEMPLATE
    try:
        return template.format(task=task, result=result)
    except (KeyError, IndexError, ValueError):
        logger.warning("bad branch_join_template; using the default")
        return DEFAULT_TEMPLATE.format(task=task, result=result)


async def write_join(db: IAgentDatabase, branch: Session, *, template: str | None = None) -> Message | None:
    """Append the branch's first-turn result to its join target. Returns the
    summary row, or None while that turn is still running. Idempotent."""
    target_id = branch.join_into_session_id
    if not target_id:
        return None
    target_rows = await db.get_own_messages(target_id, include_deleted=True)
    rows = await db.get_messages(branch.id, include_deleted=True, include_deleted_session=True)
    turn, end = first_turn(rows)
    if end is None:
        return None
    turn_id = (end.get("metadata") or {}).get("turn_id")
    existing = next((m for m in target_rows if is_branch_summary(m)
                     and (m.metadata.get(JOINED_FROM) or {}).get("session_id") == branch.id
                     and (m.metadata.get(JOINED_FROM) or {}).get("turn_id") == turn_id), None)
    if existing is not None:
        if branch.joined_message_id != existing.id:
            await db.set_joined_message(branch.id, existing.id)
        return existing
    result, summary_id, succeeded = turn_result(turn, end)
    task = turn_task(turn)
    stop = (end.get("metadata") or {}).get("stop_reason")
    joined_from = {
        "session_id": branch.id,
        "turn_id": turn_id,
        "summary_id": summary_id,
        "task": task,
        "ok": succeeded,
    }
    if stop in DROPPED_STOP_REASONS:
        # Stopped by someone: record the join, keep it out of the target's
        # context (soft-deleted, no turn_end, so no turn is counted either).
        row = await db.add_message(
            target_id, MessageRole.USER, render(template, task, result),
            metadata={NAME_KEY: BRANCH_SUMMARY, JOINED_FROM: joined_from, DROPPED: stop},
        )
        await db.soft_delete_messages(target_id, [row.id], metadata={DROPPED: stop})
        await db.set_joined_message(branch.id, row.id)
        logger.info("dropped %s branch=%s turn=%s (not joined into session=%s context) row=%s",
                    stop, branch.id, turn_id, target_id, row.id)
        return row
    row = await db.add_message(
        target_id, MessageRole.USER, render(template, task, result),
        metadata={NAME_KEY: BRANCH_SUMMARY, JOINED_FROM: joined_from},
    )
    # One completed turn in the target, so turn counting, compression
    # boundaries and the next turn all treat the joined work as finished.
    await db.add_message(target_id, MessageRole.MARK, json.dumps({
        "name": "turn_end",
        "metadata": {
            "turn_id": f"join_{uuid.uuid4().hex}", "stop_reason": "completed",
            "turn_compression_applied": False, "num_tokens": None,
            "token_count_is_estimate": True, "token_count_error": None, "usage": [],
            JOINED_FROM: {"session_id": branch.id, "turn_id": turn_id},
        },
        "content": "",
    }, ensure_ascii=False))
    await db.set_joined_message(branch.id, row.id)
    logger.info("joined branch=%s turn=%s into session=%s row=%s ok=%s",
                branch.id, turn_id, target_id, row.id, succeeded)
    return row


async def joined_turn_rows(db: IAgentDatabase, summary: Message) -> list[Message]:
    """The branch rows a branch_summary stands for (for expanding it).

    The originals its turn_summary replaced when there is one, else the
    branch's whole first turn; marks left out. Empty if the branch is gone.
    """
    joined = (summary.metadata or {}).get(JOINED_FROM) or {}
    branch_id = joined.get("session_id")
    if not branch_id:
        return []
    try:
        # A deleted branch's rows are still there (soft delete): keep the
        # joined result expandable after the branch is cleaned up.
        branch = await db.get_session(branch_id, include_deleted=True)
        rows = await db.get_messages(branch_id, include_deleted=True, include_deleted_session=True)
    except KeyError:
        return []
    if branch.deleted_at is not None:
        # Deleting a session soft-deletes its live rows at the same instant;
        # show those as they were (rows deleted earlier keep their reason).
        rows = [_undeleted(m) if m.deleted_at == branch.deleted_at else m for m in rows]
    summary_id = joined.get("summary_id")
    if summary_id:
        covered = [m for m in rows if (m.metadata or {}).get("compressed_by") == summary_id]
        if covered:
            return [m for m in covered if m.role != MessageRole.MARK]
    turn, _ = first_turn(rows)
    return [m for m in turn if m.role != MessageRole.MARK
            and (m.deleted_at is None or (m.metadata or {}).get("compressed_by"))]


async def joined_result_for(db: IAgentDatabase, rows: list[Message], message_id: str) -> Message | None:
    """The branch_summary among `rows` (one session's, deleted ones included)
    whose expanded turn shows `message_id`, a row of a joined branch.

    Looks the row up by id to learn its branch, then only that branch's
    results are checked: the cost doesn't grow with the number of branches.
    Only rows of branches joined into this session directly; a branch of a
    branch isn't followed.
    """
    try:
        row = await db.get_message(message_id)
    except KeyError:
        return None
    for summary in rows:
        if not is_branch_summary(summary):
            continue
        if ((summary.metadata or {}).get(JOINED_FROM) or {}).get("session_id") != row.session_id:
            continue
        shown = await joined_turn_rows(db, summary)
        if any(m.id == message_id for m in shown):
            return summary
    return None


def _undeleted(message: Message) -> Message:
    return message.model_copy(update={"deleted_at": None})
