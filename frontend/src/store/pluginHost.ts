import { create } from "zustand";
import { api, createPluginsStream } from "../api/client";
import type {
  PluginInfo,
  PluginLogEntry,
  PluginStreamFrame,
  PluginUIState,
  ServiceState,
} from "../api/types";

// Live state of the v2 plugin host, fed by WS /plugins/stream
// (backend/app/plugins/README.md §10.5). One global connection (no session
// filter) serves the plugin page and every session panel; components call
// connect() on mount and disconnect() on unmount (reference counted). When
// the socket drops, it reconnects after a delay while anyone still needs it.

const MAX_LOGS = 2000;
const RECONNECT_MS = 3000;

type Key = string; // `${plugin_id}|${service}|${session_id ?? ""}` or `${plugin_id}|${session_id ?? ""}`

export const serviceKey = (s: { plugin_id: string; service: string; session_id: string | null }): Key =>
  `${s.plugin_id}|${s.service}|${s.session_id ?? ""}`;
export const uiKey = (pluginId: string, sessionId: string | null): Key => `${pluginId}|${sessionId ?? ""}`;

interface Store {
  plugins: Record<string, PluginInfo>;
  services: Record<Key, ServiceState>;
  ui: Record<Key, PluginUIState>;
  logs: PluginLogEntry[];
  connected: boolean;
  error: string | null;
  connect: () => void;
  disconnect: () => void;
  refresh: () => Promise<void>;
  applyPlugin: (p: PluginInfo) => void;
  applyService: (s: ServiceState) => void;
}

let socket: WebSocket | null = null;
let users = 0;
let retry: ReturnType<typeof setTimeout> | null = null;

export const usePluginHostStore = create<Store>((set) => {
  const open = () => {
    if (socket || users === 0) return;
    const ws = createPluginsStream();
    socket = ws;
    ws.onopen = () => set({ connected: true, error: null });
    ws.onmessage = (e) => {
      const frame = JSON.parse(e.data) as PluginStreamFrame;
      set((s) => reduce(s, frame));
    };
    ws.onclose = () => {
      if (socket === ws) socket = null;
      set({ connected: false });
      if (users > 0 && retry === null) {
        retry = setTimeout(() => {
          retry = null;
          open();
        }, RECONNECT_MS);
      }
    };
    ws.onerror = () => set({ error: "Live plugin updates disconnected; retrying…" });
  };

  return {
    plugins: {},
    services: {},
    ui: {},
    logs: [],
    connected: false,
    error: null,

    connect: () => {
      users += 1;
      open();
    },

    disconnect: () => {
      users = Math.max(0, users - 1);
      if (users === 0) {
        if (retry !== null) {
          clearTimeout(retry);
          retry = null;
        }
        const ws = socket;
        socket = null;
        ws?.close();
      }
    },

    refresh: async () => {
      const list = await api.plugins.list();
      set((s) => {
        const plugins: Record<string, PluginInfo> = {};
        const services = { ...s.services };
        const ui = { ...s.ui };
        for (const p of list) {
          plugins[p.id] = p;
          for (const sv of p.services) services[serviceKey(sv)] = sv;
          if (p.ui) ui[uiKey(p.id, null)] = p.ui;
        }
        return { plugins, services, ui };
      });
    },

    applyPlugin: (p) => set((s) => reduce(s, { type: "plugin", data: p })),
    applyService: (sv) => set((s) => reduce(s, { type: "service", data: sv })),
  };
});

function reduce(s: Store, frame: PluginStreamFrame): Partial<Store> {
  switch (frame.type) {
    case "snapshot": {
      const plugins: Record<string, PluginInfo> = {};
      for (const p of frame.data.plugins) plugins[p.id] = p;
      const services: Record<Key, ServiceState> = {};
      for (const sv of frame.data.services) services[serviceKey(sv)] = sv;
      const ui: Record<Key, PluginUIState> = {};
      for (const u of frame.data.ui) ui[uiKey(u.plugin_id, u.session_id)] = u;
      return { plugins, services, ui, logs: frame.data.logs.slice(-MAX_LOGS) };
    }
    case "plugin": {
      const p = frame.data;
      const services = { ...s.services };
      const ui = { ...s.ui };
      // A disabled plugin's services and UI are gone server-side.
      if (p.state !== "loaded") {
        for (const k of Object.keys(services)) if (k.startsWith(`${p.id}|`)) delete services[k];
        for (const k of Object.keys(ui)) if (k.startsWith(`${p.id}|`)) delete ui[k];
      }
      for (const sv of p.services) services[serviceKey(sv)] = sv;
      return { plugins: { ...s.plugins, [p.id]: p }, services, ui };
    }
    case "service":
      return { services: { ...s.services, [serviceKey(frame.data)]: frame.data } };
    case "ui":
      return { ui: { ...s.ui, [uiKey(frame.data.plugin_id, frame.data.session_id)]: frame.data } };
    case "log": {
      const logs = s.logs.length >= MAX_LOGS ? s.logs.slice(-MAX_LOGS + 1) : s.logs.slice();
      logs.push(frame.data);
      return { logs };
    }
  }
}
