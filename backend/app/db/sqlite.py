from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.db.interface import IAgentDatabase, _UNSET
from app.db.seed_agents import SEED_AGENT_ROWS
from app.models.domain import (
    AgentTemplate,
    AgentType,
    Message,
    MessageRole,
    PluginInstance,
    PluginLog,
    PluginRun,
    PluginStatus,
    Session,
    SessionStatus,
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



def _agent(row: sqlite3.Row) -> AgentTemplate:
    return AgentTemplate(
        id=row["id"], name=row["name"], description=row["description"],
        agent_type=AgentType(row["agent_type"]), system_prompt=row["system_prompt"],
        tool_names=_json(row["tool_names"], []), config=_json(row["config"], {}) or {},
        openai_model=row["openai_model"], openai_base_url=row["openai_base_url"],
        created_at=_dt(row["created_at"]),
    )


def _session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"], agent_id=row["agent_id"], title=row["title"],
        working_dir=row["working_dir"], parent_session_id=row["parent_session_id"],
        additional_prompt=row["additional_prompt"],
        additional_prompt_path=row["additional_prompt_path"],
        system_prompt_snapshot=row["system_prompt_snapshot"],
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


def _plugin_instance(row: sqlite3.Row) -> PluginInstance:
    return PluginInstance(
        id=row["id"], plugin_id=row["plugin_id"], display_name=row["display_name"],
        status=PluginStatus(row["status"]), config=_json(row["config"], None),
        auto_start=bool(row["auto_start"]), current_run_id=row["current_run_id"],
        created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
    )


def _plugin_run(row: sqlite3.Row) -> PluginRun:
    return PluginRun(
        id=row["id"], plugin_instance_id=row["plugin_instance_id"], plugin_id=row["plugin_id"],
        status=PluginStatus(row["status"]), config_snapshot=_json(row["config_snapshot"], None),
        started_at=_dt(row["started_at"]), running_at=_dt(row["running_at"]),
        exited_at=_dt(row["exited_at"]), exit_code=row["exit_code"], error=row["error"],
        created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
    )


def _plugin_log(row: sqlite3.Row) -> PluginLog:
    return PluginLog(
        id=row["id"], plugin_instance_id=row["plugin_instance_id"],
        plugin_run_id=row["plugin_run_id"], session_id=row["session_id"],
        ts=_dt(row["ts"]), stream=row["stream"], level=row["level"],
        line=row["line"],
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
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                agent_type TEXT NOT NULL,
                system_prompt TEXT NOT NULL,
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
                additional_prompt TEXT DEFAULT NULL,
                additional_prompt_path TEXT DEFAULT NULL,
                system_prompt_snapshot TEXT DEFAULT NULL,
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
            CREATE TABLE IF NOT EXISTS plugin_instances (
                id TEXT PRIMARY KEY,
                plugin_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'stopped',
                config TEXT DEFAULT NULL,
                auto_start INTEGER NOT NULL DEFAULT 0,
                current_run_id TEXT DEFAULT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_plugin_instances_plugin_id ON plugin_instances (plugin_id);
            CREATE INDEX IF NOT EXISTS idx_plugin_instances_status ON plugin_instances (status);
            CREATE INDEX IF NOT EXISTS idx_plugin_instances_auto_start ON plugin_instances (auto_start);
            CREATE INDEX IF NOT EXISTS idx_plugin_instances_current_run ON plugin_instances (current_run_id);
            """
        )
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS plugin_runs (
                id TEXT PRIMARY KEY,
                plugin_instance_id TEXT NOT NULL,
                plugin_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'starting',
                config_snapshot TEXT DEFAULT NULL,
                started_at TEXT DEFAULT NULL,
                running_at TEXT DEFAULT NULL,
                exited_at TEXT DEFAULT NULL,
                exit_code INTEGER DEFAULT NULL,
                error TEXT DEFAULT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (plugin_instance_id) REFERENCES plugin_instances(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_plugin_runs_instance_started
                ON plugin_runs (plugin_instance_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_plugin_runs_plugin_id ON plugin_runs (plugin_id);
            CREATE INDEX IF NOT EXISTS idx_plugin_runs_status ON plugin_runs (status);
            CREATE INDEX IF NOT EXISTS idx_plugin_runs_started_at ON plugin_runs (started_at);
            CREATE TABLE IF NOT EXISTS plugin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_instance_id TEXT NOT NULL,
                plugin_run_id TEXT NOT NULL,
                session_id TEXT DEFAULT NULL,
                ts TEXT NOT NULL,
                stream TEXT NOT NULL,
                level TEXT DEFAULT NULL,
                line TEXT NOT NULL,
                FOREIGN KEY (plugin_instance_id) REFERENCES plugin_instances(id) ON DELETE CASCADE,
                FOREIGN KEY (plugin_run_id) REFERENCES plugin_runs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_plugin_logs_run_ts ON plugin_logs (plugin_run_id, ts);
            CREATE INDEX IF NOT EXISTS idx_plugin_logs_instance_ts ON plugin_logs (plugin_instance_id, ts);
            CREATE INDEX IF NOT EXISTS idx_plugin_logs_stream ON plugin_logs (stream);
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
        if "system_prompt_snapshot" not in session_columns:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN system_prompt_snapshot TEXT DEFAULT NULL"
            )

        plugin_log_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(plugin_logs)")
        }
        if "session_id" not in plugin_log_columns:
            self._conn.execute(
                "ALTER TABLE plugin_logs ADD COLUMN session_id TEXT DEFAULT NULL"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plugin_logs_session_ts "
            "ON plugin_logs (session_id, ts)"
        )

        message_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(messages)")
        }
        if "metadata" not in message_columns:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN metadata TEXT DEFAULT NULL"
            )

    def _seed_agents(self) -> None:
        now = datetime.now(timezone.utc)
        for agent in SEED_AGENT_ROWS:
            self._execute(
                """INSERT OR IGNORE INTO agents
                   (id, name, description, agent_type, system_prompt, tool_names,
                    config, openai_model, openai_base_url, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    agent["id"], agent["name"], agent["description"],
                    agent["agent_type"], agent["system_prompt"],
                    _dump(agent["tool_names"]), _dump(agent.get("config", {})),
                    agent["openai_model"], agent["openai_base_url"], _iso(now),
                ),
            )

    async def list_agents(self) -> list[AgentTemplate]:
        return [_agent(r) for r in self._rows("SELECT * FROM agents ORDER BY created_at")]

    async def get_agent(self, agent_id: str) -> AgentTemplate:
        row = self._row("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if row is None:
            raise KeyError(f"Agent '{agent_id}' not found")
        return _agent(row)

    async def create_agent(self, template: AgentTemplate) -> AgentTemplate:
        self._execute(
            """INSERT INTO agents
               (id, name, description, agent_type, system_prompt, tool_names,
                config, openai_model, openai_base_url, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (template.id, template.name, template.description, template.agent_type.value,
             template.system_prompt, _dump(template.tool_names), _dump(template.config),
             template.openai_model, template.openai_base_url, _iso(template.created_at)),
        )
        return template

    async def update_agent(self, agent_id: str, **kwargs: Any) -> AgentTemplate:
        updates = {k: v for k, v in kwargs.items() if v is not None}
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
            system_prompt_snapshot=kwargs.get("system_prompt_snapshot"),
        )
        if not session.title:
            session.title = f"{agent.name} · {session.created_at:%H:%M}"
        self._execute(
            """INSERT INTO sessions
               (id, agent_id, title, working_dir, parent_session_id, additional_prompt,
                additional_prompt_path, system_prompt_snapshot, status, created_at,
                updated_at, last_message_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session.id, session.agent_id, session.title, session.working_dir,
             session.parent_session_id, session.additional_prompt, session.additional_prompt_path,
             session.system_prompt_snapshot, session.status.value, _iso(session.created_at),
             _iso(session.updated_at), _iso(session.last_message_at)),
        )
        return session

    async def get_session(self, session_id: str) -> Session:
        row = self._row("SELECT * FROM sessions WHERE id = ? AND deleted_at IS NULL", (session_id,))
        if row is None:
            raise KeyError(f"Session '{session_id}' not found")
        return _session(row)

    async def update_session_system_prompt_snapshot(
        self,
        session_id: str,
        system_prompt_snapshot: str,
    ) -> Session:
        now = datetime.now(timezone.utc)
        cur = self._execute(
            "UPDATE sessions SET system_prompt_snapshot = ?, updated_at = ? WHERE id = ?",
            (system_prompt_snapshot, _iso(now), session_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Session '{session_id}' not found")
        return await self.get_session(session_id)

    async def update_session_title(self, session_id: str, title: str) -> Session:
        now = datetime.now(timezone.utc)
        cur = self._execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?", (title, _iso(now), session_id))
        if cur.rowcount == 0:
            raise KeyError(f"Session '{session_id}' not found")
        return await self.get_session(session_id)

    async def list_sessions(self) -> list[Session]:
        rows = self._rows("SELECT * FROM sessions WHERE deleted_at IS NULL ORDER BY created_at")
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
        self._execute(
            """INSERT INTO messages
               (id, session_id, role, content, from_session_id, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (message.id, message.session_id, message.role.value, message.content,
             message.from_session_id, _metadata_json(message.metadata), _iso(message.created_at)),
        )
        self._execute("UPDATE sessions SET last_message_at = ? WHERE id = ?", (_iso(message.created_at), session_id))
        await get_plugin_event_bus().publish(PluginEvent(
            type="message.created",
            source=message.from_session_id or "augentia",
            data=message.model_dump(mode="json"),
        ))
        return message

    async def get_messages(self, session_id: str) -> list[Message]:
        await self.get_session(session_id)
        rows = self._rows(
            "SELECT * FROM messages WHERE session_id = ? AND deleted_at IS NULL ORDER BY created_at, id",
            (session_id,),
        )
        return [_message(r) for r in rows]

    async def clone_messages(
        self,
        source_session_id: str,
        target_session_id: str,
        *,
        before_message_id: str | None = None,
    ) -> list[Message]:
        await self.get_session(source_session_id)
        await self.get_session(target_session_id)
        source = await self.get_messages(source_session_id)
        if before_message_id is not None:
            index = next((i for i, message in enumerate(source) if message.id == before_message_id), None)
            if index is None:
                raise KeyError(f"Message '{before_message_id}' not found")
            source = source[:index]
        cloned: list[Message] = []
        with self._conn:
            for original in source:
                message = Message(
                    session_id=target_session_id,
                    role=original.role,
                    content=original.content,
                    from_session_id=original.from_session_id,
                    metadata=dict(original.metadata),
                    created_at=original.created_at,
                )
                self._execute(
                    """INSERT INTO messages
                       (id, session_id, role, content, from_session_id, metadata, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (message.id, message.session_id, message.role.value, message.content,
                     message.from_session_id, _metadata_json(message.metadata),
                     _iso(message.created_at)),
                )
                cloned.append(message)
            self._execute(
                "UPDATE sessions SET last_message_at = ? WHERE id = ?",
                (_iso(cloned[-1].created_at) if cloned else None, target_session_id),
            )
        return cloned

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

    async def list_plugin_instances(self) -> list[PluginInstance]:
        return [_plugin_instance(r) for r in self._rows("SELECT * FROM plugin_instances ORDER BY created_at")]

    async def get_plugin_instance(self, instance_id: str) -> PluginInstance:
        row = self._row("SELECT * FROM plugin_instances WHERE id = ?", (instance_id,))
        if row is None:
            raise KeyError(f"Plugin instance '{instance_id}' not found")
        return _plugin_instance(row)

    async def create_plugin_instance(self, plugin_id: str, display_name: str, **kwargs: Any) -> PluginInstance:
        instance = PluginInstance(plugin_id=plugin_id, display_name=display_name,
                                  config=kwargs.get("config"), auto_start=kwargs.get("auto_start", False))
        self._execute(
            """INSERT INTO plugin_instances
               (id, plugin_id, display_name, status, config, auto_start, current_run_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (instance.id, instance.plugin_id, instance.display_name, instance.status.value,
             _dump(instance.config), int(instance.auto_start), instance.current_run_id,
             _iso(instance.created_at), _iso(instance.updated_at)),
        )
        return instance

    async def update_plugin_instance(self, instance_id: str, **kwargs: Any) -> PluginInstance:
        updates: dict[str, Any] = {}
        for key in ("display_name", "config", "auto_start", "status", "current_run_id"):
            if key in kwargs and kwargs[key] is not None:
                updates[key] = kwargs[key]
        if "config" in updates:
            updates["config"] = _dump(updates["config"])
        if "auto_start" in updates:
            updates["auto_start"] = int(updates["auto_start"])
        if "status" in updates:
            updates["status"] = updates["status"].value
        if updates:
            updates["updated_at"] = _iso(datetime.now(timezone.utc))
            clause = ", ".join(f"{k} = ?" for k in updates)
            cur = self._execute(f"UPDATE plugin_instances SET {clause} WHERE id = ?", [*updates.values(), instance_id])
            if cur.rowcount == 0:
                raise KeyError(f"Plugin instance '{instance_id}' not found")
        return await self.get_plugin_instance(instance_id)

    async def delete_plugin_instance(self, instance_id: str) -> None:
        await self.get_plugin_instance(instance_id)
        self._execute("DELETE FROM plugin_instances WHERE id = ?", (instance_id,))

    async def create_plugin_run(self, plugin_instance_id: str, plugin_id: str, **kwargs: Any) -> PluginRun:
        await self.get_plugin_instance(plugin_instance_id)
        now = datetime.now(timezone.utc)
        run = PluginRun(plugin_instance_id=plugin_instance_id, plugin_id=plugin_id,
                        config_snapshot=kwargs.get("config_snapshot"), started_at=now,
                        created_at=now, updated_at=now)
        self._execute(
            """INSERT INTO plugin_runs
               (id, plugin_instance_id, plugin_id, status, config_snapshot, started_at,
                running_at, exited_at, exit_code, error, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run.id, run.plugin_instance_id, run.plugin_id, run.status.value,
             _dump(run.config_snapshot), _iso(run.started_at), _iso(run.running_at),
             _iso(run.exited_at), run.exit_code, run.error, _iso(run.created_at), _iso(run.updated_at)),
        )
        return run

    async def get_plugin_run(self, run_id: str) -> PluginRun:
        row = self._row("SELECT * FROM plugin_runs WHERE id = ?", (run_id,))
        if row is None:
            raise KeyError(f"Plugin run '{run_id}' not found")
        return _plugin_run(row)

    async def list_plugin_runs(self, plugin_instance_id: str) -> list[PluginRun]:
        await self.get_plugin_instance(plugin_instance_id)
        rows = self._rows("SELECT * FROM plugin_runs WHERE plugin_instance_id = ? ORDER BY created_at DESC", (plugin_instance_id,))
        return [_plugin_run(r) for r in rows]

    async def update_plugin_run(self, run_id: str, **kwargs: Any) -> PluginRun:
        updates: dict[str, Any] = {}
        for key in ("status", "running_at", "exited_at", "exit_code", "error"):
            if kwargs.get(key, _UNSET) is not _UNSET:
                updates[key] = kwargs[key]
        if "status" in updates and updates["status"] is not None:
            updates["status"] = updates["status"].value
        for key in ("running_at", "exited_at"):
            if key in updates:
                updates[key] = _iso(updates[key])
        if updates:
            updates["updated_at"] = _iso(datetime.now(timezone.utc))
            clause = ", ".join(f"{k} = ?" for k in updates)
            cur = self._execute(f"UPDATE plugin_runs SET {clause} WHERE id = ?", [*updates.values(), run_id])
            if cur.rowcount == 0:
                raise KeyError(f"Plugin run '{run_id}' not found")
        return await self.get_plugin_run(run_id)

    async def add_plugin_log(self, plugin_instance_id: str, plugin_run_id: str, stream: str, line: str, **kwargs: Any) -> PluginLog:
        await self.get_plugin_instance(plugin_instance_id)
        await self.get_plugin_run(plugin_run_id)
        log = PluginLog(
            plugin_instance_id=plugin_instance_id,
            plugin_run_id=plugin_run_id,
            session_id=kwargs.get("session_id"),
            stream=stream,
            level=kwargs.get("level"),
            line=line,
        )
        cur = self._execute(
            """INSERT INTO plugin_logs
               (plugin_instance_id, plugin_run_id, session_id, ts, stream, level, line)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                log.plugin_instance_id,
                log.plugin_run_id,
                log.session_id,
                _iso(log.ts),
                log.stream,
                log.level,
                log.line,
            ),
        )
        log.id = cur.lastrowid
        return log

    async def list_plugin_logs(self, *, plugin_instance_id: str | None = None,
                               plugin_run_id: str | None = None,
                               session_id: str | None = None,
                               limit: int = 500) -> list[PluginLog]:
        clauses: list[str] = []
        values: list[Any] = []
        if plugin_instance_id is not None:
            clauses.append("plugin_instance_id = ?")
            values.append(plugin_instance_id)
        if plugin_run_id is not None:
            clauses.append("plugin_run_id = ?")
            values.append(plugin_run_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            values.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._rows(f"SELECT * FROM plugin_logs {where} ORDER BY ts DESC, id DESC LIMIT ?", [*values, limit])
        return [_plugin_log(r) for r in reversed(rows)]

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
