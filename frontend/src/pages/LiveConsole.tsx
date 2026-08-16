import { useEffect, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useStore } from "../store/sessions";
import type { StreamEvent } from "../api/types";
import TaskListPanel from "../components/TaskListPanel";
import SubAgentListPanel from "../components/SubAgentListPanel";
import ToolConfirmPanel from "../components/ToolConfirmPanel";
import ToolPermissionsPanel from "../components/ToolPermissionsPanel";
import SystemPromptPanel from "../components/SystemPromptPanel";
import ToolPreview from "../components/ToolPreview";
import { getToolSummary } from "../config/toolDisplay";

const EVENT_STYLE: Record<string, string> = {
  text: "text-gray-200",
  tool_call: "text-yellow-400",
  tool_confirm: "text-orange-400",
  tool_result: "text-amber-300",
  status: "text-blue-400",
  error: "text-red-400",
  done: "text-green-500",
  session_state: "text-gray-500",
  assistant_message: "text-gray-200",
  user: "text-indigo-300",
};

interface AssistantToolCall {
  id?: string;
  type?: string;
  function?: {
    name?: string;
    arguments?: string;
  };
}

interface AssistantPayload {
  role?: string;
  content?: string | null;
  tool_calls?: AssistantToolCall[] | null;
}

interface ToolCallView {
  kind: "tool_call";
  callId: string;
  name: string;
  args: unknown;
  result?: unknown;
}

type ConsoleItem = StreamEvent | ToolCallView;

function parseAssistantData(raw: string): AssistantPayload {
  try {
    const obj = JSON.parse(raw);
    if (obj && typeof obj === "object" && obj.role === "assistant") {
      return obj as AssistantPayload;
    }
  } catch {
    // Legacy rows stored plain assistant text.
  }
  return { role: "assistant", content: raw, tool_calls: null };
}

function parseToolArguments(raw: string | undefined): unknown {
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

function parseJsonObject(raw: string): Record<string, unknown> | null {
  try {
    const obj = JSON.parse(raw);
    return obj && typeof obj === "object" ? (obj as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function resultToText(result: unknown): string {
  if (typeof result === "string") return result;
  if (result === undefined) return "";
  try {
    return JSON.stringify(result, null, 2);
  } catch {
    return String(result);
  }
}

function toOneLine(s: string, max = 200): string {
  const flat = s.replace(/\s+/g, " ").trim();
  return flat.length > max ? flat.slice(0, max) + "…" : flat;
}

function buildConsoleItems(events: StreamEvent[]): ConsoleItem[] {
  const items: ConsoleItem[] = [];
  const toolCallsById = new Map<string, ToolCallView>();
  const attachResult = (callId: string | undefined, name: string | undefined, result: unknown): boolean => {
    if (callId && toolCallsById.has(callId)) {
      toolCallsById.get(callId)!.result = result;
      return true;
    }

    const reversedCalls = [...items]
      .filter((item): item is ToolCallView => "kind" in item && item.kind === "tool_call")
      .reverse();
    const fallback = reversedCalls.find(
      (item) => item.result === undefined && (!name || item.name === name),
    );
    if (fallback) {
      fallback.result = result;
      return true;
    }

    return false;
  };

  for (const event of events) {
    if (event.type === "assistant_message") {
      const payload = parseAssistantData(event.data);
      const content = payload.content ?? "";
      if (content) {
        items.push({ type: "assistant_message", data: content });
      }
      for (const [index, toolCall] of (payload.tool_calls ?? []).entries()) {
        const name = toolCall.function?.name ?? "unknown";
        const callId = toolCall.id ?? `${items.length}:tool:${index}`;
        const item: ToolCallView = {
          kind: "tool_call",
          callId,
          name,
          args: parseToolArguments(toolCall.function?.arguments),
        };
        items.push(item);
        toolCallsById.set(callId, item);
      }
      continue;
    }

    if (event.type === "tool_call" || event.type === "tool_confirm") {
      const obj = parseJsonObject(event.data);
      const name = typeof obj?.name === "string" ? obj.name : "tool";
      const callId =
        typeof obj?.call_id === "string"
          ? obj.call_id
          : typeof obj?.id === "string"
            ? obj.id
            : `${items.length}:tool`;
      const existing = toolCallsById.get(callId);
      if (existing) {
        existing.name = name;
        existing.args = obj?.args ?? existing.args;
      } else {
        const item: ToolCallView = {
          kind: "tool_call",
          callId,
          name,
          args: obj?.args ?? {},
        };
        items.push(item);
        toolCallsById.set(callId, item);
      }
      continue;
    }

    if (event.type === "tool_result") {
      const obj = parseJsonObject(event.data);
      const attached = attachResult(
        typeof obj?.call_id === "string" ? obj.call_id : undefined,
        typeof obj?.name === "string" ? obj.name : undefined,
        obj?.result ?? obj?.content ?? event.data,
      );
      if (!attached) {
        items.push(event);
      }
      continue;
    }

    items.push(event);
  }

  return items;
}

function ToolCallLine({
  item,
  showToolCalls,
  showToolResults,
}: {
  item: ToolCallView;
  showToolCalls: boolean;
  showToolResults: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const summary = getToolSummary(item.name, item.args);
  if (!showToolCalls) return null;

  return (
    <div className="text-left font-mono text-sm text-yellow-400">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full text-left flex items-start gap-1 hover:bg-gray-800/40 rounded px-1 -mx-1 cursor-pointer"
        title={`${expanded ? "Collapse" : "Expand"} · call_id: ${item.callId}`}
      >
        <span className="text-gray-500 select-none">{expanded ? "▾" : "▸"}</span>
        <span className="shrink-0">⚙ {item.name}</span>
        {summary && <span className="text-gray-400 truncate">{summary}</span>}
      </button>
      {expanded && (
        <div className="ml-4">
          <ToolPreview name={item.name} args={item.args} className="mt-1" />
        </div>
      )}
      {showToolResults && item.result !== undefined && (
        <pre className="mt-1 ml-4 px-2 py-1.5 bg-gray-950/60 border border-gray-800 rounded whitespace-pre-wrap break-all text-xs text-amber-200 overflow-auto max-h-72">
          {resultToText(item.result)}
        </pre>
      )}
    </div>
  );
}

function EventLine({
  event,
  editing = false,
  editValue = "",
  generating = false,
  onStartEdit = () => {},
  onEditChange = () => {},
  onCancelEdit = () => {},
  onSubmitEdit = () => {},
}: {
  event: StreamEvent;
  editing?: boolean;
  editValue?: string;
  generating?: boolean;
  onStartEdit?: () => void;
  onEditChange?: (value: string) => void;
  onCancelEdit?: () => void;
  onSubmitEdit?: () => void;
}) {
  const style = EVENT_STYLE[event.type] ?? "text-gray-300";
  const prefix =
    event.type === "status"
      ? "● "
      : event.type === "error"
        ? "✗ "
        : event.type === "done"
          ? "✓ "
          : event.type === "user"
            ? "❯ "
            : "";
  const body =
    typeof event.data === "string" ? event.data : JSON.stringify(event.data);
  if (event.type === "user" && editing) {
    return (
      <div className="flex flex-col gap-2 rounded-lg border border-indigo-700 bg-indigo-950/30 p-2">
        <textarea
          className="w-full bg-gray-950 border border-indigo-600 rounded px-2 py-1.5 text-sm text-indigo-100 font-mono resize-y focus:outline-none focus:border-indigo-400"
          rows={Math.max(2, editValue.split("\n").length)}
          value={editValue}
          onChange={(e) => onEditChange(e.target.value)}
          disabled={generating}
          autoFocus
        />
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancelEdit}
            className="px-3 py-1 rounded bg-gray-800 hover:bg-gray-700 text-xs text-gray-200"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onSubmitEdit}
            disabled={!editValue.trim() || generating || !event.message_id}
            className="px-3 py-1 rounded bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-xs text-white"
          >
            Send from here
          </button>
        </div>
      </div>
    );
  }
  return (
    <div
      className={`text-left font-mono text-sm whitespace-pre-wrap break-all ${style} ${
        event.type === "user" && event.message_id ? "cursor-text hover:bg-indigo-950/30 rounded px-1 -mx-1" : ""
      }`}
      onDoubleClick={event.type === "user" && event.message_id ? onStartEdit : undefined}
      title={event.type === "user" && event.message_id ? "Double-click to edit and resend from here" : undefined}
    >
      {prefix}
      {event.type === "tool_result" ? toOneLine(body) : body}
    </div>
  );
}

function isToolCallView(item: ConsoleItem): item is ToolCallView {
  return "kind" in item && item.kind === "tool_call";
}

function ConsoleItemLine({
  item,
  editingMessageId,
  editingContent,
  generating,
  showToolCalls,
  showToolResults,
  onStartEdit,
  onEditChange,
  onCancelEdit,
  onSubmitEdit,
}: {
  item: ConsoleItem;
  editingMessageId: string | null;
  editingContent: string;
  generating: boolean;
  showToolCalls: boolean;
  showToolResults: boolean;
  onStartEdit: (event: StreamEvent) => void;
  onEditChange: (value: string) => void;
  onCancelEdit: () => void;
  onSubmitEdit: () => void;
}) {
  if (isToolCallView(item)) {
    return <ToolCallLine item={item} showToolCalls={showToolCalls} showToolResults={showToolResults} />;
  }
  if (item.type === "tool_result" && (!showToolCalls || !showToolResults)) {
    return null;
  }
  return (
    <EventLine
      event={item}
      editing={!!item.message_id && editingMessageId === item.message_id}
      editValue={editingContent}
      generating={generating}
      onStartEdit={() => onStartEdit(item)}
      onEditChange={onEditChange}
      onCancelEdit={onCancelEdit}
      onSubmitEdit={onSubmitEdit}
    />
  );
}

export default function LiveConsole() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const sessions = useStore((s) => s.sessions);
  const sendMessage = useStore((s) => s.sendMessage);
  const retryMessage = useStore((s) => s.retryMessage);
  const resolveConfirm = useStore((s) => s.resolveConfirm);
  const openSession = useStore((s) => s.openSession);
  const fetchTasks = useStore((s) => s.fetchTasks);
  const hydrateSessions = useStore((s) => s.hydrateSessions);
  const [input, setInput] = useState("");
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [editingContent, setEditingContent] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<Array<HTMLDivElement | null>>([]);
  const [focusedItemIndex, setFocusedItemIndex] = useState<number | null>(null);
  const [sidebarTab, setSidebarTab] = useState<"overview" | "permissions" | "systemPrompt">("overview");
  const [showToolCalls, setShowToolCalls] = useState(true);
  const [showToolResults, setShowToolResults] = useState(true);

  const entry = sessionId ? sessions[sessionId] : undefined;

  // Child sessions (sub-agents) are just sessions whose parent is this one.
  // They land in the store via hydrateSessions, so we filter the live map.
  const children = Object.values(sessions)
    .map((e) => e.session)
    .filter((s) => s.parent_session_id === sessionId);

  const eventCount = entry?.events.length ?? 0;

  // Reactive refresh: tasks only change via task_* tool calls, which surface as
  // stream events — so refetch tasks (and re-hydrate to catch newly spawned
  // sub-agents) whenever the event count moves.
  useEffect(() => {
    if (!sessionId) return;
    fetchTasks(sessionId).catch(() => {});
    hydrateSessions().catch(() => {});
  }, [sessionId, eventCount, fetchTasks, hydrateSessions]);

  // Backup poll: covers changes that emit no event on this socket — e.g. a
  // sub-agent spawned from another tab, or its status flipping as it works.
  useEffect(() => {
    if (!sessionId) return;
    const timer = setInterval(() => {
      fetchTasks(sessionId).catch(() => {});
      hydrateSessions().catch(() => {});
    }, 5000);
    return () => clearInterval(timer);
  }, [sessionId, fetchTasks, hydrateSessions]);

  // Backfill history + attach a live socket when viewing a session we didn't
  // launch in this tab (e.g. subagent-spawned). No-op if already live.
  useEffect(() => {
    if (sessionId) openSession(sessionId);
  }, [sessionId, openSession]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entry?.events.length, entry?.session.status]);

  useEffect(() => {
    itemRefs.current = itemRefs.current.slice(0, entry?.events.length ?? 0);
  }, [entry?.events.length]);

  if (!entry) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-3 text-gray-400">
        <p>Session not found.</p>
        <button
          onClick={() => navigate("/")}
          className="text-indigo-400 hover:text-indigo-300 text-sm underline"
        >
          Back to registry
        </button>
      </div>
    );
  }

  const { session, events } = entry;
  const generating = session.status === "RUNNING";
  const consoleItems = buildConsoleItems(events);

  const scrollToConsoleItem = (index: number) => {
    const nextIndex = Math.max(0, Math.min(index, consoleItems.length - 1));
    const node = itemRefs.current[nextIndex];
    if (!node) return;
    setFocusedItemIndex(nextIndex);
    node.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const handleConsoleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    if (consoleItems.length === 0) return;

    e.preventDefault();
    const currentIndex = focusedItemIndex ?? consoleItems.length - 1;
    scrollToConsoleItem(e.key === "ArrowUp" ? currentIndex - 1 : currentIndex + 1);
  };

  const handleShowToolCallsChange = (show: boolean) => {
    setShowToolCalls(show);
    if (!show) setShowToolResults(false);
  };

  const handleSend = () => {
    const msg = input.trim();
    if (!msg || !sessionId || generating) return;
    sendMessage(sessionId, msg);
    setInput("");
  };

  const handleRetrySend = async () => {
    const msg = editingContent.trim();
    if (!msg || !sessionId || !editingMessageId || generating) return;
    await retryMessage(sessionId, editingMessageId, msg);
    setEditingMessageId(null);
    setEditingContent("");
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="relative flex flex-col h-full p-4 gap-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-white">
          {session.title ?? "Live Console"}
        </h1>
        <div className="text-left">
          <p className="text-xs text-gray-500 font-mono">{session.id}</p>
          <p className="text-xs text-gray-500 font-mono">
            <span className="text-gray-600">cwd: </span>
            {session.working_dir ?? "—"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {generating ? (
            <span className="flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium bg-indigo-900/60 text-indigo-200">
              <span className="inline-block w-1.5 h-1.5 rounded-full bg-indigo-300 animate-pulse" />
              generating…
            </span>
          ) : (
            <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-gray-800 text-gray-400">
              idle
            </span>
          )}
          <span
            className={`px-2 py-0.5 rounded-full text-xs font-medium ${
              session.status === "RUNNING"
                ? "bg-green-900 text-green-300"
                : session.status === "ERROR"
                  ? "bg-red-900 text-red-300"
                  : session.status === "WAITING_CONFIRM"
                    ? "bg-orange-900 text-orange-300"
                    : session.status === "WAITING_USER"
                      ? "bg-blue-900 text-blue-300"
                      : "bg-gray-700 text-gray-400"
            }`}
          >
            {session.status}
          </span>
        </div>
      </div>

      {/* Body: event log + status sidebar */}
      <div className="flex-1 flex gap-3 min-h-0">
        {/* Event log */}
        <div
          tabIndex={0}
          onKeyDown={handleConsoleKeyDown}
          className="flex-1 bg-gray-900 rounded-xl border border-gray-700 p-4 overflow-y-auto flex flex-col gap-1 min-h-0 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          aria-label="Event log. Use arrow up and arrow down to jump between messages."
        >
          {consoleItems.length > 1 && (
            <div className="sticky top-0 z-10 self-end rounded bg-gray-950/80 px-2 py-1 text-[11px] text-gray-500 backdrop-blur">
              聚焦日志后，按 ↑ / ↓ 跳到上一条 / 下一条
            </div>
          )}
          {consoleItems.length === 0 && (
            <p className="text-gray-600 text-sm font-mono">Waiting for output…</p>
          )}
          {consoleItems.map((item, i) => (
            <div
              key={isToolCallView(item) ? item.callId : item.message_id ?? i}
              ref={(node) => {
                itemRefs.current[i] = node;
              }}
              className={
                focusedItemIndex === i
                  ? "rounded-md bg-indigo-950/20 ring-1 ring-indigo-700/60"
                  : ""
              }
              onClick={() => setFocusedItemIndex(i)}
            >
              <ConsoleItemLine
                item={item}
                editingMessageId={editingMessageId}
                editingContent={editingContent}
                generating={generating}
                showToolCalls={showToolCalls}
                showToolResults={showToolResults}
                onStartEdit={(event) => {
                  if (generating || event.type !== "user" || !event.message_id) return;
                  setEditingMessageId(event.message_id);
                  setEditingContent(String(event.data ?? ""));
                }}
                onEditChange={setEditingContent}
                onCancelEdit={() => {
                  setEditingMessageId(null);
                  setEditingContent("");
                }}
                onSubmitEdit={handleRetrySend}
              />
            </div>
          ))}
          {generating && (
            <div className="flex items-center gap-2 font-mono text-sm text-indigo-300">
              <span className="inline-block w-3.5 h-3.5 rounded-full border-2 border-indigo-400/40 border-t-indigo-300 animate-spin" />
              <span className="text-gray-500">generating…</span>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {/* Status sidebar (hidden on narrow screens) */}
        <aside className="hidden lg:flex w-80 shrink-0 flex-col min-h-0 bg-gray-900 rounded-xl border border-gray-700">
          <div className="shrink-0 flex gap-1 border-b border-gray-800 p-2">
            {[
              ["overview", "Overview"],
              ["permissions", "Tools"],
              ["systemPrompt", "System Prompt"],
            ].map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => setSidebarTab(key as typeof sidebarTab)}
                className={`rounded px-2 py-1 text-[11px] font-medium ${
                  sidebarTab === key
                    ? "bg-indigo-600 text-white"
                    : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="flex-1 min-h-0 p-3 flex flex-col">
            {sidebarTab === "overview" && (
              <div className="flex-1 min-h-0 flex flex-col gap-3">
                <div className="flex-1 min-h-0 flex flex-col">
                  <TaskListPanel tasks={entry.tasks} />
                </div>
                <div className="flex-1 min-h-0 flex flex-col border-t border-gray-800 pt-3">
                  <SubAgentListPanel
                    agents={children}
                    onOpen={(id) => navigate(`/console/${id}`)}
                  />
                </div>
              </div>
            )}
            {sidebarTab === "permissions" && (
              <ToolPermissionsPanel
                session={session}
                showToolCalls={showToolCalls}
                showToolResults={showToolResults}
                onShowToolCallsChange={handleShowToolCallsChange}
                onShowToolResultsChange={setShowToolResults}
              />
            )}
            {sidebarTab === "systemPrompt" && <SystemPromptPanel session={session} />}
          </div>
        </aside>
      </div>

      {/* Pending tool confirmations */}
      {entry.pendingConfirms.length > 0 && (
        <div className="pointer-events-none absolute inset-x-4 bottom-24 z-30 flex flex-col items-stretch gap-2 lg:right-[21.75rem]">
          {entry.pendingConfirms.map((pc) => (
            <ToolConfirmPanel
              key={pc.call_id}
              name={pc.name}
              args={pc.args}
              callId={pc.call_id}
              onApprove={(message) =>
                sessionId && resolveConfirm(sessionId, pc.call_id, true, message)
              }
              onDeny={(message) =>
                sessionId && resolveConfirm(sessionId, pc.call_id, false, message)
              }
            />
          ))}
        </div>
      )}

      {/* Input */}
      <div className="flex gap-2">
        <textarea
          className="flex-1 bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-500 resize-none focus:outline-none focus:border-indigo-500 disabled:opacity-50"
          rows={2}
          placeholder={
            generating
              ? "Agent is generating… wait for it to finish"
              : "Send a message… (Enter to send, Shift+Enter for newline)"
          }
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={generating}
        />
        <button
          onClick={handleSend}
          disabled={!input.trim() || generating}
          className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white text-sm font-medium transition-colors self-end"
        >
          {generating ? "…" : "Send"}
        </button>
      </div>
    </div>
  );
}
