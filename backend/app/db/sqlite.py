from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.core.borrowed import aware
from app.db.interface import IAgentDatabase, _UNSET
from app.db.seed_agents import SEED_AGENT_ROWS
from app.models.domain import (
    AgentTemplate,
    AgentType,
    Message,
    MessageRole,
    PluginServiceLog,
    PluginServiceRun,
    Session,
    SessionStatus,
    SystemPrompt,
    Task,
    TaskStatus,
)
from app.plugins.events import PluginEvent, get_plugin_event_bus


def _dt(value: datetime | str | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None



# agents/sessions store system_prompt_id; reads join the version's text in.
_AGENT_SELECT = (
    "SELECT a.*, p.content AS prompt_text FROM agents a "
    "LEFT JOIN system_prompts p ON p.id = a.system_prompt_id"
)
_SESSION_SELECT = (
    "SELECT s.*, p.content AS prompt_text FROM sessions s "
    "LEFT JOIN system_prompts p ON p.id = s.system_prompt_id"
)


def _system_prompt(row: sqlite3.Row) -> SystemPrompt:
    return SystemPrompt(id=row["id"], content=row["content"], previous_id=row["previous_id"],
                        created_at=_dt(row["created_at"]))


def _agent(row: sqlite3.Row) -> AgentTemplate:
    return AgentTemplate(
        id=row["id"], name=row["name"], description=row["description"],
        agent_type=AgentType(row["agent_type"]),
        system_prompt=row["prompt_text"] or "", system_prompt_id=row["system_prompt_id"],
        tool_names=_json(row["tool_names"], []), config=_json(row["config"], {}) or {},
        openai_model=row["openai_model"], openai_base_url=row["openai_base_url"],
        created_at=_dt(row["created_at"]),
    )


def _session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"], agent_id=row["agent_id"], title=row["title"],
        working_dir=row["working_dir"], parent_session_id=row["parent_session_id"],
        branched_from_session_id=row["branched_from_session_id"],
        branched_from_message_id=row["branched_from_message_id"],
        branched_cutoff_at=_dt(row["branched_cutoff_at"]),
        join_into_session_id=row["join_into_session_id"],
        joined_message_id=row["joined_message_id"],
        join_outcome=row["join_outcome"],
        dispatch_mode=bool(row["dispatch_mode"]),
        additional_prompt=row["additional_prompt"],
        additional_prompt_path=row["additional_prompt_path"],
        system_prompt_snapshot=row["prompt_text"], system_prompt_id=row["system_prompt_id"],
        model=row["model"], model_params=_json(row["model_params"], None),
        status=SessionStatus(row["status"]), created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]), last_message_at=_dt(row["last_message_at"]),
        deleted_at=_dt(row["deleted_at"]),
    )


def _message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"], session_id=row["session_id"], role=MessageRole(row["role"]),
        content=row["content"], from_session_id=row["from_session_id"],
        metadata=_json(row["metadata"], {}) or {},
        created_at=_dt(row["created_at"]), deleted_at=_dt(row["deleted_at"]),
    )


def _metadata_json(metadata: dict) -> str | None:
    return json.dumps(metadata, ensure_ascii=False) if metadata else None


def _service_run(row: sqlite3.Row) -> PluginServiceRun:
    return PluginServiceRun(
        id=row["id"], plugin_id=row["plugin_id"], service=row["service"], session_id=row["session_id"],
        status=row["status"], restart_count=row["restart_count"],
        started_at=_dt(row["started_at"]) or datetime.now(timezone.utc), running_at=_dt(row["running_at"]),
        exited_at=_dt(row["exited_at"]), exit_code=row["exit_code"], error=row["error"],
    )


def _service_log(row: sqlite3.Row) -> PluginServiceLog:
    return PluginServiceLog(
        id=row["id"], plugin_id=row["plugin_id"], run_id=row["run_id"], session_id=row["session_id"],
        ts=_dt(row["ts"]) or datetime.now(timezone.utc), level=row["level"], line=row["line"],
    )


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"], task_list_id=row["task_list_id"], subject=row["subject"],
        description=row["description"], active_form=row["active_form"], owner=row["owner"],
        status=TaskStatus(row["status"]), blocks=_json(row["blocks"], []),
        blocked_by=_json(row["blocked_by"], []), metadata=_json(row["metadata"], None),
        created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
    )


class SqliteDatabase(IAgentDatabase):
    """SQLite persistence backend using the real business tables."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()
        self._seed_agents()

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self._conn.close()

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, tuple(params))
        self._conn.commit()
        return cur

    def _row(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, tuple(params)).fetchone()

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, tuple(params)).fetchall())

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS system_prompts (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                previous_id TEXT DEFAULT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                agent_type TEXT NOT NULL,
                system_prompt_id TEXT DEFAULT NULL,
                tool_names TEXT NOT NULL,
                config TEXT DEFAULT NULL,
                openai_model TEXT NOT NULL DEFAULT 'gpt-4o',
                openai_base_url TEXT DEFAULT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                title TEXT DEFAULT NULL,
                working_dir TEXT DEFAULT NULL,
                parent_session_id TEXT DEFAULT NULL,
                branched_from_session_id TEXT DEFAULT NULL,
                branched_from_message_id TEXT DEFAULT NULL,
                branched_cutoff_at TEXT DEFAULT NULL,
                join_into_session_id TEXT DEFAULT NULL,
                joined_message_id TEXT DEFAULT NULL,
                join_outcome TEXT DEFAULT NULL,
                dispatch_mode INTEGER NOT NULL DEFAULT 0,
                additional_prompt TEXT DEFAULT NULL,
                additional_prompt_path TEXT DEFAULT NULL,
                system_prompt_id TEXT DEFAULT NULL,
                model TEXT DEFAULT NULL,
                model_params TEXT DEFAULT NULL,
                status TEXT NOT NULL DEFAULT 'INITIALIZING',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_message_at TEXT DEFAULT NULL,
                deleted_at TEXT DEFAULT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions (agent_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions (parent_session_id);
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                from_session_id TEXT DEFAULT NULL,
                metadata TEXT DEFAULT NULL,
                created_at TEXT NOT NULL,
                deleted_at TEXT DEFAULT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id);
            CREATE TABLE IF NOT EXISTS plugin_service_runs (
                id TEXT PRIMARY KEY,
                plugin_id TEXT NOT NULL,
                service TEXT NOT NULL,
                session_id TEXT DEFAULT NULL,
                status TEXT NOT NULL,
                restart_count INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                running_at TEXT DEFAULT NULL,
                exited_at TEXT DEFAULT NULL,
                exit_code INTEGER DEFAULT NULL,
                error TEXT DEFAULT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_service_runs_plugin_started
                ON plugin_service_runs (plugin_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_service_runs_status ON plugin_service_runs (status);
            CREATE TABLE IF NOT EXISTS plugin_service_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_id TEXT NOT NULL,
                run_id TEXT DEFAULT NULL,
                session_id TEXT DEFAULT NULL,
                ts TEXT NOT NULL,
                level TEXT NOT NULL,
                line TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_service_logs_plugin ON plugin_service_logs (plugin_id, id);
            CREATE INDEX IF NOT EXISTS idx_service_logs_run ON plugin_service_logs (run_id, id);
            CREATE INDEX IF NOT EXISTS idx_service_logs_session ON plugin_service_logs (session_id, id);
            """
        )
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_list_id TEXT NOT NULL,
                id TEXT NOT NULL,
                subject TEXT NOT NULL,
                description TEXT NOT NULL,
                active_form TEXT DEFAULT NULL,
                owner TEXT DEFAULT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                blocks TEXT NOT NULL,
                blocked_by TEXT NOT NULL,
                metadata TEXT DEFAULT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (task_list_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_list ON tasks (task_list_id);
            CREATE TABLE IF NOT EXISTS task_counters (
                task_list_id TEXT PRIMARY KEY,
                next_id INTEGER NOT NULL
            );
            """
        )
        self._ensure_schema_columns()
        self._conn.commit()

    def _ensure_schema_columns(self) -> None:
        session_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(sessions)")
        }
        self._migrate_system_prompts(session_columns)
        for column in ("model", "model_params", "branched_from_session_id",
                       "branched_from_message_id", "branched_cutoff_at",
                       "join_into_session_id", "joined_message_id", "join_outcome"):
            if column not in session_columns:
                self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT DEFAULT NULL")
        if "dispatch_mode" not in session_columns:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN dispatch_mode INTEGER NOT NULL DEFAULT 0")

        # v1 plugin tables (instances / runs / logs), replaced by the
        # plugin_service_* tables; they only held test data.
        for table in ("plugin_logs", "plugin_runs", "plugin_instances"):
            self._conn.execute(f"DROP TABLE IF EXISTS {table}")

        message_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(messages)")
        }
        if "metadata" not in message_columns:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN metadata TEXT DEFAULT NULL"
            )

    def _migrate_system_prompts(self, session_columns: set[str]) -> None:
        """Move inline prompt text into system_prompts (one first version per
        agent / session), then drop the old text columns.

        For databases created before system_prompts: agents.system_prompt and
        sessions.system_prompt_snapshot become system_prompt_id. Runs once, in
        one transaction. Needs SQLite 3.35+ (ALTER TABLE ... DROP COLUMN).
        """
        agent_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(agents)")}
        moves = [
            (table, old_column, columns)
            for table, old_column, columns in (
                ("agents", "system_prompt", agent_columns),
                ("sessions", "system_prompt_snapshot", session_columns),
            )
            if "system_prompt_id" not in columns or old_column in columns
        ]
        if not moves:
            return
        with self._conn:
            for table, old_column, columns in moves:
                if "system_prompt_id" not in columns:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN system_prompt_id TEXT DEFAULT NULL")
                if old_column not in columns:
                    continue
                rows = self._conn.execute(
                    f"SELECT id, {old_column} AS text, created_at FROM {table} "
                    f"WHERE {old_column} IS NOT NULL AND system_prompt_id IS NULL"
                ).fetchall()
                for row in rows:
                    prompt_id = self._insert_prompt(row["text"], None, row["created_at"])
                    self._conn.execute(f"UPDATE {table} SET system_prompt_id = ? WHERE id = ?",
                                       (prompt_id, row["id"]))
                self._conn.execute(f"ALTER TABLE {table} DROP COLUMN {old_column}")

    def _insert_prompt(self, content: str, previous_id: str | None, created_at: str | None = None) -> str:
        """Store one version (no commit: callers own the transaction)."""
        prompt_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO system_prompts (id, content, previous_id, created_at) VALUES (?, ?, ?, ?)",
            (prompt_id, content, previous_id, created_at or _iso(datetime.now(timezone.utc))),
        )
        return prompt_id

    def _new_version(self, table: str, row_id: str, content: str) -> None:
        """Point an agents/sessions row at a new version of its prompt chained
        to the current one; nothing changes when the text is the same."""
        current = self._conn.execute(
            f"SELECT t.system_prompt_id, p.content FROM {table} t "
            f"LEFT JOIN system_prompts p ON p.id = t.system_prompt_id WHERE t.id = ?",
            (row_id,),
        ).fetchone()
        if current is None:
            raise KeyError(row_id)
        if current["system_prompt_id"] and current["content"] == content:
            return
        with self._conn:
            prompt_id = self._insert_prompt(content, current["system_prompt_id"])
            self._conn.execute(f"UPDATE {table} SET system_prompt_id = ? WHERE id = ?", (prompt_id, row_id))

    async def get_system_prompt(self, prompt_id: str) -> SystemPrompt:
        row = self._row("SELECT * FROM system_prompts WHERE id = ?", (prompt_id,))
        if row is None:
            raise KeyError(f"System prompt '{prompt_id}' not found")
        return _system_prompt(row)

    def _seed_agents(self) -> None:
        now = _iso(datetime.now(timezone.utc))
        for agent in SEED_AGENT_ROWS:
            if self._row("SELECT 1 FROM agents WHERE id = ?", (agent["id"],)):
                continue
            with self._conn:
                prompt_id = self._insert_prompt(agent["system_prompt"], None, now)
                self._conn.execute(
                    """INSERT INTO agents
                       (id, name, description, agent_type, system_prompt_id, tool_names,
                        config, openai_model, openai_base_url, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        agent["id"], agent["name"], agent["description"],
                        agent["agent_type"], prompt_id,
                        _dump(agent["tool_names"]), _dump(agent.get("config", {})),
                        agent["openai_model"], agent["openai_base_url"], now,
                    ),
                )

    async def list_agents(self) -> list[AgentTemplate]:
        return [_agent(r) for r in self._rows(f"{_AGENT_SELECT} ORDER BY a.created_at")]

    async def get_agent(self, agent_id: str) -> AgentTemplate:
        row = self._row(f"{_AGENT_SELECT} WHERE a.id = ?", (agent_id,))
        if row is None:
            raise KeyError(f"Agent '{agent_id}' not found")
        return _agent(row)

    async def create_agent(self, template: AgentTemplate) -> AgentTemplate:
        with self._conn:
            prompt_id = self._insert_prompt(template.system_prompt, None, _iso(template.created_at))
            self._conn.execute(
                """INSERT INTO agents
                   (id, name, description, agent_type, system_prompt_id, tool_names,
                    config, openai_model, openai_base_url, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (template.id, template.name, template.description, template.agent_type.value,
                 prompt_id, _dump(template.tool_names), _dump(template.config),
                 template.openai_model, template.openai_base_url, _iso(template.created_at)),
            )
        return template.model_copy(update={"system_prompt_id": prompt_id})

    async def update_agent(self, agent_id: str, **kwargs: Any) -> AgentTemplate:
        updates = {k: v for k, v in kwargs.items() if v is not None}
        await self.get_agent(agent_id)
        prompt = updates.pop("system_prompt", None)
        if prompt is not None:
            self._new_version("agents", agent_id, prompt)
        if "tool_names" in updates:
            updates["tool_names"] = _dump(updates["tool_names"])
        if "config" in updates:
            updates["config"] = _dump(updates["config"])
        if updates:
            clause = ", ".join(f"{k} = ?" for k in updates)
            self._execute(f"UPDATE agents SET {clause} WHERE id = ?", [*updates.values(), agent_id])
        return await self.get_agent(agent_id)

    async def delete_agent(self, agent_id: str) -> None:
        await self.get_agent(agent_id)
        self._execute("DELETE FROM agents WHERE id = ?", (agent_id,))

    async def create_session(self, agent_id: str, working_dir: str | None = None, **kwargs: Any) -> Session:
        agent = await self.get_agent(agent_id)
        session = Session(
            id=kwargs.get("session_id") or str(uuid.uuid4()),
            agent_id=agent_id,
            title=kwargs.get("title"),
            working_dir=working_dir,
            parent_session_id=kwargs.get("parent_session_id"),
            additional_prompt=kwargs.get("additional_prompt"),
            additional_prompt_path=kwargs.get("additional_prompt_path"),
            model=kwargs.get("model"),
            model_params=kwargs.get("model_params"),
            branched_from_session_id=kwargs.get("branched_from_session_id"),
            branched_from_message_id=kwargs.get("branched_from_message_id"),
            branched_cutoff_at=kwargs.get("branched_cutoff_at"),
            join_into_session_id=kwargs.get("join_into_session_id"),
        )
        if not session.title:
            session.title = f"{agent.name} · {session.created_at:%H:%M}"
        with self._conn:
            prompt_id = kwargs.get("system_prompt_id")
            if kwargs.get("system_prompt_snapshot") is not None:
                prompt_id = self._insert_prompt(
                    kwargs["system_prompt_snapshot"], None, _iso(session.created_at))
            self._conn.execute(
                """INSERT INTO sessions
                   (id, agent_id, title, working_dir, parent_session_id,
                    branched_from_session_id, branched_from_message_id, branched_cutoff_at,
                    join_into_session_id, additional_prompt, additional_prompt_path,
                    system_prompt_id, model, model_params, status, created_at, updated_at,
                    last_message_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session.id, session.agent_id, session.title, session.working_dir,
                 session.parent_session_id, session.branched_from_session_id,
                 session.branched_from_message_id, _iso(session.branched_cutoff_at),
                 session.join_into_session_id,
                 session.additional_prompt, session.additional_prompt_path,
                 prompt_id, session.model, _dump(session.model_params), session.status.value, _iso(session.created_at),
                 _iso(session.updated_at), _iso(session.last_message_at)),
            )
        return await self.get_session(session.id)

    async def get_session(self, session_id: str, *, include_deleted: bool = False) -> Session:
        live = "" if include_deleted else " AND s.deleted_at IS NULL"
        row = self._row(f"{_SESSION_SELECT} WHERE s.id = ?{live}", (session_id,))
        if row is None:
            raise KeyError(f"Session '{session_id}' not found")
        return _session(row)

    async def update_session_system_prompt_snapshot(
        self,
        session_id: str,
        system_prompt_snapshot: str,
    ) -> Session:
        await self.get_session(session_id)
        self._new_version("sessions", session_id, system_prompt_snapshot)
        self._execute("UPDATE sessions SET updated_at = ? WHERE id = ?",
                      (_iso(datetime.now(timezone.utc)), session_id))
        return await self.get_session(session_id)

    async def update_session_title(self, session_id: str, title: str) -> Session:
        now = datetime.now(timezone.utc)
        cur = self._execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?", (title, _iso(now), session_id))
        if cur.rowcount == 0:
            raise KeyError(f"Session '{session_id}' not found")
        return await self.get_session(session_id)

    async def update_session_working_dir(self, session_id: str, working_dir: str | None) -> Session:
        now = datetime.now(timezone.utc)
        cur = self._execute(
            "UPDATE sessions SET working_dir = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (working_dir, _iso(now), session_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Session '{session_id}' not found")
        return await self.get_session(session_id)

    async def update_session_model(
        self, session_id: str, model: str | None, model_params: dict | None,
    ) -> Session:
        now = datetime.now(timezone.utc)
        cur = self._execute(
            "UPDATE sessions SET model = ?, model_params = ?, updated_at = ? "
            "WHERE id = ? AND deleted_at IS NULL",
            (model, _dump(model_params), _iso(now), session_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Session '{session_id}' not found")
        return await self.get_session(session_id)

    async def update_session_dispatch_mode(self, session_id: str, enabled: bool) -> Session:
        now = datetime.now(timezone.utc)
        cur = self._execute(
            "UPDATE sessions SET dispatch_mode = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (1 if enabled else 0, _iso(now), session_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Session '{session_id}' not found")
        return await self.get_session(session_id)

    async def pending_joins(self) -> dict[str, int]:
        rows = self._rows(
            "SELECT join_into_session_id AS target, COUNT(*) AS n FROM sessions "
            "WHERE join_into_session_id IS NOT NULL AND joined_message_id IS NULL "
            "AND deleted_at IS NULL GROUP BY join_into_session_id")
        return {r["target"]: r["n"] for r in rows}

    async def set_joined_message(self, session_id: str, message_id: str,
                                 outcome: str | None = None) -> Session:
        cur = self._execute(
            "UPDATE sessions SET joined_message_id = ?, "
            "join_outcome = COALESCE(?, join_outcome) WHERE id = ?",
            (message_id, outcome, session_id))
        if cur.rowcount == 0:
            raise KeyError(f"Session '{session_id}' not found")
        return await self.get_session(session_id, include_deleted=True)

    async def failed_joins(self) -> dict[str, int]:
        rows = self._rows(
            "SELECT join_into_session_id AS target, COUNT(*) AS n FROM sessions "
            "WHERE join_into_session_id IS NOT NULL AND join_outcome = 'failed' "
            "AND deleted_at IS NULL GROUP BY join_into_session_id")
        return {r["target"]: r["n"] for r in rows}

    async def ack_failed_joins(self, session_id: str) -> int:
        cur = self._execute(
            "UPDATE sessions SET join_outcome = 'failed_acked' "
            "WHERE join_into_session_id = ? AND join_outcome = 'failed'", (session_id,))
        return cur.rowcount

    async def update_session_parent(self, session_id: str, parent_session_id: str | None) -> Session:
        now = datetime.now(timezone.utc)
        cur = self._execute(
            "UPDATE sessions SET parent_session_id = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (parent_session_id, _iso(now), session_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Session '{session_id}' not found")
        return await self.get_session(session_id)

    async def list_sessions(self) -> list[Session]:
        rows = self._rows(f"{_SESSION_SELECT} WHERE s.deleted_at IS NULL ORDER BY s.created_at")
        return [_session(r) for r in rows]

    async def update_session_status(self, session_id: str, status: SessionStatus) -> Session:
        now = datetime.now(timezone.utc)
        cur = self._execute(
            "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
            (status.value, _iso(now), session_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Session '{session_id}' not found")
        return await self.get_session(session_id)

    async def soft_delete_session(self, session_id: str) -> None:
        now = datetime.now(timezone.utc)
        cur = self._execute(
            "UPDATE sessions SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (_iso(now), _iso(now), session_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Session '{session_id}' not found")
        self._execute(
            "UPDATE messages SET deleted_at = ? WHERE session_id = ? AND deleted_at IS NULL",
            (_iso(now), session_id),
        )

    async def add_message(self, session_id: str, role: MessageRole, content: str, **kwargs: Any) -> Message:
        await self.get_session(session_id)
        message = Message(
            session_id=session_id, role=role, content=content,
            from_session_id=kwargs.get("from_session_id"),
            metadata=dict(kwargs.get("metadata") or {}),
        )
        if kwargs.get("created_at") is not None:
            message.created_at = aware(kwargs["created_at"]).astimezone(timezone.utc)
        self._execute(
            """INSERT INTO messages
               (id, session_id, role, content, from_session_id, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (message.id, message.session_id, message.role.value, message.content,
             message.from_session_id, _metadata_json(message.metadata), _iso(message.created_at)),
        )
        # Only forward: a row placed earlier in time (created_at=) isn't the
        # latest activity.
        self._execute(
            "UPDATE sessions SET last_message_at = ? WHERE id = ? "
            "AND (last_message_at IS NULL OR last_message_at < ?)",
            (_iso(message.created_at), session_id, _iso(message.created_at)))
        await get_plugin_event_bus().publish(PluginEvent(
            type="message.created",
            source=message.from_session_id or "augentia",
            data=message.model_dump(mode="json"),
        ))
        return message

    async def get_message(self, message_id: str) -> Message:
        row = self._row("SELECT * FROM messages WHERE id = ?", (message_id,))
        if row is None:
            raise KeyError(f"Message '{message_id}' not found")
        return _message(row)

    async def get_own_messages(
        self, session_id: str, *, include_deleted: bool = False,
        before: datetime | None = None,
    ) -> list[Message]:
        where = "" if include_deleted else " AND deleted_at IS NULL"
        params: list[Any] = [session_id]
        if before is not None:
            # Stored as UTC ISO strings of one shape, so they compare as text.
            where += " AND created_at < ?"
            params.append(_iso(aware(before).astimezone(timezone.utc)))
        rows = self._rows(
            f"SELECT * FROM messages WHERE session_id = ?{where} ORDER BY created_at, id",
            params,
        )
        return [_message(r) for r in rows]

    async def soft_delete_messages_from(self, session_id: str, message_id: str) -> Message:
        await self.get_session(session_id)
        row = self._row(
            "SELECT * FROM messages WHERE session_id = ? AND id = ? AND deleted_at IS NULL",
            (session_id, message_id),
        )
        if row is None:
            raise KeyError(f"Message '{message_id}' not found")
        target = _message(row)
        now = datetime.now(timezone.utc)
        self._execute(
            "UPDATE messages SET deleted_at = ? WHERE session_id = ? AND deleted_at IS NULL AND created_at >= ?",
            (_iso(now), session_id, _iso(target.created_at)),
        )
        latest = self._row(
            "SELECT MAX(created_at) AS value FROM messages WHERE session_id = ? AND deleted_at IS NULL",
            (session_id,),
        )
        self._execute("UPDATE sessions SET last_message_at = ? WHERE id = ?", (latest["value"], session_id))
        return target

    async def soft_delete_messages(
        self, session_id: str, message_ids: list[str], *, metadata: dict | None = None,
    ) -> int:
        await self.get_session(session_id)
        if not message_ids:
            return 0
        now = _iso(datetime.now(timezone.utc))
        changed = 0
        with self._conn:
            for message_id in dict.fromkeys(message_ids):
                row = self._conn.execute(
                    "SELECT metadata FROM messages WHERE session_id = ? AND id = ? AND deleted_at IS NULL",
                    (session_id, message_id),
                ).fetchone()
                if row is None:
                    continue
                merged = {**(_json(row["metadata"], {}) or {}), **(metadata or {})}
                self._conn.execute(
                    "UPDATE messages SET deleted_at = ?, metadata = ? WHERE session_id = ? AND id = ?",
                    (now, _metadata_json(merged), session_id, message_id),
                )
                changed += 1
            latest = self._conn.execute(
                "SELECT MAX(created_at) AS value FROM messages WHERE session_id = ? AND deleted_at IS NULL",
                (session_id,),
            ).fetchone()
            self._conn.execute(
                "UPDATE sessions SET last_message_at = ? WHERE id = ?", (latest["value"], session_id))
        return changed

    # ------- v2 plugin service runs / logs -------

    async def create_service_run(self, plugin_id: str, service: str, *, session_id: str | None = None,
                                 restart_count: int = 0) -> PluginServiceRun:
        run = PluginServiceRun(plugin_id=plugin_id, service=service, session_id=session_id,
                               restart_count=restart_count)
        self._execute(
            "INSERT INTO plugin_service_runs "
            "(id, plugin_id, service, session_id, status, restart_count, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run.id, plugin_id, service, session_id, run.status, restart_count, _iso(run.started_at)),
        )
        return run

    async def update_service_run(self, run_id: str, *, status: str | None = None, running_at: Any = _UNSET,
                                 exited_at: Any = _UNSET, exit_code: Any = _UNSET,
                                 error: Any = _UNSET) -> PluginServiceRun:
        updates: dict[str, Any] = {}
        if status is not None:
            updates["status"] = status
        for key, value in (("running_at", running_at), ("exited_at", exited_at)):
            if value is not _UNSET:
                updates[key] = _iso(value)
        for key, value in (("exit_code", exit_code), ("error", error)):
            if value is not _UNSET:
                updates[key] = value
        if updates:
            clause = ", ".join(f"{k} = ?" for k in updates)
            self._execute(f"UPDATE plugin_service_runs SET {clause} WHERE id = ?", [*updates.values(), run_id])
        row = self._row("SELECT * FROM plugin_service_runs WHERE id = ?", (run_id,))
        if row is None:
            raise KeyError(f"Service run '{run_id}' not found")
        return _service_run(row)

    async def list_service_runs(self, plugin_id: str, *, service: str | None = None,
                                session_id: str | None = None, limit: int = 100) -> list[PluginServiceRun]:
        clauses, values = ["plugin_id = ?"], [plugin_id]
        if service is not None:
            clauses.append("service = ?")
            values.append(service)
        if session_id is not None:
            clauses.append("session_id = ?")
            values.append(session_id)
        rows = self._rows(
            f"SELECT * FROM plugin_service_runs WHERE {' AND '.join(clauses)} "
            "ORDER BY started_at DESC LIMIT ?", [*values, limit])
        return [_service_run(r) for r in rows]

    async def interrupt_active_service_runs(self, error: str) -> int:
        cur = self._execute(
            "UPDATE plugin_service_runs SET status = 'errored', error = ?, exited_at = ? "
            "WHERE status IN ('starting', 'running', 'stopping')",
            (error, _iso(datetime.now(timezone.utc))))
        return cur.rowcount

    async def add_service_log(self, plugin_id: str, line: str, *, level: str = "info",
                              run_id: str | None = None, session_id: str | None = None) -> PluginServiceLog:
        log = PluginServiceLog(plugin_id=plugin_id, run_id=run_id, session_id=session_id, level=level, line=line)
        cur = self._execute(
            "INSERT INTO plugin_service_logs (plugin_id, run_id, session_id, ts, level, line) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (plugin_id, run_id, session_id, _iso(log.ts), level, line))
        log.id = cur.lastrowid
        return log

    async def list_service_logs(self, plugin_id: str, *, run_id: str | None = None,
                                session_id: str | None = None, limit: int = 500) -> list[PluginServiceLog]:
        clauses, values = ["plugin_id = ?"], [plugin_id]
        if run_id is not None:
            clauses.append("run_id = ?")
            values.append(run_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            values.append(session_id)
        rows = self._rows(
            f"SELECT * FROM plugin_service_logs WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
            [*values, limit])
        return [_service_log(r) for r in reversed(rows)]

    async def prune_service_logs(self, plugin_id: str, keep: int) -> int:
        cur = self._execute(
            "DELETE FROM plugin_service_logs WHERE plugin_id = ? AND id NOT IN "
            "(SELECT id FROM plugin_service_logs WHERE plugin_id = ? ORDER BY id DESC LIMIT ?)",
            (plugin_id, plugin_id, keep))
        return cur.rowcount

    async def _next_task_id(self, task_list_id: str) -> int:
        row = self._row("SELECT next_id FROM task_counters WHERE task_list_id = ?", (task_list_id,))
        if row is None:
            self._execute("INSERT INTO task_counters (task_list_id, next_id) VALUES (?, ?)", (task_list_id, 2))
            return 1
        next_id = int(row["next_id"])
        self._execute("UPDATE task_counters SET next_id = ? WHERE task_list_id = ?", (next_id + 1, task_list_id))
        return next_id

    async def create_task(self, task_list_id: str, subject: str, description: str, **kwargs: Any) -> Task:
        task = Task(id=str(await self._next_task_id(task_list_id)), task_list_id=task_list_id,
                    subject=subject, description=description,
                    active_form=kwargs.get("active_form"), metadata=kwargs.get("metadata"))
        self._execute(
            """INSERT INTO tasks
               (task_list_id, id, subject, description, active_form, owner, status,
                blocks, blocked_by, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task.task_list_id, task.id, task.subject, task.description, task.active_form,
             task.owner, task.status.value, _dump(task.blocks), _dump(task.blocked_by),
             _dump(task.metadata), _iso(task.created_at), _iso(task.updated_at)),
        )
        return task

    async def get_task(self, task_list_id: str, task_id: str) -> Task | None:
        row = self._row("SELECT * FROM tasks WHERE task_list_id = ? AND id = ?", (task_list_id, task_id))
        return _task(row) if row else None

    async def list_tasks(self, task_list_id: str) -> list[Task]:
        rows = self._rows("SELECT * FROM tasks WHERE task_list_id = ? ORDER BY CAST(id AS INTEGER)", (task_list_id,))
        return [_task(r) for r in rows]

    async def _write_task(self, task: Task) -> None:
        self._execute(
            """UPDATE tasks SET subject = ?, description = ?, active_form = ?, owner = ?,
               status = ?, blocks = ?, blocked_by = ?, metadata = ?, updated_at = ?
               WHERE task_list_id = ? AND id = ?""",
            (task.subject, task.description, task.active_form, task.owner, task.status.value,
             _dump(task.blocks), _dump(task.blocked_by), _dump(task.metadata), _iso(task.updated_at),
             task.task_list_id, task.id),
        )

    async def update_task(self, task_list_id: str, task_id: str, **kwargs: Any) -> Task | None:
        task = await self.get_task(task_list_id, task_id)
        if task is None:
            return None
        for key in ("subject", "description", "active_form", "status"):
            if kwargs.get(key) is not None:
                setattr(task, key, kwargs[key])
        if kwargs.get("owner", _UNSET) is not _UNSET:
            task.owner = kwargs["owner"]
        if kwargs.get("metadata") is not None:
            merged = dict(task.metadata or {})
            for k, v in kwargs["metadata"].items():
                merged.pop(k, None) if v is None else merged.__setitem__(k, v)
            task.metadata = merged or None
        for other_id in kwargs.get("add_blocks") or []:
            other = await self.get_task(task_list_id, other_id)
            if other is None:
                continue
            if other_id not in task.blocks:
                task.blocks.append(other_id)
            if task_id not in other.blocked_by:
                other.blocked_by.append(task_id)
                other.updated_at = datetime.now(timezone.utc)
                await self._write_task(other)
        for other_id in kwargs.get("add_blocked_by") or []:
            other = await self.get_task(task_list_id, other_id)
            if other is None:
                continue
            if other_id not in task.blocked_by:
                task.blocked_by.append(other_id)
            if task_id not in other.blocks:
                other.blocks.append(task_id)
                other.updated_at = datetime.now(timezone.utc)
                await self._write_task(other)
        task.updated_at = datetime.now(timezone.utc)
        await self._write_task(task)
        return task

    async def delete_task(self, task_list_id: str, task_id: str) -> bool:
        cur = self._execute("DELETE FROM tasks WHERE task_list_id = ? AND id = ?", (task_list_id, task_id))
        if cur.rowcount == 0:
            return False
        for other in await self.list_tasks(task_list_id):
            changed = False
            if task_id in other.blocks:
                other.blocks.remove(task_id)
                changed = True
            if task_id in other.blocked_by:
                other.blocked_by.remove(task_id)
                changed = True
            if changed:
                other.updated_at = datetime.now(timezone.utc)
                await self._write_task(other)
        return True
