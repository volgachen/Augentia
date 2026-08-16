import { create } from "zustand";
import type { Message, Session, SessionCreateMode, SessionStatus, StreamEvent, Task } from "../api/types";
import { api, createSessionSocket } from "../api/client";
import { useToastStore } from "./toasts";

interface PendingConfirm {
  call_id: string;
  name: string;
  args: unknown;
}

interface SessionEntry {
  session: Session;
  events: StreamEvent[];
  socket: WebSocket | null;
  tasks: Task[];
  // True when this background session newly needs user attention and has not
  // been opened since that state change.
  attentionUnread: boolean;
  // Tool calls awaiting a human approve/deny decision (see TOOL_CONFIRM).
  pendingConfirms: PendingConfirm[];
}

// Map a persisted Message into the StreamEvent shape the console renders, so
// REST-loaded history and live WS events share one render path.
function shouldMarkAttentionUnread(
  previous: SessionStatus,
  next: SessionStatus,
): boolean {
  return (
    previous !== next && (next === "WAITING_USER" || next === "WAITING_CONFIRM")
  );
}

function sessionDisplayName(session: Session): string {
  return session.title?.trim() || `Session ${session.id.slice(0, 8)}…`;
}

function toastTitle(session: Session): string {
  return `${sessionDisplayName(session)} · ${session.status}`;
}

function notifyWaitingUser(session: Session): void {
  useToastStore.getState().upsertToast({
    id: `${session.id}:WAITING_USER:${session.updated_at || session.last_message_at || Date.now()}`,
    sessionId: session.id,
    title: toastTitle(session),
    message: "完成本轮回复，点击查看",
    status: "WAITING_USER",
  });
}

function notifyWaitingConfirm(
  session: Session,
  confirm: PendingConfirm,
): void {
  useToastStore.getState().upsertToast({
    id: `${session.id}:confirm:${confirm.call_id}`,
    sessionId: session.id,
    title: toastTitle({ ...session, status: "WAITING_CONFIRM" }),
    message: `请求调用 ${confirm.name} 工具，点击查看`,
    status: "WAITING_CONFIRM",
    confirm: {
      callId: confirm.call_id,
      toolName: confirm.name,
      args: confirm.args,
    },
  });
}

function dismissSessionToasts(sessionId: string): void {
  useToastStore.getState().dismissSessionToasts(sessionId);
}

function isViewingSession(sessionId: string): boolean {
  if (typeof window === "undefined") return false;
  return window.location.pathname === `/console/${sessionId}`;
}

function toolMessageToEventData(content: string): string {
  try {
    const obj = JSON.parse(content);
    if (obj && typeof obj === "object") {
      if ("name" in obj && "result" in obj) {
        return content;
      }
      if (obj.role === "tool") {
        return JSON.stringify({
          call_id: typeof obj.tool_call_id === "string" ? obj.tool_call_id : undefined,
          name: "tool",
          result: obj.content ?? "",
        });
      }
    }
  } catch {
    // Legacy/plain tool output.
  }
  return JSON.stringify({ name: "tool", result: content });
}

function messageToEvent(m: Message): StreamEvent {
  switch (m.role) {
    case "user":
      return { type: "user", data: m.content, message_id: m.id };
    case "tool_call":
      return { type: "tool_call", data: m.content, message_id: m.id };
    case "tool":
      return { type: "tool_result", data: toolMessageToEventData(m.content), message_id: m.id };
    case "system":
      return { type: "status", data: m.content, message_id: m.id };
    case "agent":
      return { type: "assistant_message", data: m.content, message_id: m.id };
    default:
      return { type: "text", data: m.content, message_id: m.id };
  }
}

interface Store {
  sessions: Record<string, SessionEntry>;
  activeSessionId: string | null;

  setActiveSession: (id: string | null) => void;
  hydrateSessions: () => Promise<void>;
  openSession: (sessionId: string) => Promise<void>;
  launchSession: (
    agentId: string,
    sourceDir: string,
    createMode?: SessionCreateMode,
    title?: string | null,
    additionalPrompt?: string | null,
    additionalPromptPath?: string | null,
  ) => Promise<string>;
  sendMessage: (sessionId: string, content: string) => void;
  cancelGeneration: (sessionId: string) => void;
  retryMessage: (sessionId: string, messageId: string, content: string) => Promise<void>;
  resolveConfirm: (sessionId: string, callId: string, approved: boolean, message?: string) => void;
  disconnectSession: (sessionId: string) => void;
  closeSession: (sessionId: string) => Promise<void>;
  renameSession: (sessionId: string, title: string) => Promise<void>;
  refreshSession: (sessionId: string) => Promise<void>;
  fetchTasks: (sessionId: string) => Promise<void>;
}

export const useStore = create<Store>((set, get) => {
  // Open a WS for a session and wire its frames into the store. Shared by
  // launchSession (sessions we start here) and openSession (sessions started
  // elsewhere — e.g. spawned by a subagent — that we're now viewing).
  const attachSocket = (sessionId: string): WebSocket => {
    const socket = createSessionSocket(sessionId);

    socket.onmessage = (e) => {
      const frame = JSON.parse(e.data);
      set((s) => {
        const entry = s.sessions[sessionId];
        if (!entry) return s;
        if (frame.type === "session_state") {
          const nextSession = frame.data as Session;
          const becameWaiting = shouldMarkAttentionUnread(
            entry.session.status,
            nextSession.status,
          );
          const isCurrent = s.activeSessionId === sessionId || isViewingSession(sessionId);
          if (!isCurrent && becameWaiting && nextSession.status === "WAITING_USER") {
            notifyWaitingUser(nextSession);
          }
          return {
            sessions: {
              ...s.sessions,
              [sessionId]: {
                ...entry,
                session: nextSession,
                attentionUnread: isCurrent
                  ? false
                  : entry.attentionUnread || becameWaiting,
              },
            },
          };
        }
        const isTerminal = frame.type === "done" || frame.type === "error";
        const isUser = frame.type === "user";
        const inferredStatus: SessionStatus | null =
          frame.type === "tool_confirm"
            ? "WAITING_CONFIRM"
            : frame.type === "done"
              ? "WAITING_USER"
              : frame.type === "error"
                ? "ERROR"
                : isUser || frame.type === "tool_result"
                  ? "RUNNING"
                  : null;
        const nextSession = inferredStatus
          ? { ...entry.session, status: inferredStatus }
          : entry.session;
        const isCurrent = s.activeSessionId === sessionId || isViewingSession(sessionId);
        const becameWaiting = shouldMarkAttentionUnread(
          entry.session.status,
          nextSession.status,
        );

        // A tool_confirm frame carries {call_id,name,args}: enqueue an
        // interactive approve/deny card without adding an extra session log row.
        let pendingConfirms = entry.pendingConfirms;
        let shouldAppendEvent = true;
        if (frame.type === "tool_confirm") {
          try {
            const obj = JSON.parse(frame.data);
            const confirm = { call_id: obj.call_id, name: obj.name, args: obj.args };
            pendingConfirms = [...pendingConfirms, confirm];
            shouldAppendEvent = false;
            if (!isCurrent && confirm.call_id) {
              notifyWaitingConfirm(nextSession, confirm);
            }
          } catch {
            // malformed — leave the queue as-is
          }
        } else if (frame.type === "tool_result") {
          // The gate cleared (approved-and-ran or denied): drop the oldest
          // pending card for this tool name. We match by name because the
          // result frame carries {name,result} but not call_id.
          try {
            const obj = JSON.parse(frame.data);
            const idx = pendingConfirms.findIndex((p) => p.name === obj.name);
            if (idx !== -1) {
              pendingConfirms = pendingConfirms.filter((_, i) => i !== idx);
            }
          } catch {
            // ignore
          }
        } else if (isTerminal) {
          pendingConfirms = [];
        }

        if (!isCurrent && becameWaiting && nextSession.status === "WAITING_USER") {
          notifyWaitingUser(nextSession);
        }

        return {
          sessions: {
            ...s.sessions,
            [sessionId]: {
              ...entry,
              session: nextSession,
              events: shouldAppendEvent
                ? [...entry.events, frame as StreamEvent]
                : entry.events,
              pendingConfirms,
              attentionUnread: isCurrent
                ? false
                : entry.attentionUnread || becameWaiting,
            },
          },
        };
      });
    };

    socket.onclose = () => {
      // Refresh session status from server on disconnect
      get().refreshSession(sessionId);
    };

    return socket;
  };

  return {
    sessions: {},
    activeSessionId: null,

    setActiveSession: (id) =>
      set((s) => {
        if (!id) return { activeSessionId: id };
        dismissSessionToasts(id);
        const entry = s.sessions[id];
        if (!entry) return { activeSessionId: id };
        return {
          activeSessionId: id,
          sessions: {
            ...s.sessions,
            [id]: { ...entry, attentionUnread: false },
          },
        };
      }),

    // Pull every session the gateway knows about and merge any we don't already
    // track into the store as display-only entries (no live socket). Sessions we
    // launched in this tab keep their socket/events untouched; we only refresh
    // their session metadata. Lets the dashboard show the full derivation tree
    // even across a page reload.
    hydrateSessions: async () => {
      const remote = await api.sessions.list();
      set((s) => {
        const remoteIds = new Set(remote.map((session) => session.id));
        const next = Object.fromEntries(
          Object.entries(s.sessions).filter(([id]) => remoteIds.has(id)),
        ) as Record<string, SessionEntry>;
        for (const session of remote) {
          const existing = next[session.id];
          if (existing) {
            const becameWaiting = shouldMarkAttentionUnread(
              existing.session.status,
              session.status,
            );
            const isCurrent = s.activeSessionId === session.id || isViewingSession(session.id);
            if (!isCurrent && becameWaiting) {
              if (session.status === "WAITING_USER") {
                notifyWaitingUser(session);
              } else if (session.status === "WAITING_CONFIRM") {
                useToastStore.getState().upsertToast({
                  id: `${session.id}:WAITING_CONFIRM:${session.updated_at || Date.now()}`,
                  sessionId: session.id,
                  title: toastTitle(session),
                  message: "请求工具调用确认，点击查看",
                  status: "WAITING_CONFIRM",
                });
              }
            }
            next[session.id] = {
              ...existing,
              session,
              attentionUnread: isCurrent
                ? false
                : existing.attentionUnread || becameWaiting,
            };
          } else {
            next[session.id] = {
              session,
              events: [],
              socket: null,
              tasks: [],
              attentionUnread: false,
              pendingConfirms: [],
            };
          }
        }
        return { sessions: next };
      });
    },

    // Open a session for viewing: backfill its message history from REST, and
    // attach a live WS if one isn't already connected. Used when navigating to
    // a session we didn't launch in this tab (subagent-spawned, another tab,
    // post-reload) — those arrive via hydrateSessions with events:[] socket:null
    // and would otherwise render an empty console.
    openSession: async (sessionId) => {
      const existing = get().sessions[sessionId];
      // Already live in this tab (launched here / mid-stream / opened a moment
      // ago): don't clobber the in-memory buffer or open a second socket. The
      // socket is written synchronously below before any await, so React
      // StrictMode's double-invoke (and rapid re-navigation) hits this guard on
      // the second call instead of racing to create a duplicate stream.
      if (existing?.socket) {
        set((s) => {
          const entry = s.sessions[sessionId];
          return {
            activeSessionId: sessionId,
            sessions: entry
              ? {
                  ...s.sessions,
                  [sessionId]: { ...entry, attentionUnread: false },
                }
              : s.sessions,
          };
        });
        return;
      }

      // Synchronously claim the slot: attach the socket and store it before the
      // first await. zustand's set is synchronous, so a concurrent call now sees
      // socket != null above and bails out.
      const socket = attachSocket(sessionId);
      set((s) => {
        const entry = s.sessions[sessionId];
        return {
          activeSessionId: sessionId,
          sessions: {
            ...s.sessions,
            [sessionId]: entry
              ? { ...entry, socket, attentionUnread: false }
              // Placeholder until we fetch the record below; status is provisional.
              : {
                  session: {
                    id: sessionId,
                    agent_id: "",
                    title: null,
                    working_dir: null,
                    parent_session_id: null,
                    additional_prompt: null,
                    additional_prompt_path: null,
                    status: "INITIALIZING",
                    created_at: "",
                    updated_at: "",
                    last_message_at: null,
                    deleted_at: null,
                  },
                  events: [],
                  socket,
                  tasks: [],
                  attentionUnread: false,
                  pendingConfirms: [],
                },
          },
        };
      });

      // Backfill: fetch the session record (if we lacked it) and history, then
      // merge in without disturbing the socket or any live events the WS may
      // have already appended.
      const needsSession = !existing?.session;
      const [fetchedSession, messages] = await Promise.all([
        needsSession ? api.sessions.get(sessionId) : Promise.resolve(null),
        api.sessions.messages(sessionId),
      ]);
      const history = messages.map(messageToEvent);

      set((s) => {
        const entry = s.sessions[sessionId];
        if (!entry) return s;
        return {
          sessions: {
            ...s.sessions,
            [sessionId]: {
              ...entry,
              session: fetchedSession ?? entry.session,
              // History first, then any events the live socket already delivered.
              events: [...history, ...entry.events],
            },
          },
        };
      });
    },

    launchSession: async (
      agentId,
      sourceDir,
      createMode = "use_existing_directory",
      title = null,
      additionalPrompt = null,
      additionalPromptPath = null,
    ) => {
      const session = await api.sessions.create(
        agentId,
        sourceDir,
        createMode,
        title,
        additionalPrompt,
        additionalPromptPath,
      );
      const socket = attachSocket(session.id);

      set((s) => ({
        sessions: {
          ...s.sessions,
          [session.id]: {
            session,
            events: [],
            socket,
            tasks: [],
            attentionUnread: false,
            pendingConfirms: [],
          },
        },
      }));

      return session.id;
    },

    sendMessage: (sessionId, content) => {
      const entry = get().sessions[sessionId];
      if (!entry?.socket) return;
      entry.socket.send(JSON.stringify({ content }));
      set((s) => {
        const cur = s.sessions[sessionId];
        if (!cur) return s;
        return {
          sessions: {
            ...s.sessions,
            [sessionId]: {
              ...cur,
              session: { ...cur.session, status: "RUNNING" },
            },
          },
        };
      });
    },

    cancelGeneration: (sessionId) => {
      const entry = get().sessions[sessionId];
      if (!entry?.socket) return;
      entry.socket.send(JSON.stringify({ cancel: true }));
      set((s) => {
        const cur = s.sessions[sessionId];
        if (!cur) return s;
        return {
          sessions: {
            ...s.sessions,
            [sessionId]: {
              ...cur,
              session: { ...cur.session, status: "WAITING_USER" },
              pendingConfirms: [],
            },
          },
        };
      });
    },

    renameSession: async (sessionId, title) => {
      const updated = await api.sessions.updateTitle(sessionId, title);
      set((s) => {
        const entry = s.sessions[sessionId];
        if (!entry) return s;
        return {
          sessions: {
            ...s.sessions,
            [sessionId]: { ...entry, session: updated },
          },
        };
      });
    },

    retryMessage: async (sessionId, messageId, content) => {
      const entry = get().sessions[sessionId];
      entry?.socket?.close();
      await api.sessions.retryMessage(sessionId, messageId, content);
      const [session, messages] = await Promise.all([
        api.sessions.get(sessionId),
        api.sessions.messages(sessionId),
      ]);
      const socket = attachSocket(sessionId);
      const history = messages.map(messageToEvent);
      set((s) => {
        const cur = s.sessions[sessionId];
        if (!cur) return s;
        return {
          sessions: {
            ...s.sessions,
            [sessionId]: {
              ...cur,
              session,
              events: history,
              socket,
              generating: true,
              pendingConfirms: [],
            },
          },
        };
      });
    },

    resolveConfirm: (sessionId, callId, approved, message) => {
      const entry = get().sessions[sessionId];
      if (!entry?.socket) return;
      useToastStore.getState().dismissToast(`${sessionId}:confirm:${callId}`);
      entry.socket.send(
        JSON.stringify({
          decision: approved ? "approve" : "deny",
          call_id: callId,
          message: message || undefined,
        }),
      );
      // Optimistically drop the card; the eventual tool_result would clear it
      // too, but removing it now keeps the UI responsive.
      set((s) => {
        const cur = s.sessions[sessionId];
        if (!cur) return s;
        return {
          sessions: {
            ...s.sessions,
            [sessionId]: {
              ...cur,
              pendingConfirms: cur.pendingConfirms.filter(
                (p) => p.call_id !== callId,
              ),
            },
          },
        };
      });
    },

    disconnectSession: (sessionId) => {
      const entry = get().sessions[sessionId];
      if (!entry) return;
      entry.socket?.close();
      dismissSessionToasts(sessionId);
      set((s) => {
        const current = s.sessions[sessionId];
        if (!current) return s;
        return {
          sessions: {
            ...s.sessions,
            [sessionId]: { ...current, socket: null, generating: false },
          },
          activeSessionId:
            s.activeSessionId === sessionId ? null : s.activeSessionId,
        };
      });
    },

    closeSession: async (sessionId) => {
      dismissSessionToasts(sessionId);
      const entry = get().sessions[sessionId];
      entry?.socket?.close();
      await api.sessions.delete(sessionId);
      set((s) => {
        const next = { ...s.sessions };
        delete next[sessionId];
        return {
          sessions: next,
          activeSessionId: s.activeSessionId === sessionId ? null : s.activeSessionId,
        };
      });
    },

    refreshSession: async (sessionId) => {
      const session = await api.sessions.get(sessionId);
      set((s) => {
        const entry = s.sessions[sessionId];
        if (!entry) return s;
        return {
          sessions: { ...s.sessions, [sessionId]: { ...entry, session } },
        };
      });
    },

    // Refetch the session's task list (task_list_id == session id). Called
    // reactively when new stream events arrive, since task_* tool calls — the
    // only thing that mutates tasks — surface as those events. No-op if we're
    // not tracking the session.
    fetchTasks: async (sessionId) => {
      if (!get().sessions[sessionId]) return;
      const tasks = await api.sessions.tasks(sessionId);
      set((s) => {
        const entry = s.sessions[sessionId];
        if (!entry) return s;
        return {
          sessions: { ...s.sessions, [sessionId]: { ...entry, tasks } },
        };
      });
    },
  };
});
