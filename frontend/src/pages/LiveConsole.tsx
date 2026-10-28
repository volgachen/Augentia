import { useEffect, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useStore } from "../store/sessions";
import { api } from "../api/client";
import { agentAcceptsImages } from "../api/types";
import type { AgentTemplate, ImageRef, StreamEvent } from "../api/types";
import TaskListPanel from "../components/TaskListPanel";
import SubAgentListPanel from "../components/SubAgentListPanel";
import SessionListPanel from "../components/SessionListPanel";
import ToolConfirmPanel from "../components/ToolConfirmPanel";
import ToolPermissionsPanel from "../components/ToolPermissionsPanel";
import SystemPromptPanel from "../components/SystemPromptPanel";
import ToolPreview from "../components/ToolPreview";
import { AttachmentTray, ImageThumbs } from "../components/ImageAttachments";
import { ACCEPTED_IMAGE_TYPES, useImageAttachments } from "../hooks/useImageAttachments";
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
  // Images the tool returned (e.g. read on an image file).
  resultImages?: ImageRef[];
}

// A compression summary together with the rows it replaced. The model sees
// only the summary, so the console folds `hidden` by default.
interface CompressionGroup {
  kind: "compression_group";
  event: StreamEvent;
  hidden: ConsoleItem[];
}

type ConsoleItem = StreamEvent | ToolCallView | CompressionGroup;

type FoldNode = { event: StreamEvent; hidden?: FoldNode[] };

function nodeContains(node: FoldNode, messageId: string): boolean {
  if (node.event.message_id === messageId) return true;
  return !!node.hidden?.some((child) => nodeContains(child, messageId));
}

// Nest every row a compression replaced under that compression's event. A
// whole-session compression replaces everything through its boundary (earlier
// compression groups included, so they nest); a turn compression replaces
// exactly the listed rows. Events without compression info pass through.
function foldCompressed(events: StreamEvent[]): FoldNode[] {
  const out: FoldNode[] = [];
  for (const event of events) {
    const info = event.type === "compression" ? event.compression : undefined;
    if (!info) {
      out.push(event.type === "compression" ? { event, hidden: [] } : { event });
      continue;
    }
    let owned: Set<number>;
    if (info.kind === "session") {
      const through = info.through;
      const end = through ? out.findIndex((node) => nodeContains(node, through)) : -1;
      owned = new Set(Array.from({ length: end + 1 }, (_, i) => i));
    } else {
      const replaced = new Set(info.replaced ?? []);
      owned = new Set(
        out.flatMap((node, i) =>
          node.event.message_id && replaced.has(node.event.message_id) ? [i] : [],
        ),
      );
    }
    const hidden = out.filter((_, i) => owned.has(i));
    const kept = out.filter((_, i) => !owned.has(i));
    out.length = 0;
    out.push(...kept, { event, hidden });
  }
  return out;
}

function buildConsoleItems(events: StreamEvent[]): ConsoleItem[] {
  const toItems = (nodes: FoldNode[]): ConsoleItem[] => {
    const items: ConsoleItem[] = [];
    let run: StreamEvent[] = [];
    const flush = () => {
      items.push(...buildFlatItems(run));
      run = [];
    };
    for (const node of nodes) {
      if (node.hidden) {
        flush();
        items.push({ kind: "compression_group", event: node.event, hidden: toItems(node.hidden) });
      } else {
        run.push(node.event);
      }
    }
    flush();
    return items;
  };
  return toItems(foldCompressed(events));
}

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

function buildFlatItems(events: StreamEvent[]): ConsoleItem[] {
  const items: ConsoleItem[] = [];
  const toolCallsById = new Map<string, ToolCallView>();
  const attachResult = (
    callId: string | undefined,
    name: string | undefined,
    result: unknown,
    images: ImageRef[] | undefined,
  ): boolean => {
    const set = (item: ToolCallView) => {
      item.result = result;
      if (images?.length) item.resultImages = images;
    };
    if (callId && toolCallsById.has(callId)) {
      set(toolCallsById.get(callId)!);
      return true;
    }

    const reversedCalls = [...items]
      .filter((item): item is ToolCallView => "kind" in item && item.kind === "tool_call")
      .reverse();
    const fallback = reversedCalls.find(
      (item) => item.result === undefined && (!name || item.name === name),
    );
    if (fallback) {
      set(fallback);
      return true;
    }

    return false;
  };

  for (const event of events) {
    if (event.type === "assistant_message") {
      const payload = parseAssistantData(event.data);
      const content = payload.content ?? "";
      if (content) {
        items.push({ type: "assistant_message", data: content, message_id: event.message_id });
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
        event.images ?? undefined,
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
        <ToolResultBody result={item.result} images={item.resultImages} />
      )}
      {showToolResults && item.result === undefined && item.resultImages && (
        <div className="ml-4">
          <ImageThumbs images={item.resultImages} />
        </div>
      )}
    </div>
  );
}

type ResultSegment = { text: string } | { images: ImageRef[] };

/**
 * Split tool result text at image offsets ("at") so interleaved images show
 * where the tool put them. Images without "at", or past the end of a
 * truncated result, follow all text.
 */
function interleaveResult(text: string, images: ImageRef[]): ResultSegment[] {
  const segments: ResultSegment[] = [];
  let cursor = 0;
  const pushImage = (image: ImageRef) => {
    const last = segments[segments.length - 1];
    if (last && "images" in last) last.images.push(image);
    else segments.push({ images: [image] });
  };
  for (const image of images) {
    const at = typeof image.at === "number" ? Math.min(Math.max(image.at, cursor), text.length) : text.length;
    if (at > cursor) segments.push({ text: text.slice(cursor, at) });
    pushImage(image);
    cursor = at;
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor) });
  return segments;
}

const RESULT_TEXT_CLASS =
  "whitespace-pre-wrap break-all text-xs text-amber-200";

function ToolResultBody({ result, images }: { result: unknown; images?: ImageRef[] }) {
  const text = resultToText(result);
  const interleaved =
    typeof result === "string" && !!images?.some((image) => typeof image.at === "number");
  if (!interleaved) {
    return (
      <>
        <pre className={`mt-1 ml-4 px-2 py-1.5 bg-gray-950/60 border border-gray-800 rounded overflow-auto max-h-72 ${RESULT_TEXT_CLASS}`}>
          {text}
        </pre>
        {images && images.length > 0 && (
          <div className="ml-4">
            <ImageThumbs images={images} />
          </div>
        )}
      </>
    );
  }
  return (
    <div className="mt-1 ml-4 px-2 py-1.5 bg-gray-950/60 border border-gray-800 rounded overflow-auto max-h-[36rem]">
      {interleaveResult(text, images!).map((segment, index) =>
        "text" in segment ? (
          <pre key={index} className={RESULT_TEXT_CLASS}>
            {segment.text}
          </pre>
        ) : (
          <div key={index} className="mb-1">
            <ImageThumbs images={segment.images} />
          </div>
        ),
      )}
    </div>
  );
}

function EventLine({
  event,
  editing = false,
  editValue = "",
  generating = false,
  deriving = false,
  onStartEdit = () => {},
  onEditChange = () => {},
  onCancelEdit = () => {},
  onSubmitEdit = () => {},
  onDiverge = () => {},
}: {
  event: StreamEvent;
  editing?: boolean;
  editValue?: string;
  generating?: boolean;
  deriving?: boolean;
  onStartEdit?: () => void;
  onEditChange?: (value: string) => void;
  onCancelEdit?: () => void;
  onSubmitEdit?: () => void;
  onDiverge?: () => void;
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
  const hasImages = event.type === "user" && !!event.images?.length;
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
        {hasImages && (
          <div className="flex flex-col gap-1">
            <ImageThumbs images={event.images} />
            <span className="text-xs text-indigo-400/70">Images are kept when you rewrite.</span>
          </div>
        )}
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
            disabled={(!editValue.trim() && !hasImages) || generating || !event.message_id}
            className="px-3 py-1 rounded bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-xs text-white"
          >
            Rewrite from here
          </button>
          <button
            type="button"
            onClick={onDiverge}
            disabled={deriving || !event.message_id}
            className="px-3 py-1 rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-xs text-white"
          >
            Diverge from here
          </button>
        </div>
      </div>
    );
  }
  const canDiverge =
    (event.type === "user" || event.type === "assistant_message") && !!event.message_id;
  return (
    <div className="group relative flex items-start gap-1">
      <div
        className={`flex-1 min-w-0 text-left font-mono text-sm whitespace-pre-wrap break-all ${style} ${
          event.type === "user" && event.message_id ? "cursor-text hover:bg-indigo-950/30 rounded px-1 -mx-1" : ""
        }`}
        onDoubleClick={event.type === "user" && event.message_id && !generating ? onStartEdit : undefined}
        title={
          event.type === "user" && event.message_id && !generating
            ? "Double-click to edit and resend from here"
            : undefined
        }
      >
        {prefix}
        {event.type === "tool_result" ? toOneLine(body) : body}
        {hasImages && <ImageThumbs images={event.images} />}
      </div>
      {canDiverge && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onDiverge();
          }}
          disabled={deriving}
          className="shrink-0 mt-0.5 p-0.5 rounded text-gray-600 opacity-40 group-hover:opacity-100 hover:text-indigo-300 hover:bg-gray-800 disabled:opacity-20 disabled:cursor-wait"
          title="Diverge: open a new session with the history before this message (this message is not included)"
          aria-label="Diverge from this message"
        >
          <DivergeIcon />
        </button>
      )}
    </div>
  );
}

function DivergeIcon() {
  // Git-branch style glyph.
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="6" cy="5" r="2" />
      <circle cx="6" cy="19" r="2" />
      <circle cx="18" cy="7" r="2" />
      <path d="M6 7v10" />
      <path d="M18 9c0 5-12 3-12 8" />
    </svg>
  );
}

function countMessages(items: ConsoleItem[]): number {
  return items.reduce(
    (n, item) =>
      n + (isCompressionGroup(item) ? 1 + countMessages(item.hidden) : 1),
    0,
  );
}

function CompressionBlock({
  group,
  renderItem,
}: {
  group: CompressionGroup;
  renderItem: (item: ConsoleItem, key: string | number) => React.ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const isTurn = group.event.compression?.kind === "turn";
  const hiddenCount = countMessages(group.hidden);
  const summary = String(group.event.data ?? "");
  const label = isTurn ? "本轮已压缩" : "已压缩";
  return (
    <div className="my-2 text-left">
      {expanded && hiddenCount > 0 && (
        <div className="mb-2 rounded-md border border-dashed border-slate-600 bg-slate-800/50 px-2 py-1.5">
          <div className="mb-1 text-[11px] text-slate-400">
            以下 {hiddenCount} 条为被压缩的原始消息，模型已看不到它们，只看到下方摘要
          </div>
          <div className="flex flex-col gap-1 opacity-60 saturate-50">
            {group.hidden.map((item, i) =>
              renderItem(item, isToolCallView(item) ? item.callId : itemMessageId(item) ?? i),
            )}
          </div>
        </div>
      )}
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 text-[11px] text-emerald-400/80 hover:text-emerald-300"
        title={expanded ? "收起被压缩的原始消息" : "展开被压缩的原始消息"}
      >
        <span className="flex-1 border-t border-dashed border-emerald-800" />
        <span className="shrink-0">
          {expanded ? "▾" : "▸"} {label}
          {hiddenCount > 0 ? ` ${hiddenCount} 条消息` : ""}，模型从这里开始只看到摘要
          {hiddenCount > 0 ? (expanded ? "（点击收起）" : "（点击展开原始消息）") : ""}
        </span>
        <span className="flex-1 border-t border-dashed border-emerald-800" />
      </button>
      <div className="mt-1 rounded border border-emerald-900 bg-emerald-950/20 px-2 py-1.5 font-mono text-xs text-emerald-200/90 whitespace-pre-wrap break-words">
        {summary || "(empty summary)"}
      </div>
    </div>
  );
}

function isToolCallView(item: ConsoleItem): item is ToolCallView {
  return "kind" in item && item.kind === "tool_call";
}

function isCompressionGroup(item: ConsoleItem): item is CompressionGroup {
  return "kind" in item && item.kind === "compression_group";
}

function itemMessageId(item: ConsoleItem): string | undefined {
  if (isToolCallView(item)) return undefined;
  return isCompressionGroup(item) ? item.event.message_id : item.message_id;
}

type ConsoleItemLineProps = {
  item: ConsoleItem;
  editingMessageId: string | null;
  editingContent: string;
  generating: boolean;
  deriving: boolean;
  showToolCalls: boolean;
  showToolResults: boolean;
  onStartEdit: (event: StreamEvent) => void;
  onEditChange: (value: string) => void;
  onCancelEdit: () => void;
  onSubmitEdit: () => void;
  onDiverge: (messageId: string) => void;
};

function ConsoleItemLine(props: ConsoleItemLineProps) {
  const {
    item,
    editingMessageId,
    editingContent,
    generating,
    deriving,
    showToolCalls,
    showToolResults,
    onStartEdit,
    onEditChange,
    onCancelEdit,
    onSubmitEdit,
    onDiverge,
  } = props;
  if (isToolCallView(item)) {
    return <ToolCallLine item={item} showToolCalls={showToolCalls} showToolResults={showToolResults} />;
  }
  if (isCompressionGroup(item)) {
    return (
      <CompressionBlock
        group={item}
        renderItem={(child, key) => <ConsoleItemLine key={key} {...props} item={child} />}
      />
    );
  }
  if (item.type === "compression") {
    // Live/unfolded compression event with nothing attached.
    return (
      <CompressionBlock
        group={{ kind: "compression_group", event: item, hidden: [] }}
        renderItem={() => null}
      />
    );
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
      deriving={deriving}
      onStartEdit={() => onStartEdit(item)}
      onEditChange={onEditChange}
      onCancelEdit={onCancelEdit}
      onSubmitEdit={onSubmitEdit}
      onDiverge={() => item.message_id && onDiverge(item.message_id)}
    />
  );
}

export default function LiveConsole() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const sessions = useStore((s) => s.sessions);
  const sendMessage = useStore((s) => s.sendMessage);
  const cancelGeneration = useStore((s) => s.cancelGeneration);
  const retryMessage = useStore((s) => s.retryMessage);
  const divergeFromMessage = useStore((s) => s.divergeFromMessage);
  const compressSession = useStore((s) => s.compressSession);
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
  const [sidebarTab, setSidebarTab] = useState<"overview" | "permissions" | "systemPrompt" | "sessions">("overview");
  const [showToolCalls, setShowToolCalls] = useState(true);
  const [showToolResults, setShowToolResults] = useState(true);
  const [deriving, setDeriving] = useState(false);
  const [compressing, setCompressing] = useState(false);
  const [compressNote, setCompressNote] = useState<
    { scope: string; ok: boolean; text: string } | null
  >(null);
  // Follow-the-tail while the agent streams; persisted so the choice survives reloads.
  const [autoScroll, setAutoScroll] = useState(
    () => localStorage.getItem("liveConsole.autoScroll") !== "false",
  );
  const handleAutoScrollChange = (on: boolean) => {
    setAutoScroll(on);
    localStorage.setItem("liveConsole.autoScroll", String(on));
  };

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
    if (!autoScroll) return;
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entry?.events.length, entry?.session.status, autoScroll]);

  useEffect(() => {
    itemRefs.current = itemRefs.current.slice(0, entry?.events.length ?? 0);
  }, [entry?.events.length]);

  // Image input: only offered when the session's agent accepts images
  // (cagent engine + config.vision); the backend enforces the same rule.
  // Draft images and the hint are scoped to the session they were added in.
  const attachments = useImageAttachments(sessionId);
  const [agent, setAgent] = useState<AgentTemplate | null>(null);
  const [attachHint, setAttachHint] = useState<{ scope: string; text: string } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const agentId = entry?.session.agent_id;
  useEffect(() => {
    if (!agentId) return;
    let cancelled = false;
    api.agents
      .get(agentId)
      .then((a) => !cancelled && setAgent(a))
      .catch(() => !cancelled && setAgent(null));
    return () => {
      cancelled = true;
    };
  }, [agentId]);
  // A previous session's agent must not decide the current composer.
  const canAttachImages = agent?.id === agentId && agentAcceptsImages(agent);
  const hintText = attachHint && attachHint.scope === sessionId ? attachHint.text : null;
  // Manual in-place compression is a CAgent feature; legacy sessions get a
  // disabled button. Unknown until the agent loads.
  const agentLoaded = agent?.id === agentId;
  const isCagent = agentLoaded && (agent?.config?.engine ?? "cagent") === "cagent";
  const note = compressNote && compressNote.scope === sessionId ? compressNote : null;

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

  const readyImages = canAttachImages ? attachments.readyImages() : [];

  const handleSend = () => {
    const msg = input.trim();
    if ((!msg && readyImages.length === 0) || !sessionId || generating || attachments.uploading) return;
    sendMessage(sessionId, msg, readyImages);
    setInput("");
    attachments.clear();
    setAttachHint(null);
  };

  const handleFiles = (files: File[]): boolean => {
    const images = files.filter((f) => f.type.startsWith("image/"));
    if (images.length === 0 || !sessionId) return false;
    if (!canAttachImages) {
      setAttachHint({
        scope: sessionId,
        text: "This agent doesn't accept images — enable “Accept image input (vision)” in the agent settings.",
      });
      return true;
    }
    setAttachHint(null);
    attachments.addFiles(images);
    return true;
  };

  const handlePaste = (e: React.ClipboardEvent) => {
    // Only swallow the paste when it carries images; plain text pastes as usual.
    if (handleFiles(Array.from(e.clipboardData.files))) e.preventDefault();
  };

  const handleDragOver = (e: React.DragEvent) => {
    if (Array.from(e.dataTransfer.types).includes("Files")) e.preventDefault();
  };

  const handleDrop = (e: React.DragEvent) => {
    if (e.dataTransfer.files.length === 0) return;
    e.preventDefault();
    handleFiles(Array.from(e.dataTransfer.files));
  };

  const handleRetrySend = async () => {
    const msg = editingContent.trim();
    const editingHasImages = !!events.find(
      (e) => e.type === "user" && e.message_id === editingMessageId,
    )?.images?.length;
    if ((!msg && !editingHasImages) || !sessionId || !editingMessageId || generating) return;
    await retryMessage(sessionId, editingMessageId, msg);
    setEditingMessageId(null);
    setEditingContent("");
  };

  // Allowed while RUNNING: the branch copies only already-persisted history
  // before messageId and leaves the running source session untouched.
  const handleDiverge = async (messageId: string) => {
    if (!sessionId || deriving) return;
    setDeriving(true);
    try {
      const newSessionId = await divergeFromMessage(sessionId, messageId);
      navigate(`/console/${newSessionId}`);
    } finally {
      setDeriving(false);
    }
  };

  const handleCompress = async () => {
    if (!sessionId || generating || compressing || !isCagent) return;
    setCompressing(true);
    setCompressNote(null);
    try {
      const r = await compressSession(sessionId);
      const est = r.token_count_is_estimate ? "≈" : "";
      setCompressNote({
        scope: sessionId,
        ok: r.status === "compressed",
        text:
          r.status === "compressed"
            ? `已压缩：上下文 ${est}${r.num_tokens_before ?? "?"} → ${est}${r.num_tokens_after ?? "?"} tokens`
            : r.status === "skipped"
              ? `无需压缩：${r.reason ?? ""}`
              : `压缩失败：${r.reason ?? ""}`,
      });
    } catch (e) {
      setCompressNote({ scope: sessionId, ok: false, text: `压缩失败：${(e as Error).message}` });
    } finally {
      setCompressing(false);
    }
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
        <div className="flex flex-col items-end gap-1">
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
        {session.status === "RUNNING" && (
          <label className="flex items-center gap-1.5 text-xs text-gray-400 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={(e) => handleAutoScrollChange(e.target.checked)}
            />
            自动滚动到底部
          </label>
        )}
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
              key={isToolCallView(item) ? item.callId : itemMessageId(item) ?? i}
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
                deriving={deriving}
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
                onDiverge={handleDiverge}
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
              ["sessions", "Sessions"],
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
                <div className="shrink-0 border-t border-gray-800 pt-3">
                  <button
                    type="button"
                    onClick={handleCompress}
                    disabled={generating || compressing || !isCagent || events.length === 0}
                    title={
                      !agentLoaded
                        ? undefined
                        : !isCagent
                          ? "Manual compression is only supported by the cagent engine"
                          : "Summarize history through the last completed turn, in place. Earlier messages stay visible but the model sees only the summary."
                    }
                    className="w-full rounded bg-indigo-600 px-3 py-2 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-40"
                  >
                    {compressing ? "Compressing…" : "Compress"}
                  </button>
                  {agentLoaded && !isCagent && (
                    <p className="mt-1.5 text-[11px] text-gray-500">仅 cagent 引擎支持压缩</p>
                  )}
                  {note && (
                    <p className={`mt-1.5 text-[11px] break-words ${note.ok ? "text-emerald-400" : "text-amber-400"}`}>
                      {note.text}
                    </p>
                  )}
                </div>
              </div>
            )}
            {sidebarTab === "sessions" && (
              <SessionListPanel
                sessions={Object.values(sessions)}
                currentSessionId={session.id}
                onOpen={(id) => navigate(`/console/${id}`)}
              />
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
            {sidebarTab === "systemPrompt" && <SystemPromptPanel key={session.id} session={session} />}
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
      <div className="flex flex-col gap-2" onDragOver={handleDragOver} onDrop={handleDrop}>
        <AttachmentTray items={attachments.items} onRemove={attachments.remove} />
        {hintText && <div className="text-xs text-amber-400">{hintText}</div>}
        <div className="flex gap-2">
          {canAttachImages && (
            <>
              <input
                ref={fileInputRef}
                type="file"
                accept={ACCEPTED_IMAGE_TYPES}
                multiple
                hidden
                onChange={(e) => {
                  handleFiles(Array.from(e.target.files ?? []));
                  e.target.value = "";
                }}
              />
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={generating}
                title="Attach images (you can also paste or drop them)"
                className="px-3 py-2 rounded-lg bg-gray-800 border border-gray-600 hover:border-indigo-500 disabled:opacity-40 text-gray-300 text-sm self-end"
              >
                Image
              </button>
            </>
          )}
          <textarea
            className="flex-1 bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-500 resize-none focus:outline-none focus:border-indigo-500 disabled:opacity-50"
            rows={2}
            placeholder={
              generating
                ? "Agent is generating… wait for it to finish"
                : canAttachImages
                  ? "Send a message… (Enter to send, Shift+Enter for newline; paste or drop images)"
                  : "Send a message… (Enter to send, Shift+Enter for newline)"
            }
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            disabled={generating}
          />
          {generating ? (
            <button
              type="button"
              onClick={() => sessionId && cancelGeneration(sessionId)}
              className="px-4 py-2 rounded-lg bg-red-700 hover:bg-red-600 text-white text-sm font-medium transition-colors self-end"
            >
              Stop
            </button>
          ) : (
            <button
              type="button"
              onClick={handleSend}
              disabled={(!input.trim() && readyImages.length === 0) || attachments.uploading}
              title={attachments.uploading ? "Waiting for images to finish uploading" : undefined}
              className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white text-sm font-medium transition-colors self-end"
            >
              Send
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
