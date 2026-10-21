"""CAgent Store protocol bridged onto Augentia's IAgentDatabase.

Row formats stay exactly what the legacy engine persists: agent rows hold
OpenAI-native assistant message JSON in ``content`` (``{"role": "assistant",
"content": ..., "tool_calls": [{"id", "type", "function": {...}}]}``), tool rows
hold native tool message JSON (``{"role": "tool", "tool_call_id", "content"}``),
user rows hold plain text. CAgent's internal tool-call format
(``{"id", "name", "arguments"}`` with dict arguments) is translated at this
boundary, so legacy and cagent engines read and write the same history and a
session can switch engines without migration.

Framework marks (turn_end / compression_started / compression_completed) are
persisted as ``MessageRole.MARK`` rows whose content is JSON
``{"name", "metadata", "content"}``. They round-trip through ``list_messages``
for the framework; the messages router filters them out for clients.

Session status is translated both ways:

    CAgent        DB (SessionStatus)
    RUNNING       RUNNING
    IDLE          WAITING_USER
    FAILED        ERROR
    INTERRUPTED   WAITING_USER  (Augentia cancels turns via asyncio, not status)

Framework ``set_status`` writes also land in an in-memory overlay: a
cooperative interrupt (``INTERRUPTED``) must read back as "INTERRUPTED" within
the same turn even though SessionStatus has no such value. The overlay is
per-store (one store per session adapter) and is refreshed on every
``set_status``; ``get_status`` prefers it, everything else reads the DB.

``list_messages`` repairs incomplete tool history on read (cancelled turns can
leave an assistant tool_call without its result row): missing results are
synthesized as ``[result not recorded]`` and legacy split tool_call/tool rows
are re-paired with synthesized ids — the same repair the legacy adapter's
``restore_history`` performs at rehydrate time, applied continuously.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.db.interface import IAgentDatabase
from app.models.domain import Message, MessageRole, SessionStatus

logger = logging.getLogger("augentia.cagent.store")

# CAgent framework status -> SessionStatus written to the DB.
_STATUS_TO_DB = {
    "RUNNING": SessionStatus.RUNNING,
    "IDLE": SessionStatus.WAITING_USER,
    "FAILED": SessionStatus.ERROR,
    # Augentia cancels turns through task cancellation; a cooperative
    # INTERRUPTED still maps to a user-waiting DB state.
    "INTERRUPTED": SessionStatus.WAITING_USER,
}

# SessionStatus read from the DB -> CAgent framework status.
_STATUS_TO_FRAMEWORK = {
    SessionStatus.INITIALIZING: "IDLE",
    SessionStatus.RUNNING: "RUNNING",
    SessionStatus.WAITING_USER: "IDLE",
    SessionStatus.WAITING_CONFIRM: "RUNNING",
    SessionStatus.COMPLETED: "IDLE",
    SessionStatus.ERROR: "FAILED",
}


def _parse_json(content: str) -> Any:
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None


def _wire_arguments(arguments: Any) -> str:
    # OpenAI wire format carries arguments as a JSON string.
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)


def _internal_arguments(arguments: Any) -> dict:
    if isinstance(arguments, dict):
        return arguments
    parsed = _parse_json(arguments) if isinstance(arguments, str) else None
    return parsed if isinstance(parsed, dict) else {}


def _wire_tool_calls(calls: list[dict]) -> list[dict]:
    return [
        {
            "id": call["id"],
            "type": "function",
            "function": {"name": call["name"], "arguments": _wire_arguments(call.get("arguments"))},
        }
        for call in calls
    ]


class AugentiaStore:
    """One instance per session adapter; methods may be awaited by the framework."""

    def __init__(self, db: IAgentDatabase, session_id: str) -> None:
        self._db = db
        self._session_id = session_id
        # Framework-status overlay, see module docstring.
        self._framework_status: dict[str, str] = {}
        # Injected by the adapter before each turn: attached to the first user
        # row CAgent's pre_run_turn writes (the runner bakes the sender into a
        # "[from-session:X]" prefix; we lift it back into the column).
        self.pending_from_session: str | None = None

    # ------- sessions -------

    async def create_session(self, session_id: str, user_id: str) -> dict:
        # Augentia creates sessions through its routers; the framework never
        # calls this. Present for protocol completeness only.
        raise NotImplementedError("Augentia creates sessions via its own routers")

    async def get_session(self, session_id: str) -> dict:
        session = await self._db.get_session(session_id)
        return {
            "id": session.id,
            "user_id": session.agent_id,
            "status": _STATUS_TO_FRAMEWORK.get(session.status, "IDLE"),
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "deleted_at": session.deleted_at.isoformat() if session.deleted_at else None,
        }

    async def get_status(self, session_id: str) -> str:
        overlay = self._framework_status.get(session_id)
        if overlay is not None:
            return overlay
        session = await self._db.get_session(session_id)
        return _STATUS_TO_FRAMEWORK.get(session.status, "IDLE")

    async def set_status(self, session_id: str, status: str) -> None:
        self._framework_status[session_id] = status
        db_status = _STATUS_TO_DB.get(status)
        if db_status is None:
            # Host-side waiting statuses (e.g. WAITING_CONFIRM) pass through;
            # anything else stays overlay-only.
            try:
                db_status = SessionStatus(status)
            except ValueError:
                return
        await self._db.update_session_status(session_id, db_status)

    # ------- messages -------

    async def list_messages(self, session_id: str, *, include_deleted: bool = False) -> list[dict]:
        # IAgentDatabase.get_messages only returns live rows; include_deleted
        # cannot be honored without an interface extension. It only matters
        # once compression marks reference soft-deleted boundaries (PR2).
        rows = await self._db.get_messages(session_id)
        return self._to_framework_records(rows)

    async def save_messages(self, session_id: str, messages: list[dict]) -> list[dict]:
        saved: list[dict] = []
        for message in messages:
            role = message["role"]
            if role == "user":
                saved.append(await self._save_user(session_id, message))
            elif role == "assistant":
                saved.append(await self._save_assistant(session_id, message))
            elif role == "tool":
                saved.append(await self._save_tool(session_id, message))
            elif role == "mark":
                saved.append(await self._save_mark(session_id, message))
            else:
                raise ValueError(f"Unsupported message role for persistence: {role}")
        return saved

    async def delete_message(self, session_id: str, message_id: str) -> None:
        # IAgentDatabase only offers delete-from-message-onward (diverge);
        # single-row soft delete is not needed by the framework core.
        raise NotImplementedError("Single-message soft delete is not supported by IAgentDatabase")

    # ------- persistence helpers -------

    async def _save_user(self, session_id: str, message: dict) -> dict:
        from_session = message.get("from_session_id")
        if from_session is None and self.pending_from_session is not None:
            from_session = self.pending_from_session
            self.pending_from_session = None
        # CAgent keeps image references in metadata.images; the row's text
        # content stays plain so text-only readers are unaffected.
        metadata = dict(message.get("metadata") or {})
        row = await self._db.add_message(
            session_id, MessageRole.USER, message.get("content", ""),
            from_session_id=from_session,
            metadata=metadata,
        )
        return self._record(row, role="user", content=row.content, metadata=dict(row.metadata))

    async def _save_assistant(self, session_id: str, message: dict) -> dict:
        payload: dict[str, Any] = {
            "role": "assistant",
            "content": message.get("content") or "",
            "refusal": None,
        }
        if message.get("tool_calls"):
            payload["tool_calls"] = _wire_tool_calls(message["tool_calls"])
        row = await self._db.add_message(
            session_id, MessageRole.AGENT, json.dumps(payload, ensure_ascii=False)
        )
        return self._record(
            row, role="assistant", content=payload["content"],
            tool_calls=list(message.get("tool_calls") or []),
        )

    async def _save_tool(self, session_id: str, message: dict) -> dict:
        payload = {
            "role": "tool",
            "tool_call_id": message["tool_call_id"],
            "content": message.get("content", ""),
        }
        # CAgent keeps tool-result image references (and any host metadata)
        # in metadata; the row content stays the OpenAI-native tool JSON.
        metadata = dict(message.get("metadata") or {})
        row = await self._db.add_message(
            session_id, MessageRole.TOOL, json.dumps(payload, ensure_ascii=False),
            metadata=metadata,
        )
        return self._record(
            row, role="tool", content=payload["content"],
            tool_call_id=payload["tool_call_id"], metadata=dict(row.metadata),
        )

    async def _save_mark(self, session_id: str, message: dict) -> dict:
        payload = {
            "name": message["name"],
            "metadata": message.get("metadata") or {},
            "content": message.get("content", ""),
        }
        row = await self._db.add_message(
            session_id, MessageRole.MARK, json.dumps(payload, ensure_ascii=False)
        )
        return self._record(
            row, role="mark", name=payload["name"], content=payload["content"],
            metadata=dict(payload["metadata"]),
        )

    @staticmethod
    def _record(row: Message, **fields: Any) -> dict:
        record = {
            "id": row.id,
            "session_id": row.session_id,
            "role": None,
            "type": None,
            "name": None,
            "content": "",
            "metadata": {},
            "tool_calls": [],
            "tool_call_id": None,
            "from_session_id": row.from_session_id,
            "created_at": row.created_at.isoformat(),
            "deleted_at": None,
        }
        record.update(fields)
        return record

    # ------- read translation + repair -------

    def _to_framework_records(self, rows: list[Message]) -> list[dict]:
        records: list[dict] = []
        synth_seq = 0
        i = 0
        n = len(rows)
        while i < n:
            row = rows[i]
            if row.role == MessageRole.USER:
                records.append(self._record(
                    row, role="user", content=row.content, metadata=dict(row.metadata or {}),
                ))
                i += 1
            elif row.role == MessageRole.AGENT:
                i = self._read_assistant(rows, i, records)
            elif row.role == MessageRole.TOOL_CALL:
                # Legacy split rows: assistant(tool_calls) + synthesized result.
                parsed = _parse_json(row.content)
                name, arguments = "unknown", {}
                if isinstance(parsed, dict):
                    name = parsed.get("name", "unknown")
                    arguments = parsed.get("args", {}) if isinstance(parsed.get("args"), dict) else {}
                call_id = f"restored_call_{synth_seq}"
                synth_seq += 1
                records.append(self._record(
                    row, role="assistant", content="",
                    tool_calls=[{"id": call_id, "name": name, "arguments": arguments}],
                ))
                result = "[result not recorded]"
                if i + 1 < n and rows[i + 1].role == MessageRole.TOOL:
                    result = self._legacy_tool_result(rows[i + 1].content)
                    i += 1
                records.append(self._record(
                    row, role="tool", content=result, tool_call_id=call_id,
                ))
                i += 1
            elif row.role == MessageRole.TOOL:
                parsed = _parse_json(row.content)
                if isinstance(parsed, dict) and parsed.get("role") == "tool" and parsed.get("tool_call_id"):
                    records.append(self._record(
                        row, role="tool", content=parsed.get("content") or "",
                        tool_call_id=parsed["tool_call_id"],
                    ))
                else:
                    # Legacy orphan result with no preceding call: keep it as
                    # plain assistant context so it never dangles.
                    records.append(self._record(
                        row, role="assistant",
                        content=f"[previous tool result] {self._legacy_tool_result(row.content)}",
                    ))
                i += 1
            elif row.role == MessageRole.MARK:
                parsed = _parse_json(row.content)
                if isinstance(parsed, dict) and parsed.get("name"):
                    records.append(self._record(
                        row, role="mark", name=parsed["name"],
                        content=parsed.get("content", ""),
                        metadata=parsed.get("metadata") or {},
                    ))
                i += 1
            else:
                i += 1
        return records

    def _read_assistant(self, rows: list[Message], i: int, records: list[dict]) -> int:
        """Translate one agent row, pairing its native tool results with repair."""
        row = rows[i]
        parsed = _parse_json(row.content)
        if not (isinstance(parsed, dict) and parsed.get("role") == "assistant"):
            records.append(self._record(row, role="assistant", content=row.content))
            return i + 1

        wire_calls = [
            call for call in (parsed.get("tool_calls") or [])
            if isinstance(call, dict) and isinstance(call.get("function"), dict)
        ]
        internal_calls = [
            {
                "id": call.get("id") or f"restored_call_{len(records)}_{index}",
                "name": call["function"].get("name", "unknown"),
                "arguments": _internal_arguments(call["function"].get("arguments")),
            }
            for index, call in enumerate(wire_calls)
        ]
        records.append(self._record(
            row, role="assistant", content=parsed.get("content") or "",
            tool_calls=internal_calls,
        ))

        # Consecutive native tool rows belong to this assistant's calls.
        j = i + 1
        available: dict[str, tuple[Message, str]] = {}
        while j < len(rows) and rows[j].role == MessageRole.TOOL:
            tool_parsed = _parse_json(rows[j].content)
            if not (isinstance(tool_parsed, dict) and tool_parsed.get("role") == "tool"
                    and tool_parsed.get("tool_call_id")):
                break
            available[tool_parsed["tool_call_id"]] = (rows[j], tool_parsed.get("content") or "")
            j += 1

        for call in internal_calls:
            call_id = call["id"]
            if call_id in available:
                tool_row, tool_content = available.pop(call_id)
                records.append(self._record(
                    tool_row, role="tool", content=tool_content, tool_call_id=call_id,
                    metadata=dict(tool_row.metadata or {}),
                ))
            else:
                # Cancelled turn: synthesize the missing paired result.
                records.append(self._record(
                    row, role="tool", content="[result not recorded]", tool_call_id=call_id,
                ))
        # Native tool rows that matched no call are orphans; keep their text as
        # assistant context so the framework never sees an unpaired result.
        for orphan_row, orphan_content in available.values():
            records.append(self._record(
                orphan_row, role="assistant", content=f"[previous tool result] {orphan_content}",
            ))
        return j

    @staticmethod
    def _legacy_tool_result(content: str) -> str:
        parsed = _parse_json(content)
        if isinstance(parsed, dict) and "result" in parsed:
            result = parsed["result"]
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return content
