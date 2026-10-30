import type {
  AgentTemplate,
  AgentType,
  CompressOptions,
  CompressResult,
  Session,
  SessionCreateMode,
  Message,
  PluginDefinition,
  PluginInstance,
  PluginLogLine,
  PluginRun,
  ShareState,
  SharedSession,
  Task,
} from "./types";

// Use the same host the browser connected to, so the app works on any machine in the LAN.
const API_HOST = `${window.location.hostname}:12598`;
const BASE = `http://${API_HOST}/api/v1`;
const WS_BASE = `ws://${API_HOST}/api/v1`;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (res.status === 401 && path !== "/auth/login") {
    window.dispatchEvent(new Event("augentia:unauthorized"));
  }
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`${res.status} ${detail}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export interface DirEntry {
  name: string;
  path: string;
  is_dir: boolean;
}

export interface UploadedAttachment {
  id: string;
  ref: string;
  mime: string;
  size: number;
}

// URL for displaying a stored image ("att:<sha256>" -> download endpoint).
export function attachmentUrl(ref: string): string {
  const id = ref.startsWith("att:") ? ref.slice(4) : ref;
  return `${BASE}/attachments/${encodeURIComponent(id)}`;
}

// Image URL inside a shared transcript; served without login, but only for
// images that appear in that shared session.
export function sharedAttachmentUrl(sessionId: string, ref: string): string {
  const id = ref.startsWith("att:") ? ref.slice(4) : ref;
  return `${BASE}/share/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(id)}`;
}

async function uploadAttachment(file: Blob): Promise<UploadedAttachment> {
  // Raw bytes body (not JSON / multipart); the backend sniffs the format.
  const res = await fetch(`${BASE}/attachments`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });
  if (res.status === 401) {
    window.dispatchEvent(new Event("augentia:unauthorized"));
  }
  if (!res.ok) {
    let detail = await res.text();
    try {
      detail = JSON.parse(detail).detail ?? detail;
    } catch {
      // keep raw text
    }
    throw new Error(detail);
  }
  return res.json() as Promise<UploadedAttachment>;
}

export interface BrowseResponse {
  path: string;
  parent: string | null;
  entries: DirEntry[];
}

function browseQuery(path: string | null | undefined): string {
  return path ? `?path=${encodeURIComponent(path)}` : "";
}

function pluginLogsQuery(limit?: number, sessionId?: string | null): string {
  const params = new URLSearchParams();
  if (limit != null) params.set("limit", String(limit));
  if (sessionId) params.set("session_id", sessionId);
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export interface CreateAgentPayload {
  name: string;
  description: string;
  agent_type: AgentType;
  system_prompt: string;
  tool_names: string[];
  config?: Record<string, unknown>;
  openai_model: string;
  openai_base_url: string | null;
}

export type UpdateAgentPayload = Partial<Omit<CreateAgentPayload, "agent_type">>;

export interface CreatePluginInstancePayload {
  plugin_id: string;
  display_name: string;
  config?: Record<string, unknown> | null;
  auto_start?: boolean;
}

export interface UpdatePluginInstancePayload {
  display_name?: string;
  config?: Record<string, unknown> | null;
  auto_start?: boolean;
}

export interface PluginCommandPayload {
  command: string;
  data?: Record<string, unknown>;
  timeout_ms?: number;
}

export const api = {
  auth: {
    me: () => request<{ authenticated: boolean; auth_enabled: boolean }>("/auth/me"),
    login: (password: string) =>
      request<{ authenticated: boolean; auth_enabled: boolean }>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ password }),
      }),
    logout: () => request<void>("/auth/logout", { method: "POST" }),
  },
  agents: {
    list: () => request<AgentTemplate[]>("/agents"),
    get: (id: string) => request<AgentTemplate>(`/agents/${id}`),
    create: (body: CreateAgentPayload) =>
      request<AgentTemplate>("/agents", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    update: (id: string, body: UpdateAgentPayload) =>
      request<AgentTemplate>(`/agents/${id}`, {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    delete: (id: string) =>
      request<void>(`/agents/${id}`, { method: "DELETE" }),
  },
  tools: {
    list: () => request<string[]>("/tools"),
  },
  attachments: {
    upload: uploadAttachment,
  },
  sessions: {
    create: (
      agent_id: string,
      source_dir: string | null,
      create_mode: SessionCreateMode = "use_existing_directory",
      title: string | null = null,
      additional_prompt: string | null = null,
      additional_prompt_path: string | null = null,
    ) =>
      request<Session>("/sessions", {
        method: "POST",
        body: JSON.stringify({
          agent_id,
          source_dir,
          create_mode,
          title,
          additional_prompt,
          additional_prompt_path,
        }),
      }),
    updateTitle: (id: string, title: string) =>
      request<Session>(`/sessions/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      }),
    get: (id: string) => request<Session>(`/sessions/${id}`),
    list: () => request<Session[]>("/sessions"),
    systemPrompt: (id: string) =>
      request<{ system_prompt: string; source?: string }>(`/sessions/${id}/system-prompt`),
    updateSystemPrompt: (id: string, system_prompt: string) =>
      request<{ system_prompt: string; applied: "live_adapter" | "persisted" | "runner_restarted" }>(
        `/sessions/${id}/system-prompt`,
        { method: "PUT", body: JSON.stringify({ system_prompt }) },
      ),
    config: (id: string) => request<Record<string, unknown>>(`/sessions/${id}/config`),
    updateConfig: (id: string, config: Record<string, unknown>) =>
      request<Record<string, unknown>>(`/sessions/${id}/config`, {
        method: "PUT",
        body: JSON.stringify({ config }),
      }),
    testToolPermission: (id: string, body: { tool: string; path?: string | null; args?: Record<string, unknown> }) =>
      request<{
        action: "allow" | "deny" | "ask";
        reason: string;
        rule_id: string | null;
        resolved_path: string | null;
      }>(`/sessions/${id}/tool-permissions/test`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    // includeDeleted adds rows the user deleted (not compressed ones).
    messages: (id: string, includeDeleted = false) =>
      request<Message[]>(
        `/sessions/${id}/messages?include_compression=true${includeDeleted ? "&include_deleted=true" : ""}`,
      ),
    retryMessage: (id: string, messageId: string, content: string) =>
      request<{ status: string }>(`/sessions/${id}/messages/${messageId}/retry`, {
        method: "POST",
        body: JSON.stringify({ content }),
      }),
    divergeFromMessage: (id: string, messageId: string) =>
      request<Session>(`/sessions/${id}/messages/${messageId}/diverge`, {
        method: "POST",
        body: JSON.stringify({}),
      }),
    compressedMessages: (id: string, summaryId: string) =>
      request<Message[]>(`/sessions/${id}/messages/${summaryId}/compressed`),
    deleteMessage: (id: string, messageId: string) =>
      request<{ deleted_ids: string[] }>(`/sessions/${id}/messages/${messageId}`, {
        method: "DELETE",
      }),
    share: (id: string) => request<ShareState>(`/sessions/${id}/share`),
    updateShare: (id: string, enabled: boolean) =>
      request<ShareState>(`/sessions/${id}/share`, {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      }),
    compress: (id: string, options?: CompressOptions) =>
      request<CompressResult>(`/sessions/${id}/compress`, {
        method: "POST",
        ...(options ? { body: JSON.stringify(options) } : {}),
      }),
    tasks: (id: string) => request<Task[]>(`/sessions/${id}/tasks`),
    delete: (id: string) =>
      request<void>(`/sessions/${id}`, { method: "DELETE" }),
  },
  share: {
    get: (id: string) => request<SharedSession>(`/share/${encodeURIComponent(id)}`),
    compressedMessages: (id: string, summaryId: string) =>
      request<Message[]>(
        `/share/${encodeURIComponent(id)}/messages/${encodeURIComponent(summaryId)}/compressed`,
      ),
  },
  fs: {
    browse: (path?: string | null) =>
      request<BrowseResponse>(`/fs/browse${browseQuery(path)}`),
    templates: (path?: string | null) =>
      request<BrowseResponse>(`/fs/templates${browseQuery(path)}`),
    home: () => request<{ home: string; templates_root: string }>("/fs/home"),
  },
  plugins: {
    catalog: () => request<PluginDefinition[]>("/plugins/catalog"),
    definition: (id: string) => request<PluginDefinition>(`/plugins/catalog/${id}`),
    instances: () => request<PluginInstance[]>("/plugins/instances"),
    instance: (id: string) => request<PluginInstance>(`/plugins/instances/${id}`),
    createInstance: (body: CreatePluginInstancePayload) =>
      request<PluginInstance>("/plugins/instances", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    updateInstance: (id: string, body: UpdatePluginInstancePayload) =>
      request<PluginInstance>(`/plugins/instances/${id}`, {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    deleteInstance: (id: string) =>
      request<void>(`/plugins/instances/${id}`, { method: "DELETE" }),
    startInstance: (id: string) =>
      request<PluginRun>(`/plugins/instances/${id}/start`, { method: "POST" }),
    stopInstance: (id: string) =>
      request<PluginInstance>(`/plugins/instances/${id}/stop`, { method: "POST" }),
    restartInstance: (id: string) =>
      request<PluginRun>(`/plugins/instances/${id}/restart`, { method: "POST" }),
    command: (id: string, body: PluginCommandPayload) =>
      request<Record<string, unknown>>(`/plugins/instances/${id}/commands`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    runs: (instanceId: string) =>
      request<PluginRun[]>(`/plugins/instances/${instanceId}/runs`),
    run: (runId: string) => request<PluginRun>(`/plugins/runs/${runId}`),
    runLogs: (runId: string, limit?: number, sessionId?: string | null) =>
      request<PluginLogLine[]>(`/plugins/runs/${runId}/logs${pluginLogsQuery(limit, sessionId)}`),
    instanceLogs: (instanceId: string, limit?: number, sessionId?: string | null) =>
      request<PluginLogLine[]>(`/plugins/instances/${instanceId}/logs${pluginLogsQuery(limit, sessionId)}`),
  },
};

export function createSessionSocket(sessionId: string): WebSocket {
  return new WebSocket(`${WS_BASE}/sessions/${sessionId}/stream`);
}

export function createPluginSocket(instanceId: string): WebSocket {
  return new WebSocket(`${WS_BASE}/plugins/instances/${instanceId}/stream`);
}
