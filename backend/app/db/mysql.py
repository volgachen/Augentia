import json
import uuid
import warnings
from datetime import datetime, timezone
from typing import Any

import aiomysql
from aiomysql import DictCursor, Pool

from app.config import Settings
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

_SCHEMA_SQL = [
    """\
CREATE TABLE IF NOT EXISTS agents (
    id             VARCHAR(36)   PRIMARY KEY,
    name           VARCHAR(200)  NOT NULL,
    description    TEXT          NOT NULL,
    agent_type     VARCHAR(50)   NOT NULL,
    system_prompt  TEXT          NOT NULL,
    tool_names     JSON          NOT NULL,
    config         JSON          DEFAULT NULL,
    openai_model   VARCHAR(100)  NOT NULL DEFAULT 'gpt-4o',
    openai_base_url VARCHAR(500) DEFAULT NULL,
    created_at     DATETIME(3)   NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """\
CREATE TABLE IF NOT EXISTS sessions (
    id                VARCHAR(36)   PRIMARY KEY,
    agent_id          VARCHAR(36)   NOT NULL,
    title             VARCHAR(300)  DEFAULT NULL,
    working_dir       VARCHAR(1000) DEFAULT NULL,
    parent_session_id VARCHAR(36)   DEFAULT NULL,
    additional_prompt LONGTEXT      DEFAULT NULL,
    additional_prompt_path VARCHAR(1000) DEFAULT NULL,
    system_prompt_snapshot LONGTEXT DEFAULT NULL,
    status            VARCHAR(30)   NOT NULL DEFAULT 'INITIALIZING',
    created_at        DATETIME(3)   NOT NULL,
    updated_at        DATETIME(3)   NOT NULL,
    last_message_at   DATETIME(3)   DEFAULT NULL,
    deleted_at        DATETIME(3)   DEFAULT NULL,
    INDEX idx_sessions_agent (agent_id),
    INDEX idx_sessions_parent (parent_session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """\
CREATE TABLE IF NOT EXISTS messages (
    id               VARCHAR(36)  PRIMARY KEY,
    session_id       VARCHAR(36)  NOT NULL,
    role             VARCHAR(20)  NOT NULL,
    content          LONGTEXT     NOT NULL,
    from_session_id  VARCHAR(100) DEFAULT NULL,
    created_at       DATETIME(3)  NOT NULL,
    deleted_at       DATETIME(3)  DEFAULT NULL,
    INDEX idx_messages_session (session_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """\
CREATE TABLE IF NOT EXISTS plugin_instances (
    id              VARCHAR(36)   PRIMARY KEY,
    plugin_id       VARCHAR(200)  NOT NULL,
    display_name    VARCHAR(200)  NOT NULL,
    status          VARCHAR(30)   NOT NULL DEFAULT 'stopped',
    config          JSON          DEFAULT NULL,
    auto_start      BOOLEAN       NOT NULL DEFAULT FALSE,
    current_run_id  VARCHAR(36)   DEFAULT NULL,
    created_at      DATETIME(3)   NOT NULL,
    updated_at      DATETIME(3)   NOT NULL,
    INDEX idx_plugin_instances_plugin_id (plugin_id),
    INDEX idx_plugin_instances_status (status),
    INDEX idx_plugin_instances_auto_start (auto_start),
    INDEX idx_plugin_instances_current_run (current_run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """\
CREATE TABLE IF NOT EXISTS plugin_runs (
    id                  VARCHAR(36)   PRIMARY KEY,
    plugin_instance_id  VARCHAR(36)   NOT NULL,
    plugin_id           VARCHAR(200)  NOT NULL,
    status              VARCHAR(30)   NOT NULL DEFAULT 'starting',
    config_snapshot     JSON          DEFAULT NULL,
    started_at          DATETIME(3)   DEFAULT NULL,
    running_at          DATETIME(3)   DEFAULT NULL,
    exited_at           DATETIME(3)   DEFAULT NULL,
    exit_code           INT           DEFAULT NULL,
    error               TEXT          DEFAULT NULL,
    created_at          DATETIME(3)   NOT NULL,
    updated_at          DATETIME(3)   NOT NULL,
    INDEX idx_plugin_runs_instance_started (plugin_instance_id, started_at),
    INDEX idx_plugin_runs_plugin_id (plugin_id),
    INDEX idx_plugin_runs_status (status),
    INDEX idx_plugin_runs_started_at (started_at),
    FOREIGN KEY (plugin_instance_id) REFERENCES plugin_instances(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """\
CREATE TABLE IF NOT EXISTS plugin_logs (
    id                  BIGINT        AUTO_INCREMENT PRIMARY KEY,
    plugin_instance_id  VARCHAR(36)   NOT NULL,
    plugin_run_id       VARCHAR(36)   NOT NULL,
    session_id          VARCHAR(36)   DEFAULT NULL,
    ts                  DATETIME(3)   NOT NULL,
    stream              VARCHAR(20)   NOT NULL,
    level               VARCHAR(20)   DEFAULT NULL,
    line                TEXT          NOT NULL,
    INDEX idx_plugin_logs_run_ts (plugin_run_id, ts),
    INDEX idx_plugin_logs_instance_ts (plugin_instance_id, ts),
    INDEX idx_plugin_logs_session_ts (session_id, ts),
    INDEX idx_plugin_logs_stream (stream),
    FOREIGN KEY (plugin_instance_id) REFERENCES plugin_instances(id) ON DELETE CASCADE,
    FOREIGN KEY (plugin_run_id) REFERENCES plugin_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """\
CREATE TABLE IF NOT EXISTS tasks (
    task_list_id  VARCHAR(64)  NOT NULL,
    id            VARCHAR(20)  NOT NULL,
    subject       TEXT         NOT NULL,
    description   LONGTEXT     NOT NULL,
    active_form   VARCHAR(500) DEFAULT NULL,
    owner         VARCHAR(200) DEFAULT NULL,
    status        VARCHAR(20)  NOT NULL DEFAULT 'pending',
    blocks        JSON         NOT NULL,
    blocked_by    JSON         NOT NULL,
    metadata      JSON         DEFAULT NULL,
    created_at    DATETIME(3)  NOT NULL,
    updated_at    DATETIME(3)  NOT NULL,
    PRIMARY KEY (task_list_id, id),
    INDEX idx_tasks_list (task_list_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """\
CREATE TABLE IF NOT EXISTS task_counters (
    task_list_id  VARCHAR(64)  PRIMARY KEY,
    next_id       BIGINT       NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

# Idempotent column migrations for databases created before a column existed.
# MySQL 8 lacks `ADD COLUMN IF NOT EXISTS`, so we probe information_schema first.
# Each entry: (table, column, [SQL statements run once, when the column is added]).
# The list lets a migration backfill the new column right after adding it.
_COLUMN_MIGRATIONS: list[tuple[str, str, list[str]]] = [
    (
        "agents",
        "config",
        ["ALTER TABLE agents ADD COLUMN config JSON DEFAULT NULL"],
    ),
    (
        "sessions",
        "title",
        [
            "ALTER TABLE sessions ADD COLUMN title VARCHAR(300) DEFAULT NULL "
            "AFTER agent_id"
        ],
    ),
    (
        "sessions",
        "last_message_at",
        [
            "ALTER TABLE sessions ADD COLUMN last_message_at DATETIME(3) "
            "DEFAULT NULL AFTER updated_at",
            # One-off backfill from the existing messages so pre-migration
            # sessions sort correctly instead of collapsing to NULL.
            """UPDATE sessions s SET s.last_message_at = (
                   SELECT MAX(m.created_at) FROM messages m
                   WHERE m.session_id = s.id
               )""",
        ],
    ),
    (
        "sessions",
        "system_prompt_snapshot",
        [
            "ALTER TABLE sessions ADD COLUMN system_prompt_snapshot LONGTEXT DEFAULT NULL "
            "AFTER additional_prompt_path"
        ],
    ),
    (
        "sessions",
        "deleted_at",
        ["ALTER TABLE sessions ADD COLUMN deleted_at DATETIME(3) DEFAULT NULL AFTER last_message_at"],
    ),
    (
        "messages",
        "deleted_at",
        ["ALTER TABLE messages ADD COLUMN deleted_at DATETIME(3) DEFAULT NULL AFTER created_at"],
    ),
    (
        "plugin_logs",
        "session_id",
        [
            "ALTER TABLE plugin_logs ADD COLUMN session_id VARCHAR(36) DEFAULT NULL AFTER plugin_run_id",
            "ALTER TABLE plugin_logs ADD INDEX idx_plugin_logs_session_ts (session_id, ts)",
        ],
    ),
]

def _row_to_agent(row: dict[str, Any]) -> AgentTemplate:
    tool_names = row["tool_names"]
    if isinstance(tool_names, str):
        tool_names = json.loads(tool_names)
    # `.get` so a database not yet ALTER'd with config still loads.
    config = row.get("config")
    if isinstance(config, str):
        config = json.loads(config)
    return AgentTemplate(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        agent_type=AgentType(row["agent_type"]),
        system_prompt=row["system_prompt"],
        tool_names=tool_names,
        config=config or {},
        openai_model=row["openai_model"],
        openai_base_url=row["openai_base_url"],
        created_at=row["created_at"],
    )


def _row_to_session(row: dict[str, Any]) -> Session:
    return Session(
        id=row["id"],
        agent_id=row["agent_id"],
        title=row.get("title"),
        working_dir=row["working_dir"],
        parent_session_id=row["parent_session_id"],
        # `.get` instead of `[]` so a database that hasn't had the additional_*
        # columns ALTER'd in yet still loads. Operators run that ALTER manually
        # (see CLAUDE.md / migration notes).
        additional_prompt=row.get("additional_prompt"),
        additional_prompt_path=row.get("additional_prompt_path"),
        system_prompt_snapshot=row.get("system_prompt_snapshot"),
        status=SessionStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_message_at=row.get("last_message_at"),
        deleted_at=row.get("deleted_at"),
    )


def _row_to_message(row: dict[str, Any]) -> Message:
    return Message(
        id=row["id"],
        session_id=row["session_id"],
        role=MessageRole(row["role"]),
        content=row["content"],
        from_session_id=row["from_session_id"],
        created_at=row["created_at"],
        deleted_at=row.get("deleted_at"),
    )


def _json_col(value: Any, default: Any) -> Any:
    # JSON columns come back as already-decoded objects on some aiomysql/MySQL
    # versions and as raw strings on others; normalize both.
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _json_dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _row_to_plugin_instance(row: dict[str, Any]) -> PluginInstance:
    return PluginInstance(
        id=row["id"],
        plugin_id=row["plugin_id"],
        display_name=row["display_name"],
        status=PluginStatus(row["status"]),
        config=_json_col(row.get("config"), None),
        auto_start=bool(row["auto_start"]),
        current_run_id=row.get("current_run_id"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_plugin_run(row: dict[str, Any]) -> PluginRun:
    return PluginRun(
        id=row["id"],
        plugin_instance_id=row["plugin_instance_id"],
        plugin_id=row["plugin_id"],
        status=PluginStatus(row["status"]),
        config_snapshot=_json_col(row.get("config_snapshot"), None),
        started_at=row.get("started_at"),
        running_at=row.get("running_at"),
        exited_at=row.get("exited_at"),
        exit_code=row.get("exit_code"),
        error=row.get("error"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_plugin_log(row: dict[str, Any]) -> PluginLog:
    return PluginLog(
        id=row["id"],
        plugin_instance_id=row["plugin_instance_id"],
        plugin_run_id=row["plugin_run_id"],
        session_id=row.get("session_id"),
        ts=row["ts"],
        stream=row["stream"],
        level=row.get("level"),
        line=row["line"],
    )


def _row_to_task(row: dict[str, Any]) -> Task:
    return Task(
        id=row["id"],
        task_list_id=row["task_list_id"],
        subject=row["subject"],
        description=row["description"],
        active_form=row["active_form"],
        owner=row["owner"],
        status=TaskStatus(row["status"]),
        blocks=_json_col(row["blocks"], []),
        blocked_by=_json_col(row["blocked_by"], []),
        metadata=_json_col(row["metadata"], None),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class MySqlDatabase(IAgentDatabase):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: Pool | None = None

    async def connect(self) -> None:
        self._pool = await aiomysql.create_pool(
            host=self._settings.mysql_host,
            port=self._settings.mysql_port,
            user=self._settings.mysql_user,
            password=self._settings.mysql_password,
            db=self._settings.mysql_database,
            minsize=2,
            maxsize=10,
            autocommit=True,
        )
        await self._init_schema()
        await self._migrate_columns()
        await self._migrate_column_types()
        await self._seed_agents()

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def _init_schema(self) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    for sql in _SCHEMA_SQL:
                        await cur.execute(sql)

    async def _migrate_columns(self) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    for table, column, statements in _COLUMN_MIGRATIONS:
                        await cur.execute(
                            """SELECT COUNT(*) FROM information_schema.columns
                               WHERE table_schema = %s
                                 AND table_name = %s
                                 AND column_name = %s""",
                            (self._settings.mysql_database, table, column),
                        )
                        (exists,) = await cur.fetchone()
                        if not exists:
                            for stmt in statements:
                                await cur.execute(stmt)

    async def _migrate_column_types(self) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT CHARACTER_MAXIMUM_LENGTH
                       FROM information_schema.columns
                       WHERE table_schema = %s
                         AND table_name = 'messages'
                         AND column_name = 'from_session_id'""",
                    (self._settings.mysql_database,),
                )
                row = await cur.fetchone()
                if row is None:
                    return
                (max_len,) = row
                if max_len is not None and max_len < 100:
                    await cur.execute(
                        "ALTER TABLE messages MODIFY COLUMN from_session_id VARCHAR(100) DEFAULT NULL"
                    )

    async def _seed_agents(self) -> None:
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    for agent in SEED_AGENT_ROWS:
                        await cur.execute(
                        """INSERT IGNORE INTO agents
                           (id, name, description, agent_type, system_prompt,
                            tool_names, config, openai_model, openai_base_url, created_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            agent["id"],
                            agent["name"],
                            agent["description"],
                            agent["agent_type"],
                            agent["system_prompt"],
                            json.dumps(agent["tool_names"]),
                            json.dumps(agent.get("config", {})),
                            agent["openai_model"],
                            agent["openai_base_url"],
                            now,
                        ),
                    )

    # ---- Agents ----

    async def list_agents(self) -> list[AgentTemplate]:
        async with self._pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute("SELECT * FROM agents ORDER BY created_at")
                rows = await cur.fetchall()
        return [_row_to_agent(r) for r in rows]

    async def get_agent(self, agent_id: str) -> AgentTemplate:
        async with self._pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM agents WHERE id = %s", (agent_id,)
                )
                row = await cur.fetchone()
        if row is None:
            raise KeyError(f"Agent '{agent_id}' not found")
        return _row_to_agent(row)

    async def create_agent(self, template: AgentTemplate) -> AgentTemplate:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO agents
                       (id, name, description, agent_type, system_prompt,
                        tool_names, config, openai_model, openai_base_url, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        template.id,
                        template.name,
                        template.description,
                        template.agent_type.value,
                        template.system_prompt,
                        json.dumps(template.tool_names),
                        json.dumps(template.config),
                        template.openai_model,
                        template.openai_base_url,
                        template.created_at,
                    ),
                )
        return template

    async def update_agent(
        self,
        agent_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        system_prompt: str | None = None,
        tool_names: list[str] | None = None,
        config: dict | None = None,
        openai_model: str | None = None,
        openai_base_url: str | None = None,
    ) -> AgentTemplate:
        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        if system_prompt is not None:
            updates["system_prompt"] = system_prompt
        if tool_names is not None:
            updates["tool_names"] = json.dumps(tool_names)
        if config is not None:
            updates["config"] = json.dumps(config)
        if openai_model is not None:
            updates["openai_model"] = openai_model
        if openai_base_url is not None:
            updates["openai_base_url"] = openai_base_url

        if not updates:
            return await self.get_agent(agent_id)

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        values = list(updates.values()) + [agent_id]

        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"UPDATE agents SET {set_clause} WHERE id = %s",
                    values,
                )
        return await self.get_agent(agent_id)

    async def delete_agent(self, agent_id: str) -> None:
        await self.get_agent(agent_id)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM agents WHERE id = %s", (agent_id,))

    # ---- Sessions ----

    async def create_session(
        self,
        agent_id: str,
        working_dir: str | None = None,
        *,
        session_id: str | None = None,
        title: str | None = None,
        parent_session_id: str | None = None,
        additional_prompt: str | None = None,
        additional_prompt_path: str | None = None,
        system_prompt_snapshot: str | None = None,
    ) -> Session:
        agent = await self.get_agent(agent_id)
        session = Session(
            id=session_id or str(uuid.uuid4()),
            agent_id=agent_id,
            title=title,
            working_dir=working_dir,
            parent_session_id=parent_session_id,
            additional_prompt=additional_prompt,
            additional_prompt_path=additional_prompt_path,
            system_prompt_snapshot=system_prompt_snapshot,
        )
        # Seed a friendly default so the UI never shows a blank title. Uses the
        # agent's name + creation time; a caller-supplied title (e.g. a spawning
        # agent's task description) takes precedence.
        if not session.title:
            session.title = f"{agent.name} · {session.created_at:%H:%M}"
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO sessions
                       (id, agent_id, title, working_dir, parent_session_id,
                        additional_prompt, additional_prompt_path,
                        system_prompt_snapshot,
                        status, created_at, updated_at, last_message_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        session.id,
                        session.agent_id,
                        session.title,
                        session.working_dir,
                        session.parent_session_id,
                        session.additional_prompt,
                        session.additional_prompt_path,
                        session.system_prompt_snapshot,
                        session.status.value,
                        session.created_at,
                        session.updated_at,
                        session.last_message_at,
                    ),
                )
        return session

    async def update_session_title(self, session_id: str, title: str) -> Session:
        session = await self.get_session(session_id)
        session.title = title
        session.updated_at = datetime.now(timezone.utc)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE sessions SET title = %s, updated_at = %s WHERE id = %s",
                    (session.title, session.updated_at, session_id),
                )
        return session

    async def get_session(self, session_id: str) -> Session:
        async with self._pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM sessions WHERE id = %s AND deleted_at IS NULL", (session_id,)
                )
                row = await cur.fetchone()
        if row is None:
            raise KeyError(f"Session '{session_id}' not found")
        return _row_to_session(row)

    async def update_session_system_prompt_snapshot(
        self,
        session_id: str,
        system_prompt_snapshot: str,
    ) -> Session:
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                result = await cur.execute(
                    """UPDATE sessions
                       SET system_prompt_snapshot = %s, updated_at = %s
                       WHERE id = %s AND deleted_at IS NULL""",
                    (system_prompt_snapshot, now, session_id),
                )
        if result == 0:
            raise KeyError(f"Session '{session_id}' not found")
        return await self.get_session(session_id)

    async def list_sessions(self) -> list[Session]:
        async with self._pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute("SELECT * FROM sessions WHERE deleted_at IS NULL ORDER BY created_at")
                rows = await cur.fetchall()
        return [_row_to_session(r) for r in rows]

    async def update_session_status(
        self, session_id: str, status: SessionStatus
    ) -> Session:
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                result = await cur.execute(
                    """UPDATE sessions SET status = %s, updated_at = %s
                       WHERE id = %s""",
                    (status.value, now, session_id),
                )
        if result == 0:
            raise KeyError(f"Session '{session_id}' not found")
        return await self.get_session(session_id)

    async def soft_delete_session(self, session_id: str) -> None:
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                result = await cur.execute(
                    """UPDATE sessions SET deleted_at = %s, updated_at = %s
                       WHERE id = %s AND deleted_at IS NULL""",
                    (now, now, session_id),
                )
                if result == 0:
                    raise KeyError(f"Session '{session_id}' not found")
                await cur.execute(
                    """UPDATE messages SET deleted_at = %s
                       WHERE session_id = %s AND deleted_at IS NULL""",
                    (now, session_id),
                )

    # ---- Messages ----

    async def add_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        *,
        from_session_id: str | None = None,
    ) -> Message:
        await self.get_session(session_id)
        message = Message(
            session_id=session_id,
            role=role,
            content=content,
            from_session_id=from_session_id,
        )
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO messages
                       (id, session_id, role, content, from_session_id, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        message.id,
                        message.session_id,
                        message.role.value,
                        message.content,
                        message.from_session_id,
                        message.created_at,
                    ),
                )
                # Denormalized "last activity" marker on the session. Kept in
                # sync here because add_message is the only write path for
                # messages, which keeps the dashboard's sort key a plain column
                # read instead of a MAX() join over the (large) messages table.
                await cur.execute(
                    "UPDATE sessions SET last_message_at = %s WHERE id = %s",
                    (message.created_at, session_id),
                )
        await get_plugin_event_bus().publish(PluginEvent(
            type="message.created",
            source=from_session_id or "augentia",
            data=message.model_dump(mode="json"),
        ))
        return message

    async def get_messages(self, session_id: str) -> list[Message]:
        await self.get_session(session_id)
        async with self._pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(
                    """SELECT * FROM messages
                       WHERE session_id = %s AND deleted_at IS NULL
                       ORDER BY created_at""",
                    (session_id,),
                )
                rows = await cur.fetchall()
        return [_row_to_message(r) for r in rows]

    async def clone_messages(
        self,
        source_session_id: str,
        target_session_id: str,
        *,
        before_message_id: str | None = None,
    ) -> list[Message]:
        await self.get_session(source_session_id)
        await self.get_session(target_session_id)
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(DictCursor) as cur:
                    await cur.execute(
                        """SELECT * FROM messages
                           WHERE session_id = %s AND deleted_at IS NULL
                           ORDER BY created_at, id""",
                        (source_session_id,),
                    )
                    rows = await cur.fetchall()
                    originals = [_row_to_message(row) for row in rows]
                    if before_message_id is not None:
                        index = next(
                            (i for i, message in enumerate(originals) if message.id == before_message_id),
                            None,
                        )
                        if index is None:
                            raise KeyError(f"Message '{before_message_id}' not found")
                        originals = originals[:index]
                    cloned: list[Message] = []
                    for original in originals:
                        message = Message(
                            session_id=target_session_id,
                            role=original.role,
                            content=original.content,
                            from_session_id=original.from_session_id,
                            created_at=original.created_at,
                        )
                        await cur.execute(
                            """INSERT INTO messages
                               (id, session_id, role, content, from_session_id, created_at)
                               VALUES (%s, %s, %s, %s, %s, %s)""",
                            (
                                message.id,
                                message.session_id,
                                message.role.value,
                                message.content,
                                message.from_session_id,
                                message.created_at,
                            ),
                        )
                        cloned.append(message)
                    await cur.execute(
                        "UPDATE sessions SET last_message_at = %s WHERE id = %s",
                        (cloned[-1].created_at if cloned else None, target_session_id),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return cloned

    async def soft_delete_messages_from(self, session_id: str, message_id: str) -> Message:
        await self.get_session(session_id)
        async with self._pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(
                    """SELECT * FROM messages
                       WHERE session_id = %s AND id = %s AND deleted_at IS NULL""",
                    (session_id, message_id),
                )
                row = await cur.fetchone()
                if row is None:
                    raise KeyError(f"Message '{message_id}' not found")
                target = _row_to_message(row)

            now = datetime.now(timezone.utc)
            async with conn.cursor() as cur:
                await cur.execute(
                    """UPDATE messages SET deleted_at = %s
                       WHERE session_id = %s AND deleted_at IS NULL
                         AND created_at >= %s""",
                    (now, session_id, target.created_at),
                )
                await cur.execute(
                    """UPDATE sessions s SET last_message_at = (
                           SELECT MAX(m.created_at) FROM messages m
                           WHERE m.session_id = s.id AND m.deleted_at IS NULL
                       ) WHERE s.id = %s""",
                    (session_id,),
                )
        return target

    # ---- Plugin instances/runs/logs ----

    async def list_plugin_instances(self) -> list[PluginInstance]:
        async with self._pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute("SELECT * FROM plugin_instances ORDER BY created_at")
                rows = await cur.fetchall()
        return [_row_to_plugin_instance(r) for r in rows]

    async def get_plugin_instance(self, instance_id: str) -> PluginInstance:
        async with self._pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM plugin_instances WHERE id = %s", (instance_id,)
                )
                row = await cur.fetchone()
        if row is None:
            raise KeyError(f"Plugin instance '{instance_id}' not found")
        return _row_to_plugin_instance(row)

    async def create_plugin_instance(
        self,
        plugin_id: str,
        display_name: str,
        *,
        config: dict | None = None,
        auto_start: bool = False,
    ) -> PluginInstance:
        instance = PluginInstance(
            plugin_id=plugin_id,
            display_name=display_name,
            config=config,
            auto_start=auto_start,
        )
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO plugin_instances
                       (id, plugin_id, display_name, status, config, auto_start,
                        current_run_id, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        instance.id,
                        instance.plugin_id,
                        instance.display_name,
                        instance.status.value,
                        _json_dump(instance.config),
                        instance.auto_start,
                        instance.current_run_id,
                        instance.created_at,
                        instance.updated_at,
                    ),
                )
        return instance

    async def update_plugin_instance(
        self,
        instance_id: str,
        *,
        display_name: str | None = None,
        config: dict | None = None,
        auto_start: bool | None = None,
        status: PluginStatus | None = None,
        current_run_id: str | None = None,
    ) -> PluginInstance:
        updates: dict[str, Any] = {}
        if display_name is not None:
            updates["display_name"] = display_name
        if config is not None:
            updates["config"] = _json_dump(config)
        if auto_start is not None:
            updates["auto_start"] = auto_start
        if status is not None:
            updates["status"] = status.value
        if current_run_id is not None:
            updates["current_run_id"] = current_run_id
        if not updates:
            return await self.get_plugin_instance(instance_id)
        updates["updated_at"] = datetime.now(timezone.utc)
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        values = list(updates.values()) + [instance_id]
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                result = await cur.execute(
                    f"UPDATE plugin_instances SET {set_clause} WHERE id = %s",
                    values,
                )
        if result == 0:
            raise KeyError(f"Plugin instance '{instance_id}' not found")
        return await self.get_plugin_instance(instance_id)

    async def delete_plugin_instance(self, instance_id: str) -> None:
        await self.get_plugin_instance(instance_id)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM plugin_instances WHERE id = %s", (instance_id,))

    async def create_plugin_run(
        self,
        plugin_instance_id: str,
        plugin_id: str,
        *,
        config_snapshot: dict | None = None,
    ) -> PluginRun:
        await self.get_plugin_instance(plugin_instance_id)
        now = datetime.now(timezone.utc)
        run = PluginRun(
            plugin_instance_id=plugin_instance_id,
            plugin_id=plugin_id,
            config_snapshot=config_snapshot,
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO plugin_runs
                       (id, plugin_instance_id, plugin_id, status, config_snapshot,
                        started_at, running_at, exited_at, exit_code, error,
                        created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        run.id,
                        run.plugin_instance_id,
                        run.plugin_id,
                        run.status.value,
                        _json_dump(run.config_snapshot),
                        run.started_at,
                        run.running_at,
                        run.exited_at,
                        run.exit_code,
                        run.error,
                        run.created_at,
                        run.updated_at,
                    ),
                )
        return run

    async def get_plugin_run(self, run_id: str) -> PluginRun:
        async with self._pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute("SELECT * FROM plugin_runs WHERE id = %s", (run_id,))
                row = await cur.fetchone()
        if row is None:
            raise KeyError(f"Plugin run '{run_id}' not found")
        return _row_to_plugin_run(row)

    async def list_plugin_runs(self, plugin_instance_id: str) -> list[PluginRun]:
        await self.get_plugin_instance(plugin_instance_id)
        async with self._pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(
                    """SELECT * FROM plugin_runs
                       WHERE plugin_instance_id = %s ORDER BY created_at DESC""",
                    (plugin_instance_id,),
                )
                rows = await cur.fetchall()
        return [_row_to_plugin_run(r) for r in rows]

    async def update_plugin_run(
        self,
        run_id: str,
        *,
        status: PluginStatus | None = None,
        running_at: Any = _UNSET,
        exited_at: Any = _UNSET,
        exit_code: Any = _UNSET,
        error: Any = _UNSET,
    ) -> PluginRun:
        updates: dict[str, Any] = {}
        if status is not None:
            updates["status"] = status.value
        if running_at is not _UNSET:
            updates["running_at"] = running_at
        if exited_at is not _UNSET:
            updates["exited_at"] = exited_at
        if exit_code is not _UNSET:
            updates["exit_code"] = exit_code
        if error is not _UNSET:
            updates["error"] = error
        if not updates:
            return await self.get_plugin_run(run_id)
        updates["updated_at"] = datetime.now(timezone.utc)
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        values = list(updates.values()) + [run_id]
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                result = await cur.execute(
                    f"UPDATE plugin_runs SET {set_clause} WHERE id = %s",
                    values,
                )
        if result == 0:
            raise KeyError(f"Plugin run '{run_id}' not found")
        return await self.get_plugin_run(run_id)

    async def add_plugin_log(
        self,
        plugin_instance_id: str,
        plugin_run_id: str,
        stream: str,
        line: str,
        *,
        level: str | None = None,
        session_id: str | None = None,
    ) -> PluginLog:
        log = PluginLog(
            plugin_instance_id=plugin_instance_id,
            plugin_run_id=plugin_run_id,
            session_id=session_id,
            stream=stream,
            level=level,
            line=line,
        )
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO plugin_logs
                       (plugin_instance_id, plugin_run_id, session_id, ts, stream, level, line)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        log.plugin_instance_id,
                        log.plugin_run_id,
                        log.session_id,
                        log.ts,
                        log.stream,
                        log.level,
                        log.line,
                    ),
                )
                log.id = cur.lastrowid
        return log

    async def list_plugin_logs(
        self,
        *,
        plugin_instance_id: str | None = None,
        plugin_run_id: str | None = None,
        session_id: str | None = None,
        limit: int = 500,
    ) -> list[PluginLog]:
        clauses: list[str] = []
        values: list[Any] = []
        if plugin_instance_id is not None:
            clauses.append("plugin_instance_id = %s")
            values.append(plugin_instance_id)
        if plugin_run_id is not None:
            clauses.append("plugin_run_id = %s")
            values.append(plugin_run_id)
        if session_id is not None:
            clauses.append("session_id = %s")
            values.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        async with self._pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(
                    f"""SELECT * FROM plugin_logs {where}
                        ORDER BY ts DESC, id DESC LIMIT %s""",
                    values,
                )
                rows = await cur.fetchall()
        return [_row_to_plugin_log(r) for r in reversed(rows)]

    # ---- Tasks ----

    async def _next_task_id(self, task_list_id: str) -> int:

        # Atomic per-list counter via the LAST_INSERT_ID upsert trick: the INSERT
        # seeds next_id=1, the ON DUPLICATE path bumps it; both stash the value in
        # the connection's LAST_INSERT_ID() so a single follow-up SELECT reads it.
        # Counter survives deletes, so ids are monotonic and never reused.
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO task_counters (task_list_id, next_id)
                       VALUES (%s, LAST_INSERT_ID(1))
                       ON DUPLICATE KEY UPDATE next_id = LAST_INSERT_ID(next_id + 1)""",
                    (task_list_id,),
                )
                await cur.execute("SELECT LAST_INSERT_ID()")
                row = await cur.fetchone()
        return int(row[0])

    async def create_task(
        self,
        task_list_id: str,
        subject: str,
        description: str,
        *,
        active_form: str | None = None,
        metadata: dict | None = None,
    ) -> Task:
        next_id = await self._next_task_id(task_list_id)
        task = Task(
            id=str(next_id),
            task_list_id=task_list_id,
            subject=subject,
            description=description,
            active_form=active_form,
            metadata=metadata,
        )
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO tasks
                       (task_list_id, id, subject, description, active_form,
                        owner, status, blocks, blocked_by, metadata,
                        created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        task.task_list_id,
                        task.id,
                        task.subject,
                        task.description,
                        task.active_form,
                        task.owner,
                        task.status.value,
                        json.dumps(task.blocks),
                        json.dumps(task.blocked_by),
                        json.dumps(task.metadata) if task.metadata is not None else None,
                        task.created_at,
                        task.updated_at,
                    ),
                )
        return task

    async def get_task(self, task_list_id: str, task_id: str) -> Task | None:
        async with self._pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM tasks WHERE task_list_id = %s AND id = %s",
                    (task_list_id, task_id),
                )
                row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def list_tasks(self, task_list_id: str) -> list[Task]:
        async with self._pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM tasks WHERE task_list_id = %s "
                    "ORDER BY CAST(id AS UNSIGNED)",
                    (task_list_id,),
                )
                rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def _write_task(self, task: Task) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """UPDATE tasks SET
                       subject = %s, description = %s, active_form = %s,
                       owner = %s, status = %s, blocks = %s, blocked_by = %s,
                       metadata = %s, updated_at = %s
                       WHERE task_list_id = %s AND id = %s""",
                    (
                        task.subject,
                        task.description,
                        task.active_form,
                        task.owner,
                        task.status.value,
                        json.dumps(task.blocks),
                        json.dumps(task.blocked_by),
                        json.dumps(task.metadata) if task.metadata is not None else None,
                        task.updated_at,
                        task.task_list_id,
                        task.id,
                    ),
                )

    async def update_task(
        self,
        task_list_id: str,
        task_id: str,
        *,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        status: TaskStatus | None = None,
        owner: str | None = _UNSET,
        metadata: dict | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
    ) -> Task | None:
        task = await self.get_task(task_list_id, task_id)
        if task is None:
            return None

        if subject is not None:
            task.subject = subject
        if description is not None:
            task.description = description
        if active_form is not None:
            task.active_form = active_form
        if status is not None:
            task.status = status
        if owner is not _UNSET:
            task.owner = owner
        if metadata is not None:
            merged = dict(task.metadata or {})
            for k, v in metadata.items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = v
            task.metadata = merged or None

        # Reciprocal dependency wiring: A blocks B  <=>  B blockedBy A. The other
        # side of each edge is read-modify-written separately (autocommit, no txn).
        for other_id in add_blocks or []:
            other = await self.get_task(task_list_id, other_id)
            if other is None:
                continue
            if other_id not in task.blocks:
                task.blocks.append(other_id)
            if task_id not in other.blocked_by:
                other.blocked_by.append(task_id)
                other.updated_at = datetime.now(timezone.utc)
                await self._write_task(other)
        for other_id in add_blocked_by or []:
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
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                result = await cur.execute(
                    "DELETE FROM tasks WHERE task_list_id = %s AND id = %s",
                    (task_list_id, task_id),
                )
        if result == 0:
            return False
        # Cascade: strip dangling references from every other task in the list.
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
