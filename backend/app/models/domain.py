from enum import Enum
from datetime import datetime, timezone
from typing import Any, Optional
from pydantic import BaseModel, Field
import uuid


class SessionStatus(str, Enum):
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    # A turn is blocked awaiting a human decision on a tool call (see
    # StreamEventType.TOOL_CONFIRM). Distinct from WAITING_USER, which means the
    # turn finished and the agent is idle.
    WAITING_CONFIRM = "WAITING_CONFIRM"
    # Waiting on the summarizer: turn compression at the end of a turn, a
    # threshold-triggered whole-history compression, or POST /compress.
    # Busy like RUNNING (stored, reset after a restart like RUNNING).
    COMPRESSING = "COMPRESSING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    # Never stored: reported (Session.status in API responses) for a session
    # whose dispatch mode is on (Session.dispatch_mode) while it is idle.
    # Each message sent to it runs in a new branch whose result is joined
    # back; Session.pending_joins says how many are still running. The
    # stored status keeps saying what the session itself is doing.
    DISPATCHING = "DISPATCHING"


class JoinOutcome(str, Enum):
    """Session.join_outcome: how a fork-dispatch branch was joined back."""
    # Result appended to the target's context.
    OK = "ok"
    # Someone stopped the branch (cancelled / interrupted): kept out of context.
    DROPPED = "dropped"
    # The branch's turn failed: kept out of context, counted in the target's
    # failed_joins until acknowledged.
    FAILED = "failed"
    FAILED_ACKED = "failed_acked"


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    AGENT = "agent"
    TOOL_CALL = "tool_call"
    TOOL = "tool"
    # CAgent framework bookkeeping rows (turn_end / compression_started /
    # compression_completed). Content is JSON {"name", "metadata", "content"}.
    # Persisted for the agent loop's turn accounting and compression boundaries;
    # filtered out of client-facing message listings.
    MARK = "mark"


class AgentType(str, Enum):
    TOOL_USE = "tool_use"


class SystemPrompt(BaseModel):
    """One immutable version of a system prompt (table system_prompts).

    Agents and sessions point at their current version by id; changing a
    prompt adds a version whose previous_id is the one it replaces, so
    following previous_id from the current version gives the full history.
    Versions are never updated or deleted, so a diverged session can share
    its source's version.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content: str
    previous_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentTemplate(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    agent_type: AgentType
    # Resolved text of the current version; the table stores system_prompt_id.
    system_prompt: str
    system_prompt_id: Optional[str] = None
    # tool_use agent config
    tool_names: list[str] = Field(default_factory=list)
    # Per-agent configuration bag. Currently holds `tool_approvals`, a
    # {tool_name: requires_approval} dict that overrides each tool's class-level
    # default (BaseTool.requires_approval). Only tools the agent wants to deviate
    # on need an entry; everything else falls back to the tool default. Kept as a
    # generic dict so future per-agent knobs (e.g. max_iterations) can live here
    # without another schema migration. Only honored by the tool_use adapter.
    config: dict = Field(default_factory=dict)
    openai_model: str = "gpt-4o"
    openai_base_url: Optional[str] = None
    # Wire format of the model endpoint: "chat" (/chat/completions) or
    # "responses" (/responses). Which one works depends on what the gateway
    # serves for the model; sessions may override it (Session.api_format).
    openai_api_format: str = "chat"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Session(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str
    # Human-friendly label for the session. Auto-seeded at creation (agent name +
    # time, or a spawning caller's description) and renameable via PATCH. Optional
    # so rows created before this column exists still load.
    title: Optional[str] = None
    working_dir: Optional[str] = None
    # Session that spawned this one via POST /sessions, if any. None means the
    # session was created directly by an operator. Used to render the agent
    # derivation tree and to let the child report results back to its parent.
    parent_session_id: Optional[str] = None
    # Set on a branch made by diverge: the session it was branched from and
    # the message it branched at (the divergence point the user picked; the
    # branch holds a copy of the history before it, or through it for a
    # summary). Separate from parent_session_id, which is for sub-agents.
    branched_from_session_id: Optional[str] = None
    branched_from_message_id: Optional[str] = None
    # When the branch was made. The branch does not copy the source's history:
    # it borrows the source's rows created before this time, as they were at
    # this time (rows the source deletes or compresses later still show).
    branched_cutoff_at: Optional[datetime] = None
    # Set on a branch made by fork-dispatch: when its first turn ends, a
    # summary of that turn is appended to this session (the one it was
    # branched from). joined_message_id is that appended row once it exists;
    # until then the join is pending (and is retried after a restart).
    join_into_session_id: Optional[str] = None
    joined_message_id: Optional[str] = None
    # How that join went (see JoinOutcome); None until joined. A failed
    # branch is joined but kept out of the target's context, and counts in
    # the target's failed_joins until someone acknowledges it.
    join_outcome: Optional[str] = None
    # Dispatch mode (PUT /sessions/{id}/dispatching): every message sent to
    # the session (POST /messages) runs in a new branch of it, and the
    # branch's result is joined back. The session runs no turns of its own.
    dispatch_mode: bool = False
    # Not stored: how many branches dispatched from this session haven't been
    # joined back yet (filled in by the API; 0 = all results are in).
    pending_joins: int = 0
    # Not stored: branches dispatched from this session whose turn failed and
    # nobody has acknowledged yet (join_outcome == "failed").
    failed_joins: int = 0
    # Per-session add-ons to the AgentTemplate's base system_prompt. Persisted so
    # a session rehydrated after a backend restart sees the exact same effective
    # prompt it was launched with. The router applies them at adapter.start()
    # time; the database itself just stores them. additional_prompt is inline
    # text; additional_prompt_path is a server-side path read at start (and
    # re-read on rehydrate).
    additional_prompt: Optional[str] = None
    additional_prompt_path: Optional[str] = None
    # Effective system prompt of this session (the current version's text).
    # Unlike additional_prompt_path, this is not recomputed after launch; it
    # preserves the exact effective prompt for rehydration and debugging. The
    # table stores system_prompt_id; editing the prompt adds a version.
    system_prompt_snapshot: Optional[str] = None
    system_prompt_id: Optional[str] = None
    # Per-session model override (PUT /sessions/{id}/model). None uses the
    # agent's openai_model / config.model_params. model_params replaces the
    # agent's params as a whole (no merge), like compression_model_params.
    model: Optional[str] = None
    model_params: Optional[dict] = None
    # Per-session wire format ("chat" / "responses"); None uses the agent's
    # openai_api_format. Set together with model in PUT /sessions/{id}/model.
    api_format: Optional[str] = None
    status: SessionStatus = SessionStatus.INITIALIZING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # created_at of the newest message in this session, or None while it has no
    # messages. Maintained on every add_message (a stored column, not derived on
    # read) so the dashboard can sort on it without joining the messages table.
    last_message_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    role: MessageRole
    content: str
    from_session_id: Optional[str] = None
    # Structured extras that don't belong in the text content. Currently only
    # user rows use it: {"images": [{"ref": "att:<sha256>", "detail"?: ...}]},
    # image references attached to the turn (bytes live in the attachment
    # store). Empty for every other row.
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deleted_at: Optional[datetime] = None


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class Task(BaseModel):
    # Per-list monotonic integer, stored as a string (e.g. "1", "2") so agents
    # can reference tasks as "#1". Assigned by the DB's task_counters, never reused.
    id: str
    task_list_id: str
    subject: str
    description: str
    active_form: Optional[str] = None  # present-continuous form for spinners
    owner: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    blocks: list[str] = Field(default_factory=list)       # task IDs this task blocks
    blocked_by: list[str] = Field(default_factory=list)   # task IDs that block this one
    metadata: Optional[dict] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---- v2 plugin services (app/plugins/README.md §6, §11.3) ----

class PluginServiceRun(BaseModel):
    """One start of a plugin service (a global one, or one per session)."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    plugin_id: str
    service: str
    session_id: str | None = None
    # starting / running / stopping / exited / errored (README §6.2).
    status: str = "starting"
    # How many automatic restarts preceded this run.
    restart_count: int = 0
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    running_at: datetime | None = None
    exited_at: datetime | None = None
    exit_code: int | None = None
    error: str | None = None


class PluginServiceLog(BaseModel):
    """A log line written by a plugin (ctx.log) or by the host about it."""

    id: int | None = None
    plugin_id: str
    # The service run it came from; None for hooks, tools, actions, on_load.
    run_id: str | None = None
    session_id: str | None = None
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # debug / info / warning / error
    level: str = "info"
    line: str

