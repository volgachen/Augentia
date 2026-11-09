# Database Structure

Augentia persists runtime state through the `IAgentDatabase` interface (`app/db/interface.py`). The canonical MySQL schema lives in `app/db/mysql.py` (`_SCHEMA_SQL`); `MockMemoryDatabase` (`app/db/mock.py`) mirrors the same shapes in memory. `DB_TYPE` (`mysql` | `mock`) in `.env` selects the implementation.

The MySQL schema is auto-created idempotently (`CREATE TABLE IF NOT EXISTS`) on startup via the FastAPI `lifespan` → `MySqlDatabase.connect()`.

Plugin definitions are not stored in the database. Installed plugin definitions are scanned dynamically from local `plugins/*/plugin.json` by `app/plugins/catalog.py`.

## `system_prompts`
Immutable system prompt versions. Agents and sessions point at their current
version (`system_prompt_id`); editing a prompt inserts a new version whose
`previous_id` is the one it replaces and repoints the owner, so following
`previous_id` from the current version gives the full history (newest first).
Versions are never updated or deleted, which lets a diverged session share its
source's version. An edit with unchanged text adds no version.

| Column | Type | Notes |
|---|---|---|
| `id` | VARCHAR(36) | PK |
| `content` | LONGTEXT | NOT NULL; the full prompt text |
| `previous_id` | VARCHAR(36) | nullable; the version this one replaced (NULL: first version) |
| `created_at` | DATETIME(3) | NOT NULL; for migrated rows, the owner's `created_at` |

## `agents`
Agent templates (seed data + operator-created).

| Column | Type | Notes |
|---|---|---|
| `id` | VARCHAR(36) | PK |
| `name` | VARCHAR(200) | NOT NULL |
| `description` | TEXT | NOT NULL |
| `agent_type` | VARCHAR(50) | enum-as-string (`claude_code` \| `tool_use`) |
| `system_prompt_id` | VARCHAR(36) | current `system_prompts` version (the template prompt) |
| `tool_names` | JSON | NOT NULL |
| `config` | JSON | nullable |
| `openai_model` | VARCHAR(100) | default `'gpt-4o'` |
| `openai_base_url` | VARCHAR(500) | nullable |
| `created_at` | DATETIME(3) | NOT NULL |

## `sessions`
Live/past sessions; form a tree via `parent_session_id`.

| Column | Type | Notes |
|---|---|---|
| `id` | VARCHAR(36) | PK |
| `agent_id` | VARCHAR(36) | NOT NULL |
| `title` | VARCHAR(300) | nullable |
| `working_dir` | VARCHAR(1000) | nullable |
| `parent_session_id` | VARCHAR(36) | nullable |
| `additional_prompt` | LONGTEXT | nullable |
| `additional_prompt_path` | VARCHAR(1000) | nullable |
| `system_prompt_id` | VARCHAR(36) | nullable; current `system_prompts` version of the effective prompt (agent prompt + additions + runtime context, frozen at first start). NULL until first start |
| `status` | VARCHAR(30) | default `'INITIALIZING'` |
| `created_at` | DATETIME(3) | NOT NULL |
| `updated_at` | DATETIME(3) | NOT NULL |
| `last_message_at` | DATETIME(3) | nullable |
| `deleted_at` | DATETIME(3) | nullable; NULL means visible, non-NULL means soft-deleted |

Indexes: `idx_sessions_agent (agent_id)`, `idx_sessions_parent (parent_session_id)`.

The API still returns the resolved text as `agents.system_prompt` /
`sessions.system_prompt_snapshot` alongside `system_prompt_id`. Databases
created before `system_prompts` are migrated on startup: each agent's
`system_prompt` and each session's `system_prompt_snapshot` becomes a first
version, and those text columns are dropped. History before the migration was
overwritten in place, so it starts at the migrated version.

## `messages`
Turn history.

| Column | Type | Notes |
|---|---|---|
| `id` | VARCHAR(36) | PK |
| `session_id` | VARCHAR(36) | NOT NULL |
| `role` | VARCHAR(20) | NOT NULL |
| `content` | LONGTEXT | NOT NULL |
| `from_session_id` | VARCHAR(100) | nullable source id, for example another session id or `plugin:{instance_id}` |
| `created_at` | DATETIME(3) | NOT NULL |
| `deleted_at` | DATETIME(3) | nullable; NULL means visible, non-NULL means soft-deleted |

Index: `idx_messages_session (session_id)`. FK: `session_id → sessions(id) ON DELETE CASCADE`.

## `plugin_instances`
Long-lived user-created plugin instances. A definition comes from local `plugins/*/plugin.json`; this table stores the user's instance, configuration, current status, and current/latest run pointer.

| Column | Type | Notes |
|---|---|---|
| `id` | VARCHAR(36) | PK |
| `plugin_id` | VARCHAR(200) | manifest id, for example `wechat-bridge` |
| `display_name` | VARCHAR(200) | user-facing instance name |
| `status` | VARCHAR(30) | `stopped` \| `starting` \| `waiting_input` \| `running` \| `stopping` \| `exited` \| `errored` \| `cancelled` |
| `config` | JSON | nullable instance config |
| `auto_start` | BOOLEAN | whether backend startup should start this instance |
| `current_run_id` | VARCHAR(36) | nullable current/latest run id |
| `created_at` | DATETIME(3) | NOT NULL |
| `updated_at` | DATETIME(3) | NOT NULL |

Indexes: `plugin_id`, `status`, `auto_start`, `current_run_id`.

## `plugin_runs`
One row per plugin instance start attempt. Logs attach to runs so previous failures remain inspectable.

| Column | Type | Notes |
|---|---|---|
| `id` | VARCHAR(36) | PK |
| `plugin_instance_id` | VARCHAR(36) | FK to `plugin_instances.id` |
| `plugin_id` | VARCHAR(200) | copied from the instance for easier querying |
| `status` | VARCHAR(30) | same enum family as `plugin_instances.status`, except normally starts at `starting` |
| `config_snapshot` | JSON | nullable config used for this run |
| `started_at` | DATETIME(3) | nullable start-attempt time |
| `running_at` | DATETIME(3) | nullable time when the process was spawned successfully |
| `exited_at` | DATETIME(3) | nullable exit time |
| `exit_code` | INT | nullable process exit code |
| `error` | TEXT | nullable error summary |
| `created_at` | DATETIME(3) | NOT NULL |
| `updated_at` | DATETIME(3) | NOT NULL |

Indexes: `(plugin_instance_id, started_at)`, `plugin_id`, `status`, `started_at`. FK: `plugin_instance_id → plugin_instances(id) ON DELETE CASCADE`.

## `plugin_logs`
Persistent plugin run logs.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT | auto-increment PK |
| `plugin_instance_id` | VARCHAR(36) | FK to `plugin_instances.id` |
| `plugin_run_id` | VARCHAR(36) | FK to `plugin_runs.id` |
| `ts` | DATETIME(3) | NOT NULL |
| `stream` | VARCHAR(20) | `stdout` \| `stderr` \| `system` \| `event` |
| `level` | VARCHAR(20) | nullable structured log level |
| `line` | TEXT | NOT NULL |

Indexes: `(plugin_run_id, ts)`, `(plugin_instance_id, ts)`, `stream`. FKs cascade on instance/run deletion.

## `tasks`
Agent todo lists. Keyed by `task_list_id` (= the owning session id); ids are per-list monotonic integers.

| Column | Type | Notes |
|---|---|---|
| `task_list_id` | VARCHAR(64) | PK part |
| `id` | VARCHAR(20) | PK part |
| `subject` | TEXT | NOT NULL |
| `description` | LONGTEXT | NOT NULL |
| `active_form` | VARCHAR(500) | nullable |
| `owner` | VARCHAR(200) | nullable |
| `status` | VARCHAR(20) | `pending` \| `in_progress` \| `completed` |
| `blocks` | JSON | NOT NULL |
| `blocked_by` | JSON | NOT NULL |
| `metadata` | JSON | nullable |
| `created_at` | DATETIME(3) | NOT NULL |
| `updated_at` | DATETIME(3) | NOT NULL |

PK: `(task_list_id, id)`. Index: `idx_tasks_list (task_list_id)`.

## `task_counters`
Per-list monotonic id allocator.

| Column | Type | Notes |
|---|---|---|
| `task_list_id` | VARCHAR(64) | PK |
| `next_id` | BIGINT | NOT NULL |

## Conventions
- Engine `InnoDB`, charset `utf8mb4` on every table.
- `aiomysql` runs `autocommit=True`.
- Enums are stored as VARCHAR; list/dict fields are stored as JSON columns.
- Agents are seeded via `INSERT IGNORE` from `_SEED_AGENTS`, duplicated in both `db/mysql.py` and `db/mock.py`.
- `PostgreSQLDatabase` remains the Milestone 4 target.
