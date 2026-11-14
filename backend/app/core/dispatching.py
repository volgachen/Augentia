"""DISPATCHING: a session waiting for fork-dispatch results to be joined back.

Not a stored status. The stored one keeps saying what the session itself is
doing; API responses report DISPATCHING on top of an idle status while some
of its fork-dispatch branches haven't been joined back (Session.pending_joins
> 0), and new messages are refused (409) until they all are. Clients that
poll the status (e.g. the heartbeat script) just treat it as busy.
"""

from __future__ import annotations

from app.db.interface import IAgentDatabase
from app.models.domain import Session, SessionStatus

# A session doing its own work reports that instead; DISPATCHING only
# replaces "idle" statuses.
_IDLE = (SessionStatus.WAITING_USER, SessionStatus.COMPLETED, SessionStatus.INITIALIZING)

BUSY_DETAIL = (
    "session is waiting for fork-dispatch results to be joined back "
    "({n} branch(es) still running); send after they are joined"
)


def with_pending(session: Session, pending: int) -> Session:
    """A copy reporting `pending` joins (and DISPATCHING when idle)."""
    session = session.model_copy(update={"pending_joins": pending})
    if pending and session.status in _IDLE:
        session.status = SessionStatus.DISPATCHING
    return session


async def view(db: IAgentDatabase, session: Session) -> Session:
    return with_pending(session, (await db.pending_joins()).get(session.id, 0))


async def view_all(db: IAgentDatabase, sessions: list[Session]) -> list[Session]:
    counts = await db.pending_joins()
    return [with_pending(s, counts.get(s.id, 0)) for s in sessions]


async def pending_for(db: IAgentDatabase, session_id: str) -> int:
    return (await db.pending_joins()).get(session_id, 0)
