export type AgentType = "tool_use";

export type SessionStatus =
  | "INITIALIZING"
  | "RUNNING"
  | "WAITING_USER"
  | "WAITING_CONFIRM"
  | "COMPLETED"
  | "ERROR";

export type MessageRole = "system" | "user" | "agent" | "tool_call" | "tool" | "mark";

export type StreamEventType =
  | "text"
  | "assistant_message"
  | "tool_call"
  | "tool_confirm"
  | "tool_result"
  | "status"
  | "error"
  | "done"
  | "user"
  // Client-only: a compression_completed mark loaded from history.
  | "compression";

export interface AgentTemplate {
  id: string;
  name: string;
  description: string;
  agent_type: AgentType;
  system_prompt: string;
  tool_names: string[];
  // Per-agent config bag. config.tool_approvals is a { toolName: requiresApproval }
  // map overriding each tool's backend default; other keys reserved for future use.
  config: {
    tool_approvals?: Record<string, boolean>;
    // Agent loop implementation; the backend defaults to "cagent".
    engine?: "cagent" | "legacy";
    // Accept image input (cagent engine only; the model must support vision).
    vision?: boolean;
    image_detail?: ImageDetail;
    // Extra Chat Completions parameters merged into every model request
    // (temperature, reasoning_effort, max_tokens, extra_body, ...).
    model_params?: Record<string, unknown>;
    // Summarizer-only replacement for model_params (CAgent engine).
    compression_model_params?: Record<string, unknown>;
    [key: string]: unknown;
  };
  openai_model: string;
  openai_base_url: string | null;
  created_at: string;
}

export type ImageDetail = "auto" | "low" | "high";

// A stored image reference attached to a user turn. Bytes live in the
// backend attachment store; ref is "att:<sha256>".
export interface ImageRef {
  ref: string;
  detail?: ImageDetail;
  // Interleaved content only: character offset in the message/tool result
  // text where the image sits. Absent = after all text.
  at?: number;
}

export function agentAcceptsImages(agent: AgentTemplate | null | undefined): boolean {
  if (!agent) return false;
  return (agent.config.engine ?? "cagent") === "cagent" && agent.config.vision === true;
}

export type SessionCreateMode =
  | "use_existing_directory"
  | "duplicate_by_copy"
  | "git_worktree";

export interface Session {
  id: string;
  agent_id: string;
  title: string | null;
  working_dir: string | null;
  parent_session_id: string | null;
  additional_prompt: string | null;
  additional_prompt_path: string | null;
  status: SessionStatus;
  created_at: string;
  updated_at: string;
  // created_at of the newest message; null for a session with no messages yet.
  last_message_at: string | null;
  deleted_at: string | null;
}

export interface Message {
  id: string;
  session_id: string;
  role: MessageRole;
  content: string;
  from_session_id: string | null;
  // User and tool rows may carry { images: ImageRef[] }.
  metadata?: { images?: ImageRef[]; [key: string]: unknown };
  created_at: string;
  deleted_at: string | null;
}

export interface ShareState {
  enabled: boolean;
  updated_at: string | null;
}

// Public read-only view of a shared session (GET /share/{id}).
export interface SharedSession {
  session: {
    id: string;
    title: string | null;
    agent_name: string | null;
    status: SessionStatus;
    created_at: string;
    last_message_at: string | null;
  };
  messages: Message[];
}

export interface CompressOptions {
  // Compress only the turns from this time on (ISO 8601), or from `hours`
  // ago; earlier history stays. Omit both for whole-history compression.
  since?: string;
  hours?: number;
  // Replaces the summarizer instruction.
  instruction?: string;
}

export interface CompressResult {
  status: "compressed" | "skipped" | "failed";
  kind?: "session" | "range";
  reason?: string;
  num_tokens_before?: number | null;
  num_tokens_after?: number | null;
  token_count_is_estimate?: boolean | null;
}

export interface StreamEvent {
  type: StreamEventType;
  data: string;
  message_id?: string;
  // On "user" events: images attached to that turn; on "tool_result" events:
  // images the tool returned.
  images?: ImageRef[] | null;
  // Client-only: set on history rows the user deleted, shown only when
  // "显示已删除消息" is on.
  deleted?: boolean;
  // Client-only, on "compression" events loaded from history.
  compression?: CompressionInfo;
}

export interface CompressionInfo {
  // "session": whole-history compression (a compression_completed mark).
  // "turn": turn-scoped compression (a turn_summary user row). "range":
  // compress_session(since=...) (a range_summary user row). The rows a summary
  // replaced are soft-deleted with metadata.compressed_by = its id and are
  // fetched on demand (GET .../messages/{id}/compressed).
  kind: "session" | "turn" | "range";
  // How many rows a turn/range summary lists as replaced (a hint only).
  replacedCount?: number;
}

export type TaskStatus = "pending" | "in_progress" | "completed";

// Mirror of backend app/models/domain.py::Task. The id is a per-list monotonic
// integer rendered as a string ("1", "2"); task_list_id is the session id.
export interface Task {
  id: string;
  task_list_id: string;
  subject: string;
  description: string;
  active_form: string | null;
  owner: string | null;
  status: TaskStatus;
  blocks: string[];
  blocked_by: string[];
  metadata: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export type PluginScope = "system_side" | "session_side" | "hybrid";

export type PluginStatus =
  | "stopped"
  | "starting"
  | "waiting_input"
  | "running"
  | "stopping"
  | "exited"
  | "errored"
  | "cancelled";

export interface PluginEntrySpec {
  type: string;
  main: string | null;
  skill: string | null;
  scripts_dir: string | null;
}

export interface PluginSessionSpec {
  selectable: boolean;
  default_enabled: boolean;
}

export interface PluginDefinition {
  id: string;
  name: string;
  version: string;
  scope: PluginScope;
  has_session_dialog: boolean;
  provider: string;
  description: string;
  entry: PluginEntrySpec;
  capabilities: string[];
  subscriptions: string[];
  actions: string[];
  default_config: Record<string, unknown>;
  session: PluginSessionSpec;
  root: string;
}

export interface PluginInstance {
  id: string;
  plugin_id: string;
  display_name: string;
  status: PluginStatus;
  config: Record<string, unknown> | null;
  auto_start: boolean;
  current_run_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface PluginRun {
  id: string;
  plugin_instance_id: string;
  plugin_id: string;
  status: PluginStatus;
  config_snapshot: Record<string, unknown> | null;
  started_at: string | null;
  running_at: string | null;
  exited_at: string | null;
  exit_code: number | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export type PluginLogStream = "stdout" | "stderr" | "system" | "plugin";

export interface PluginLogLine {
  id?: number | null;
  plugin_instance_id?: string;
  plugin_run_id?: string;
  session_id?: string | null;
  ts: string;
  stream: PluginLogStream;
  level?: string | null;
  line: string;
}

export type PluginWsFrame =
  | { type: "plugin_instance_state"; data: PluginInstance }
  | { type: "log"; data: PluginLogLine }
  | { type: "plugin_event"; data: Record<string, unknown> }
  | { type: "status"; data: { status: PluginStatus; error?: string | null; run_id?: string | null } }
  | { type: "logs_cleared"; data: null }
  | { type: "error"; data: string };
