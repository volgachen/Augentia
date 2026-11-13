import { useNavigate } from "react-router-dom";
import { useEffect, useMemo, useState } from "react";
import { useStore } from "../store/sessions";
import { usePluginStore } from "../store/plugins";
import { api } from "../api/client";
import type { AgentTemplate, PluginDefinition, PluginInstance, Session, SessionStatus } from "../api/types";
import PluginSessionConsoleDialog from "../components/plugins/PluginSessionConsoleDialog";

function formatTimestamp(iso: string | null): string {
  // The backend stores timestamps as UTC but the DATETIME column serializes
  // without a timezone suffix, so a bare "2026-06-09T03:00:00" would be parsed
  // as local time. Append "Z" when no offset is present so it's read as UTC.
  if (!iso) return "—";
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso);
  const d = new Date(hasTz ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

const STATUS_STYLE: Record<SessionStatus, string> = {
  INITIALIZING: "bg-yellow-900 text-yellow-300",
  RUNNING: "bg-green-900 text-green-300",
  WAITING_USER: "bg-blue-900 text-blue-300",
  WAITING_CONFIRM: "bg-orange-900 text-orange-300",
  COMPLETED: "bg-gray-700 text-gray-400",
  ERROR: "bg-red-900 text-red-300",
};

interface Entry {
  session: Session;
  socket: WebSocket | null;
}

// A session is "active" when it holds a live WebSocket to the backend stream.
function isActive(entry: Entry): boolean {
  const s = entry.socket;
  return (
    s != null && (s.readyState === WebSocket.OPEN || s.readyState === WebSocket.CONNECTING)
  );
}

interface Row {
  entry: Entry;
  depth: number;
  // Set when this session has a parent that isn't currently loaded in the
  // store; it's rendered as a root but we still hint at the missing parent.
  orphanParent: string | null;
}

interface SessionDialogPlugin {
  definition: PluginDefinition;
  instance: PluginInstance;
}

interface OpenPluginDialog {
  sessionId: string;
  plugin: SessionDialogPlugin;
}

type SortMode = "time" | "dir";
const SORT_MODE_KEY = "augentia.sessions.sortMode";

function loadSortMode(): SortMode {
  try {
    return localStorage.getItem(SORT_MODE_KEY) === "dir" ? "dir" : "time";
  } catch {
    return "time";
  }
}

// Directory groups the user collapsed (by dirKey), remembered across visits.
const COLLAPSED_DIRS_KEY = "augentia.sessions.collapsedDirs";

function loadCollapsedDirs(): Set<string> {
  try {
    const raw = JSON.parse(localStorage.getItem(COLLAPSED_DIRS_KEY) ?? "[]");
    return new Set(Array.isArray(raw) ? raw.filter((k): k is string => typeof k === "string") : []);
  } catch {
    return new Set();
  }
}

// Last activity; sessions with no messages yet fall back to their creation
// time so they don't sink below every session that has ever been talked to.
const lastActivity = (e: Entry) => e.session.last_message_at ?? e.session.created_at;
const byUpdated = (a: Entry, b: Entry) => lastActivity(b).localeCompare(lastActivity(a));
const byActiveThenUpdated = (a: Entry, b: Entry) =>
  Number(isActive(b)) - Number(isActive(a)) || byUpdated(a, b);

// Flatten the sessions into a depth-first list ordered as a parent -> child
// forest, `compare` ordering the roots and each sibling group. Children whose
// parent is not among `entries` (closed, owned by another client, or in
// another directory group) become roots so nothing is ever hidden.
function buildForest(entries: Entry[], compare: (a: Entry, b: Entry) => number): Row[] {
  const byId = new Map(entries.map((e) => [e.session.id, e]));
  const childrenOf = new Map<string, Entry[]>();
  for (const e of entries) {
    const pid = e.session.parent_session_id;
    if (pid && byId.has(pid)) {
      const arr = childrenOf.get(pid) ?? [];
      arr.push(e);
      childrenOf.set(pid, arr);
    }
  }

  const roots = entries
    .filter((e) => {
      const pid = e.session.parent_session_id;
      return !pid || !byId.has(pid);
    })
    .sort(compare);

  const rows: Row[] = [];
  const visited = new Set<string>();
  const walk = (entry: Entry, depth: number) => {
    if (visited.has(entry.session.id)) return; // guard against cycles
    visited.add(entry.session.id);
    const pid = entry.session.parent_session_id;
    rows.push({
      entry,
      depth,
      orphanParent: pid && !byId.has(pid) ? pid : null,
    });
    const kids = (childrenOf.get(entry.session.id) ?? []).slice().sort(compare);
    for (const kid of kids) walk(kid, depth + 1);
  };
  for (const root of roots) walk(root, 0);
  return rows;
}

interface DirGroup {
  key: string;
  // A path as one of its sessions shows it; null for sessions without one.
  dir: string | null;
  rows: Row[];
  count: number;
  activeCount: number;
}

// Windows paths compare case-insensitively and with either slash.
function dirKey(dir: string | null): string {
  if (!dir) return "";
  const path = dir.replace(/\\/g, "/").replace(/\/+$/, "");
  return /^[a-zA-Z]:/.test(path) ? path.toLowerCase() : path;
}

// One group per working directory. Groups holding an active session come
// first, the ones with fewer sessions ahead (then the most recent); the other
// groups follow by most recent activity. Inside a group, active sessions are
// pinned to the top of that group only, the rest follow by time.
function buildDirGroups(entries: Entry[]): DirGroup[] {
  const byKey = new Map<string, Entry[]>();
  for (const e of entries) {
    const key = dirKey(e.session.working_dir);
    const arr = byKey.get(key) ?? [];
    arr.push(e);
    byKey.set(key, arr);
  }
  const groups = [...byKey.entries()].map(([key, members]) => ({
    key,
    dir: members[0].session.working_dir,
    rows: buildForest(members, byActiveThenUpdated),
    count: members.length,
    activeCount: members.filter(isActive).length,
    latest: members.map(lastActivity).reduce((x, y) => (y > x ? y : x)),
  }));
  groups.sort((a, b) => {
    const aActive = a.activeCount > 0;
    const bActive = b.activeCount > 0;
    if (aActive !== bActive) return aActive ? -1 : 1;
    if (aActive && a.count !== b.count) return a.count - b.count;
    return b.latest.localeCompare(a.latest);
  });
  return groups;
}

function UnlinkIcon() {
  // A chain link with a break: "detach from parent".
  return (
    <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor"
      strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9 17H7a5 5 0 0 1 0-10h2" />
      <path d="M15 7h2a5 5 0 0 1 4 8" />
      <path d="M8 12h3" />
      <path d="M3 3l18 18" />
    </svg>
  );
}

export default function SessionDashboard() {
  const sessions = useStore((s) => s.sessions);
  const setActive = useStore((s) => s.setActiveSession);
  const closeSession = useStore((s) => s.closeSession);
  const renameSession = useStore((s) => s.renameSession);
  const unlinkParent = useStore((s) => s.unlinkParent);
  const hydrateSessions = useStore((s) => s.hydrateSessions);
  const pluginCatalog = usePluginStore((s) => s.catalog);
  const pluginInstances = usePluginStore((s) => s.instances);
  const loadPlugins = usePluginStore((s) => s.loadPlugins);
  const navigate = useNavigate();

  // agent_id -> display name. Sessions only carry the agent UUID, so we fetch
  // the registry once to render human-readable names in the Agent column.
  const [agentNames, setAgentNames] = useState<Record<string, string>>({});

  // Which session's title is being edited inline, and the draft text.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [openPluginDialog, setOpenPluginDialog] = useState<OpenPluginDialog | null>(null);
  const [openMoreForSession, setOpenMoreForSession] = useState<string | null>(null);
  const [sortMode, setSortModeState] = useState<SortMode>(loadSortMode);
  const setSortMode = (mode: SortMode) => {
    setSortModeState(mode);
    try {
      localStorage.setItem(SORT_MODE_KEY, mode);
    } catch {
      // Private mode etc.: the choice just isn't remembered.
    }
  };

  const [collapsedDirs, setCollapsedDirsState] = useState<Set<string>>(loadCollapsedDirs);
  const setCollapsedDirs = (next: Set<string>) => {
    setCollapsedDirsState(next);
    try {
      localStorage.setItem(COLLAPSED_DIRS_KEY, JSON.stringify([...next]));
    } catch {
      // Not remembered; still applies for this visit.
    }
  };
  const toggleDir = (key: string) => {
    const next = new Set(collapsedDirs);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setCollapsedDirs(next);
  };

  const handleUnlink = (session: Session) => {
    const name = session.title?.trim() || session.id.slice(0, 8);
    if (!window.confirm(`把「${name}」从父会话解绑？解绑后它成为独立的会话，父会话的子会话列表里不再显示它。`)) {
      return;
    }
    unlinkParent(session.id).catch((err) => {
      console.error("failed to unlink session", err);
      window.alert(`解绑失败：${err instanceof Error ? err.message : String(err)}`);
    });
  };

  useEffect(() => {
    hydrateSessions().catch((err) =>
      console.error("failed to hydrate sessions", err),
    );
    loadPlugins().catch((err) =>
      console.error("failed to load plugins", err),
    );
    api.agents
      .list()
      .then((agents: AgentTemplate[]) =>
        setAgentNames(Object.fromEntries(agents.map((a) => [a.id, a.name]))),
      )
      .catch((err) => console.error("failed to load agents", err));
  }, [hydrateSessions, loadPlugins]);

  const commitRename = (sessionId: string) => {
    const title = draftTitle.trim();
    setEditingId(null);
    const current = sessions[sessionId]?.session.title ?? "";
    if (!title || title === current) return;
    renameSession(sessionId, title).catch((err) =>
      console.error("failed to rename session", err),
    );
  };

  const sessionDialogPlugins = useMemo<SessionDialogPlugin[]>(() => {
    const definitionsById = new Map(pluginCatalog.map((definition) => [definition.id, definition]));
    return Object.values(pluginInstances)
      .filter((instance) => instance.status === "running")
      .map((instance) => {
        const definition = definitionsById.get(instance.plugin_id);
        return definition?.has_session_dialog ? { definition, instance } : null;
      })
      .filter((item): item is SessionDialogPlugin => item !== null);
  }, [pluginCatalog, pluginInstances]);

  const entries: Entry[] = Object.values(sessions).map((e) => ({
    session: e.session,
    socket: e.socket,
  }));

  if (entries.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-3 text-gray-400">
        <p>No active sessions.</p>
        <button
          onClick={() => navigate("/")}
          className="text-indigo-400 hover:text-indigo-300 text-sm underline"
        >
          Launch one from the Agent Registry
        </button>
      </div>
    );
  }

  // Table body: plain rows by time, or rows under one header per directory.
  // A collapsed group keeps its header row and hides its sessions.
  type Item = { kind: "group"; group: DirGroup } | { kind: "row"; row: Row };
  const dirGroups = sortMode === "dir" ? buildDirGroups(entries) : [];
  const items: Item[] =
    sortMode === "dir"
      ? dirGroups.flatMap((group): Item[] => [
          { kind: "group", group },
          ...(collapsedDirs.has(group.key)
            ? []
            : group.rows.map((row): Item => ({ kind: "row", row }))),
        ])
      : buildForest(entries, byUpdated).map((row): Item => ({ kind: "row", row }));
  const allCollapsed = dirGroups.length > 0 && dirGroups.every((g) => collapsedDirs.has(g.key));

  const openSession = (sessionId: string) => {
    setActive(sessionId);
    navigate(`/console/${sessionId}`);
  };

  return (
    <div className="p-6 flex flex-col min-h-0 flex-1">
      <div className="mb-6 flex shrink-0 items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold text-white">Sessions</h1>
        <div className="flex items-center gap-2">
          {sortMode === "dir" && dirGroups.length > 0 && (
            <button
              type="button"
              onClick={() =>
                setCollapsedDirs(allCollapsed ? new Set() : new Set(dirGroups.map((g) => g.key)))
              }
              className="rounded-md px-2.5 py-1 text-xs text-gray-400 hover:bg-gray-800 hover:text-gray-200 transition-colors"
            >
              {allCollapsed ? "全部展开" : "全部折叠"}
            </button>
          )}
          <div className="flex items-center gap-1 rounded-lg border border-gray-800 bg-gray-900/60 p-0.5 text-xs">
            <span className="px-2 text-gray-500">排序</span>
            {(
              [
                ["time", "按时间", "按最后活动时间，最新在前（子会话跟在父会话下）"],
                ["dir", "按工作目录", "按工作目录归类：有 active 会话的目录在前，其中会话少的靠前；active 会话只在自己的目录里置顶"],
              ] as const
            ).map(([mode, label, hint]) => (
              <button
                key={mode}
                type="button"
                title={hint}
                onClick={() => setSortMode(mode)}
                className={`rounded-md px-2.5 py-1 transition-colors ${
                  sortMode === mode
                    ? "bg-indigo-700 text-white"
                    : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>
      {openPluginDialog && (
        <PluginSessionConsoleDialog
          sessionId={openPluginDialog.sessionId}
          plugin={openPluginDialog.plugin.definition}
          instance={openPluginDialog.plugin.instance}
          onClose={() => setOpenPluginDialog(null)}
        />
      )}
      <div className="flex-1 min-h-0 overflow-auto">
        <table className="w-full min-w-[56rem] text-sm text-left">
          <thead className="sticky top-0 z-10 bg-gray-950">
            <tr className="text-gray-400 border-b border-gray-700">
              <th className="pb-3 pr-4 font-medium">Active</th>
              <th className="pb-3 pr-4 font-medium">Title</th>
              <th className="pb-3 pr-4 font-medium">Agent</th>
              <th className="pb-3 pr-4 font-medium">Working Dir</th>
              <th className="pb-3 pr-4 font-medium">Status</th>
              <th className="pb-3 pr-4 font-medium">Last Update</th>
              <th className="pb-3 font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              if (item.kind === "group") {
                const { group } = item;
                const collapsed = collapsedDirs.has(group.key);
                return (
                  <tr
                    key={`dir:${group.key}`}
                    onClick={() => toggleDir(group.key)}
                    title={collapsed ? "展开这个目录" : "折叠这个目录"}
                    className="border-b border-gray-800 bg-gray-900/70 cursor-pointer select-none hover:bg-gray-800/70"
                  >
                    <td colSpan={7} className="px-2 py-1.5">
                      <span className="flex items-center gap-2 text-xs">
                        <span
                          aria-hidden="true"
                          className={`w-3 shrink-0 text-gray-500 transition-transform ${collapsed ? "" : "rotate-90"}`}
                        >
                          ▶
                        </span>
                        <span
                          className={`h-2 w-2 shrink-0 rounded-full ${
                            group.activeCount > 0 ? "bg-green-400" : "bg-gray-600"
                          }`}
                        />
                        <span className="truncate font-mono text-gray-300" title={group.dir ?? undefined}>
                          {group.dir ?? "（无工作目录）"}
                        </span>
                        <span className="shrink-0 text-gray-500">
                          {group.count} 个会话
                          {group.activeCount > 0 && `，${group.activeCount} 个 active`}
                        </span>
                      </span>
                    </td>
                  </tr>
                );
              }
              const { entry, depth, orphanParent } = item.row;
              const { session } = entry;
              const active = isActive(entry);
              return (
              <tr
                key={session.id}
                onDoubleClick={(e) => {
                  if (editingId === session.id) return;
                  if ((e.target as HTMLElement).closest("button, input")) return;
                  openSession(session.id);
                }}
                className="border-b border-gray-800 hover:bg-gray-800/50 transition-colors cursor-pointer"
              >
                <td className="py-3 pr-4">
                  <span className="inline-flex items-center gap-1.5">
                    <span
                      className={`w-2 h-2 rounded-full ${active ? "bg-green-400" : "bg-gray-600"}`}
                    />
                    <span className={`text-xs ${active ? "text-green-300" : "text-gray-500"}`}>
                      {active ? "Active" : "Inactive"}
                    </span>
                  </span>
                </td>
                <td className="py-3 pr-4 text-gray-200 max-w-xs">
                  <span
                    className="inline-flex items-center gap-1 max-w-full"
                    style={{ paddingLeft: `${depth * 1.25}rem` }}
                  >
                    {session.parent_session_id && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleUnlink(session);
                        }}
                        title={`从父会话 ${session.parent_session_id} 解绑`}
                        className="shrink-0 rounded p-0.5 text-gray-600 hover:bg-gray-800 hover:text-amber-300 transition-colors"
                      >
                        <UnlinkIcon />
                      </button>
                    )}
                    {depth > 0 && (
                      <span className="text-gray-600 shrink-0">└─</span>
                    )}
                    {editingId === session.id ? (
                      <input
                        autoFocus
                        value={draftTitle}
                        onChange={(e) => setDraftTitle(e.target.value)}
                        onBlur={() => commitRename(session.id)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") commitRename(session.id);
                          if (e.key === "Escape") setEditingId(null);
                        }}
                        className="w-full bg-gray-800 border border-indigo-600 rounded px-2 py-0.5 text-sm text-white focus:outline-none"
                      />
                    ) : (
                      <button
                        // Hover reveals the full session id (the ID column was
                        // removed); the tree indent/connector live here now.
                        title={`${session.id}\n\nClick to rename`}
                        onClick={() => {
                          setEditingId(session.id);
                          setDraftTitle(session.title ?? "");
                        }}
                        className="text-left truncate hover:text-indigo-300 transition-colors"
                      >
                        {session.title ?? (
                          <span className="text-gray-600">untitled</span>
                        )}
                      </button>
                    )}
                    {orphanParent && (
                      <span className="ml-1 text-[10px] text-gray-600 shrink-0">
                        ↳ from {orphanParent.slice(0, 8)}…
                      </span>
                    )}
                  </span>
                </td>
                <td className="py-3 pr-4 text-gray-300">
                  {agentNames[session.agent_id] ?? (
                    <span className="font-mono text-xs text-gray-500">
                      {session.agent_id}
                    </span>
                  )}
                </td>
                <td className="py-3 pr-4 font-mono text-gray-400 text-xs">
                  {session.working_dir ? (
                    // Long absolute paths were the main thing forcing the table
                    // wider than the viewport; clamp + tooltip instead.
                    <div
                      className="max-w-[18rem] truncate"
                      title={session.working_dir}
                      dir="rtl"
                    >
                      {session.working_dir}
                    </div>
                  ) : (
                    <span className="text-gray-600">—</span>
                  )}
                </td>
                <td className="py-3 pr-4">
                  <span
                    className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_STYLE[session.status]}`}
                  >
                    {session.status}
                  </span>
                </td>
                <td className="py-3 pr-4 text-gray-400 text-xs whitespace-nowrap">
                  {formatTimestamp(session.last_message_at)}
                </td>
                <td className="py-3 flex gap-2 relative">
                  <button
                    onClick={() => openSession(session.id)}
                    className="px-3 py-1 rounded bg-indigo-700 hover:bg-indigo-600 text-white text-xs transition-colors"
                  >
                    Open
                  </button>
                  <button
                    onClick={() => closeSession(session.id)}
                    className="px-3 py-1 rounded bg-gray-700 hover:bg-red-800 text-gray-300 hover:text-white text-xs transition-colors"
                  >
                    Close
                  </button>
                  {sessionDialogPlugins.length > 0 && (
                    <div className="relative">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setOpenMoreForSession((current) =>
                            current === session.id ? null : session.id,
                          );
                        }}
                        className="px-3 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300 hover:text-white text-xs transition-colors"
                      >
                        More
                      </button>
                      {openMoreForSession === session.id && (
                        <div className="absolute right-0 top-8 z-20 min-w-40 overflow-hidden rounded-lg border border-gray-700 bg-gray-900 shadow-xl">
                          {sessionDialogPlugins.map((plugin) => (
                            <button
                              key={plugin.instance.id}
                              onClick={(e) => {
                                e.stopPropagation();
                                setOpenMoreForSession(null);
                                setOpenPluginDialog({ sessionId: session.id, plugin });
                              }}
                              className="block w-full px-3 py-2 text-left text-xs text-gray-200 hover:bg-gray-800"
                            >
                              {plugin.definition.name}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
