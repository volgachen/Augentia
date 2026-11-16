"""Dispatch mode: a session whose messages each run in a new branch.

Turned on and off with PUT /sessions/{id}/dispatching (Session.dispatch_mode,
stored). While on, POST /messages doesn't run a turn in the session: it
branches the session (borrowing its history) and runs the message there; when
that turn ends its result is joined back (app.core.branch_join). Messages run
concurrently, each seeing the session's history as of when it arrived.

The status a client sees is DISPATCHING while the mode is on and the session
itself is idle (never stored: the stored status keeps saying what the session
itself is doing, so turns and compression can't overwrite the switch).
pending_joins, reported for every session, counts branches whose results
haven't been joined back yet (dispatch mode or a one-off fork-dispatch).
"""

from __future__ import annotations

from app.db.interface import IAgentDatabase
from app.models.domain import Session, SessionStatus

# A session doing its own work (RUNNING / WAITING_CONFIRM / ERROR) reports
# that; DISPATCHING only stands in for an idle status.
_IDLE = (SessionStatus.WAITING_USER, SessionStatus.COMPLETED, SessionStatus.INITIALIZING)

MODE_DETAIL = (
    "session is in dispatch mode: messages run in new branches (POST /messages); "
    "turn dispatch mode off to {action}"
)
PENDING_DETAIL = "{n} dispatched branch(es) not joined back yet; wait until they are"


def with_pending(session: Session, pending: int) -> Session:
    """A copy reporting `pending` joins, and DISPATCHING when the mode is on."""
    session = session.model_copy(update={"pending_joins": pending})
    if session.dispatch_mode and session.status in _IDLE:
        session.status = SessionStatus.DISPATCHING
    return session


async def view(db: IAgentDatabase, session: Session) -> Session:
    return with_pending(session, (await db.pending_joins()).get(session.id, 0))


async def view_all(db: IAgentDatabase, sessions: list[Session]) -> list[Session]:
    counts = await db.pending_joins()
    return [with_pending(s, counts.get(s.id, 0)) for s in sessions]


async def pending_for(db: IAgentDatabase, session_id: str) -> int:
    return (await db.pending_joins()).get(session_id, 0)
