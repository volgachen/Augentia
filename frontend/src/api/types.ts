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

// GET /tools: one entry per selectable tool (built-in, or from a loaded plugin).
export interface ToolInfo {
  name: string;
  description: string;
  source: "builtin" | "plugin";
  plugin_id: string | null;
  plugin_name: string | null;
  // Default confirm policy; config.tool_approvals overrides it per agent.
  requires_approval: boolean;
}

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
    // Accept image input (the model must support vision).
    vision?: boolean;
    image_detail?: ImageDetail;
    // Extra Chat Completions parameters merged into every model request
    // (temperature, reasoning_effort, max_tokens, extra_body, ...).
    model_params?: Record<string, unknown>;
    // Summarizer-only replacement for model_params.
    compression_model_params?: Record<string, unknown>;
    // Append the runtime context block (OS, cwd, session start time, shell
    // notes) to the system prompt when a session starts. Defaults to true.
    append_runtime_context?: boolean;
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
  return agent.config.vision === true;
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
  // The session's own model / Chat Completions params; null = the agent's.
  model?: string | null;
  model_params?: Record<string, unknown> | null;
  status: SessionStatus;
  created_at: string;
  updated_at: string;
  // created_at of the newest message; null for a session with no messages yet.
  last_message_at: string | null;
  deleted_at: string | null;
}

// GET/PUT /sessions/{id}/model: the session's values (null = not set) and
// what the next turn uses after falling back to the agent.
export interface SessionModelView {
  model: string | null;
  model_params: Record<string, unknown> | null;
  agent_model: string;
  agent_model_params: Record<string, unknown>;
  effective_model: string;
  effective_model_params: Record<string, unknown>;
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

// Where a linked message sits on the share page (GET /share/{id}/locate/{mid}).
export interface SharedMessageLocation {
  message_id: string;
  role: Message["role"];
  // Summary ids to expand, outermost first, before the message is visible.
  expand: string[];
  // Removed with the delete button: visible only with "show deleted" on.
  deleted: boolean;
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

// ---- v2 plugin host (backend/app/plugins/README.md) ----

// A JSON Schema as written in plugin.json (config.global / config.session).
export type JsonSchema = Record<string, unknown>;

// GET /sessions/{id}/plugins: one plugin as seen from a session.
export interface SessionPluginInfo {
  id: string;
  name: string;
  description: string;
  state: "loaded" | "disabled" | "error";
  error: string | null;
  // Hooks and session services run only when enabled (tools do not depend on it).
  enabled: boolean;
  default_enabled: boolean;
  // Effective config: schema defaults applied, x-secret values masked.
  config: Record<string, unknown>;
  schema: JsonSchema | null;
  // The stored entry no longer matches the schema (e.g. after a plugin upgrade).
  config_errors: string[];
  tools: string[];
  hooks: string[];
  session_services: string[];
  actions: string[];
  // This session's service instances and the plugin's UI card for it.
  services: ServiceState[];
  ui: PluginUIState | null;
}

export type ServiceStatus =
  | "stopped"
  | "starting"
  | "running"
  | "stopping"
  | "exited"
  | "errored"
  | "backoff";

export interface ServiceState {
  plugin_id: string;
  service: string;
  session_id: string | null;
  scope: "global" | "session";
  restart: "never" | "on-failure" | "always";
  status: ServiceStatus;
  run_id: string | null;
  restart_count: number;
  error: string | null;
  next_restart_at: string | null;
  started_at: string | null;
}

export interface PluginUIAction {
  id: string;
  label: string;
  style?: "default" | "primary" | "danger";
}

// Status card a plugin pushes with ctx.ui.update (README §5.4).
export interface PluginUIState {
  plugin_id: string;
  session_id: string | null;
  status: "idle" | "waiting" | "ok" | "error";
  message: string | null;
  link: string | null;
  qr: string | null;
  actions: PluginUIAction[];
  updated_at: string;
}

export interface PluginLogEntry {
  id: number | null;
  plugin_id: string;
  run_id: string | null;
  session_id: string | null;
  ts: string;
  level: "debug" | "info" | "warning" | "error";
  line: string;
}

export interface PluginServiceRun {
  id: string;
  plugin_id: string;
  service: string;
  session_id: string | null;
  status: string;
  restart_count: number;
  started_at: string;
  running_at: string | null;
  exited_at: string | null;
  exit_code: number | null;
  error: string | null;
}

export interface PluginCapabilities {
  tools: { name: string; full_name: string; description: string; requires_approval: boolean }[];
  hooks: { event: string; priority: number; timeout: number; on_error: "skip" | "fail" }[];
  services: { name: string; scope: "global" | "session"; restart: string; autostart: boolean }[];
  actions: string[];
  http_routes: boolean;
}

// GET /plugins.
export interface PluginInfo {
  id: string;
  path: string;
  state: "loaded" | "disabled" | "error";
  enabled: boolean;
  error: string | null;
  missing_dependencies: string[];
  name: string | null;
  version: string | null;
  description: string;
  author: string;
  api_version: number | null;
  python_dependencies: string[];
  global_config_schema: JsonSchema | null;
  session_config_schema: JsonSchema | null;
  session_default_enabled: boolean;
  public_routes: string[];
  // Known only while loaded.
  capabilities: PluginCapabilities | null;
  services: ServiceState[];
  ui: PluginUIState | null;
}

// Frames of WS /plugins/stream.
export type PluginStreamFrame =
  | {
      type: "snapshot";
      data: {
        plugins: PluginInfo[];
        services: ServiceState[];
        ui: PluginUIState[];
        logs: PluginLogEntry[];
      };
    }
  | { type: "plugin"; data: PluginInfo }
  | { type: "service"; data: ServiceState }
  | { type: "ui"; data: PluginUIState }
  | { type: "log"; data: PluginLogEntry };
