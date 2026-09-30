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
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


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


class AgentTemplate(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    agent_type: AgentType
    system_prompt: str
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
    # Per-session add-ons to the AgentTemplate's base system_prompt. Persisted so
    # a session rehydrated after a backend restart sees the exact same effective
    # prompt it was launched with. The router applies them at adapter.start()
    # time; the database itself just stores them. additional_prompt is inline
    # text; additional_prompt_path is a server-side path read at start (and
    # re-read on rehydrate).
    additional_prompt: Optional[str] = None
    additional_prompt_path: Optional[str] = None
    # Final system prompt snapshot used to start this session. Unlike
    # additional_prompt_path, this is not recomputed after launch; it preserves the
    # exact effective prompt for rehydration and debugging.
    system_prompt_snapshot: Optional[str] = None
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


class PluginStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    WAITING_INPUT = "waiting_input"
    RUNNING = "running"
    STOPPING = "stopping"
    EXITED = "exited"
    ERRORED = "errored"
    CANCELLED = "cancelled"


class PluginInstance(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    plugin_id: str
    display_name: str
    status: PluginStatus = PluginStatus.STOPPED
    config: dict[str, Any] | None = None
    auto_start: bool = False
    current_run_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PluginRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    plugin_instance_id: str
    plugin_id: str
    status: PluginStatus = PluginStatus.STARTING
    config_snapshot: dict[str, Any] | None = None
    started_at: datetime | None = None
    running_at: datetime | None = None
    exited_at: datetime | None = None
    exit_code: int | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PluginLog(BaseModel):
    id: int | None = None
    plugin_instance_id: str
    plugin_run_id: str
    session_id: str | None = None
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    stream: str
    level: str | None = None
    line: str

