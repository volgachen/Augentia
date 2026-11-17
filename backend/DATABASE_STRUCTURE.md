# Database Structure

Augentia persists runtime state through the `IAgentDatabase` interface (`app/db/interface.py`). The canonical MySQL schema lives in `app/db/mysql.py` (`_SCHEMA_SQL`); `MockMemoryDatabase` (`app/db/mock.py`) mirrors the same shapes in memory. `DB_TYPE` (`mysql` | `mock`) in `.env` selects the implementation.

The MySQL schema is auto-created idempotently (`CREATE TABLE IF NOT EXISTS`) on startup via the FastAPI `lifespan` → `MySqlDatabase.connect()`.

Plugin definitions and settings are not stored in the database: plugins are scanned from `~/.augentia/plugins/*/plugin.json` (`app/plugins/loader.py`), global settings live in `~/.augentia/plugin_settings.json`, per-session settings in each session's `config.json`. The database only keeps plugin service runs and plugin log lines.

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
| `parent_session_id` | VARCHAR(36) | nullable; the session that spawned this one as a sub-agent (`subagent` tool / `POST /sessions`) |
| `branched_from_session_id` | VARCHAR(36) | nullable; set on a branch made by diverge: the session it was branched from |
| `branched_from_message_id` | VARCHAR(36) | nullable; the message the branch was made at (in the source session) |
| `branched_cutoff_at` | DATETIME(3) | nullable; the branch borrows the source's rows created before this time (see below) |
| `join_into_session_id` | VARCHAR(36) | nullable; fork-dispatch branches: the session its first turn's result is appended to |
| `joined_message_id` | VARCHAR(36) | nullable; the `branch_summary` row that result became (NULL while the join is pending) |
| `dispatch_mode` | TINYINT(1) | NOT NULL, default 0; dispatch mode on: each `POST /messages` runs in a new branch whose result is joined back |
| `additional_prompt` | LONGTEXT | nullable |
| `additional_prompt_path` | VARCHAR(1000) | nullable |
| `system_prompt_id` | VARCHAR(36) | nullable; current `system_prompts` version of the effective prompt (agent prompt + additions + runtime context, frozen at first start). NULL until first start |
| `model` | VARCHAR(200) | nullable; the session's own model (PUT `/sessions/{id}/model`). NULL uses the agent's `openai_model` |
| `model_params` | JSON | nullable; the session's own Chat Completions params, replacing the agent's `config.model_params` as a whole. NULL uses the agent's |
| `status` | VARCHAR(30) | default `'INITIALIZING'` |
| `created_at` | DATETIME(3) | NOT NULL |
| `updated_at` | DATETIME(3) | NOT NULL |
| `last_message_at` | DATETIME(3) | nullable |
| `deleted_at` | DATETIME(3) | nullable; NULL means visible, non-NULL means soft-deleted |

Indexes: `idx_sessions_agent (agent_id)`, `idx_sessions_parent (parent_session_id)`.

`model` / `model_params`, `branched_from_*` and `branched_cutoff_at` are added
to existing databases on startup (NULL). Branches made before `branched_from_*`
existed were linked through `parent_session_id` and hold copied rows; they are
not migrated.

**Branches borrow, they don't copy.** A branch has no rows for the history it
was made from: reading its messages returns the source's rows created before
`branched_cutoff_at` (recursively for a branch of a branch), then its own
rows. Borrowed rows keep their ids and are rebuilt as they were when the
branch was made (`app/core/borrowed.py::as_of`): rows are never edited after
they are written, only `deleted_at` set and a reason merged into `metadata`,
so a row the source deletes or compresses later stays live in the branch.
API responses mark them `metadata.borrowed_from = <owning session id>`; a
branch can't delete, rewrite or compress them (400 / compression skipped).
`IAgentDatabase.get_own_messages` reads only a session's own rows;
`get_messages` adds the borrowed ones.

**fork-dispatch joins.** `POST /sessions/{id}/fork-dispatch` branches the
session once per message; each branch has `join_into_session_id` set. When a
branch's first turn ends, the session it came from gets two rows, written on
its runner queue between its own turns (`app/core/branch_join.py`): a user
row with `metadata.cagent_name = "branch_summary"` and
`metadata.joined_from = {session_id, turn_id, summary_id, task, ok}` (content:
`config.branch_join_template` filled with the task and the result), and a
`turn_end` mark. The branch's rows are not copied; expanding the summary
reads them from the branch. Pending joins are redone at startup.

API responses add `pending_joins` (live branches not joined back yet,
computed from `join_into_session_id` / `joined_message_id`) and report
`status` as `DISPATCHING` for an idle session with `dispatch_mode` on; that
value is never stored in `status`.

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
| `from_session_id` | VARCHAR(100) | nullable source id: another session id, or `plugin:{plugin_id}` for messages a plugin sent |
| `created_at` | DATETIME(3) | NOT NULL |
| `deleted_at` | DATETIME(3) | nullable; NULL means visible, non-NULL means soft-deleted |

Index: `idx_messages_session (session_id)`. FK: `session_id → sessions(id) ON DELETE CASCADE`.

## `plugin_service_runs`
One row per start of a plugin service (global, or one instance per session), including automatic restarts.

| Column | Type | Notes |
|---|---|---|
| `id` | VARCHAR(36) | PK |
| `plugin_id` | VARCHAR(40) | NOT NULL |
| `service` | VARCHAR(40) | service name declared by the plugin |
| `session_id` | VARCHAR(36) | nullable; set for per-session services |
| `status` | VARCHAR(20) | `starting` \| `running` \| `stopping` \| `exited` \| `errored` |
| `restart_count` | INT | automatic restarts before this run |
| `started_at` | DATETIME(3) | NOT NULL |
| `running_at` | DATETIME(3) | nullable |
| `exited_at` | DATETIME(3) | nullable |
| `exit_code` | INT | nullable (subprocess services only) |
| `error` | TEXT | nullable error summary |

Indexes: `(plugin_id, started_at)`, `status`. Runs still active at startup are marked `errored` ("backend restarted while the service ran").

## `plugin_service_logs`
Log lines written by plugins (`ctx.log`, except debug) and by the host about them. The newest 20000 lines per plugin are kept.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT | auto-increment PK |
| `plugin_id` | VARCHAR(40) | NOT NULL |
| `run_id` | VARCHAR(36) | nullable; the service run that wrote it (NULL for hooks, tools, actions) |
| `session_id` | VARCHAR(36) | nullable |
| `ts` | DATETIME(3) | NOT NULL |
| `level` | VARCHAR(10) | `info` \| `warning` \| `error` |
| `line` | TEXT | NOT NULL |

Indexes: `(plugin_id, id)`, `(run_id, id)`, `(session_id, id)`.

The v1 tables `plugin_instances`, `plugin_runs` and `plugin_logs` are dropped at startup if present.

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
