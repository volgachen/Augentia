import type {
  ApiFormat,
  AgentTemplate,
  AgentType,
  CompressOptions,
  CompressResult,
  FailedJoin,
  Session,
  SessionCreateMode,
  SessionModelView,
  Message,
  ShareState,
  SharedMessageLocation,
  SharedSession,
  PluginInfo,
  PluginLogEntry,
  PluginServiceRun,
  ServiceState,
  SessionPluginInfo,
  Task,
  ToolInfo,
} from "./types";

import { API_BASE, WS_API_BASE } from "./backend";

// Backend address: see ./backend (default: this host, port 12598; changeable
// in the settings dialog behind the "Augentia" title).
const BASE = API_BASE;
const WS_BASE = WS_API_BASE;

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

export interface CreateAgentPayload {
  name: string;
  description: string;
  agent_type: AgentType;
  system_prompt: string;
  tool_names: string[];
  config?: Record<string, unknown>;
  openai_model: string;
  openai_base_url: string | null;
  openai_api_format?: ApiFormat;
}

export type UpdateAgentPayload = Partial<Omit<CreateAgentPayload, "agent_type">>;

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
    list: () => request<ToolInfo[]>("/tools"),
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
    setDispatchMode: (id: string, enabled: boolean) =>
      request<Session>(`/sessions/${id}/dispatching`, {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      }),
    failedJoins: (id: string) => request<FailedJoin[]>(`/sessions/${id}/failed-joins`),
    ackFailedJoins: (id: string) =>
      request<Session>(`/sessions/${id}/failed-joins/ack`, { method: "POST" }),
    unlinkParent: (id: string) =>
      request<Session>(`/sessions/${id}/parent`, { method: "DELETE" }),
    get: (id: string) => request<Session>(`/sessions/${id}`),
    list: () => request<Session[]>("/sessions"),
    systemPrompt: (id: string) =>
      request<{ system_prompt: string; source?: string }>(`/sessions/${id}/system-prompt`),
    updateSystemPrompt: (id: string, system_prompt: string) =>
      request<{ system_prompt: string; applied: "live_adapter" | "persisted" | "runner_restarted" }>(
        `/sessions/${id}/system-prompt`,
        { method: "PUT", body: JSON.stringify({ system_prompt }) },
      ),
    updateWorkingDir: (id: string, working_dir: string, update_prompt = true) =>
      request<{ session: Session; prompt_updated: boolean }>(`/sessions/${id}/working-dir`, {
        method: "PUT",
        body: JSON.stringify({ working_dir, update_prompt }),
      }),
    model: (id: string) => request<SessionModelView>(`/sessions/${id}/model`),
    locate: (id: string, messageId: string) =>
      request<SharedMessageLocation>(
        `/sessions/${encodeURIComponent(id)}/locate/${encodeURIComponent(messageId)}`,
      ),
    updateModel: (
      id: string,
      model: string | null,
      model_params: Record<string, unknown> | null,
      api_format: ApiFormat | null,
    ) =>
      request<SessionModelView & { session: Session }>(`/sessions/${id}/model`, {
        method: "PUT",
        body: JSON.stringify({ model, model_params, api_format }),
      }),
    config: (id: string) => request<Record<string, unknown>>(`/sessions/${id}/config`),
    updateConfig: (id: string, config: Record<string, unknown>) =>
      request<Record<string, unknown>>(`/sessions/${id}/config`, {
        method: "PUT",
        body: JSON.stringify({ config }),
      }),
    plugins: (id: string) => request<SessionPluginInfo[]>(`/sessions/${id}/plugins`),
    updatePlugin: (
      id: string,
      pluginId: string,
      body: { enabled?: boolean; config?: Record<string, unknown> },
    ) =>
      request<SessionPluginInfo>(`/sessions/${id}/plugins/${pluginId}`, {
        method: "PUT",
        body: JSON.stringify(body),
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
    // Several entries at once (multi-select); all or nothing.
    deleteMessages: (id: string, messageIds: string[]) =>
      request<{ deleted_ids: string[] }>(`/sessions/${id}/messages/delete`, {
        method: "POST",
        body: JSON.stringify({ message_ids: messageIds }),
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
    // includeDeleted adds rows removed with the delete button.
    get: (id: string, includeDeleted = false) =>
      request<SharedSession>(
        `/share/${encodeURIComponent(id)}${includeDeleted ? "?include_deleted=true" : ""}`,
      ),
    compressedMessages: (id: string, summaryId: string) =>
      request<Message[]>(
        `/share/${encodeURIComponent(id)}/messages/${encodeURIComponent(summaryId)}/compressed`,
      ),
    locate: (id: string, messageId: string) =>
      request<SharedMessageLocation>(
        `/share/${encodeURIComponent(id)}/locate/${encodeURIComponent(messageId)}`,
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
    list: () => request<PluginInfo[]>("/plugins"),
    get: (id: string) => request<PluginInfo>(`/plugins/${id}`),
    reload: () => request<{ changed: string[]; plugins: PluginInfo[] }>("/plugins/reload", { method: "POST" }),
    setEnabled: (id: string, enabled: boolean) =>
      request<PluginInfo>(`/plugins/${id}/enabled`, {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      }),
    globalConfig: (id: string) => request<Record<string, unknown>>(`/plugins/${id}/config`),
    setGlobalConfig: (id: string, config: Record<string, unknown>) =>
      request<Record<string, unknown>>(`/plugins/${id}/config`, {
        method: "PUT",
        body: JSON.stringify({ config }),
      }),
    service: (id: string, service: string, op: "start" | "stop" | "restart", sessionId?: string | null) =>
      request<ServiceState>(
        `/plugins/${id}/services/${service}/${op}${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ""}`,
        { method: "POST" },
      ),
    serviceRuns: (id: string, params: { service?: string; sessionId?: string | null; limit?: number } = {}) =>
      request<PluginServiceRun[]>(`/plugins/${id}/runs${query({
        service: params.service,
        session_id: params.sessionId ?? undefined,
        limit: params.limit,
      })}`),
    logs: (id: string, params: { sessionId?: string | null; runId?: string | null; limit?: number } = {}) =>
      request<PluginLogEntry[]>(`/plugins/${id}/logs${query({
        session_id: params.sessionId ?? undefined,
        run_id: params.runId ?? undefined,
        limit: params.limit,
      })}`),
    action: (id: string, action: string, sessionId: string | null, data: Record<string, unknown> = {}) =>
      request<{ result: unknown }>(`/plugins/${id}/actions/${action}`, {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, data }),
      }),
  },
};

export function createSessionSocket(sessionId: string): WebSocket {
  return new WebSocket(`${WS_BASE}/sessions/${sessionId}/stream`);
}

// WS /plugins/stream; sessionId limits it to one session's items (plus global ones).
export function createPluginsStream(sessionId?: string | null): WebSocket {
  const q = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  return new WebSocket(`${WS_BASE}/plugins/stream${q}`);
}

function query(params: Record<string, string | number | undefined>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== "");
  if (entries.length === 0) return "";
  return "?" + entries.map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&");
}
