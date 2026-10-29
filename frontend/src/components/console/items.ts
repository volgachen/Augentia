import type { ImageRef, StreamEvent } from "../../api/types";

// Pure transcript shaping shared by the live console and the read-only share
// page: raw stream events -> render items (tool calls paired with results,
// compressed history folded under its summary).

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

export interface ToolCallView {
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
export interface CompressionGroup {
  kind: "compression_group";
  event: StreamEvent;
  hidden: ConsoleItem[];
}

export type ConsoleItem = StreamEvent | ToolCallView | CompressionGroup;

type FoldNode = { event: StreamEvent; hidden?: FoldNode[] };

function nodeContains(node: FoldNode, messageId: string): boolean {
  if (node.event.message_id === messageId) return true;
  return !!node.hidden?.some((child) => nodeContains(child, messageId));
}

// Nest every row a compression replaced under that compression's event. A
// whole-session compression replaces everything through its boundary (earlier
// compression groups included, so they nest); a turn or range compression
// replaces exactly the listed rows (an earlier summary among them nests). Events without compression info pass through.
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

export function buildConsoleItems(events: StreamEvent[]): ConsoleItem[] {
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

export function resultToText(result: unknown): string {
  if (typeof result === "string") return result;
  if (result === undefined) return "";
  try {
    return JSON.stringify(result, null, 2);
  } catch {
    return String(result);
  }
}

export function toOneLine(s: string, max = 200): string {
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

export type ResultSegment = { text: string } | { images: ImageRef[] };

/**
 * Split tool result text at image offsets ("at") so interleaved images show
 * where the tool put them. Images without "at", or past the end of a
 * truncated result, follow all text.
 */
export function interleaveResult(text: string, images: ImageRef[]): ResultSegment[] {
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

export function countMessages(items: ConsoleItem[]): number {
  return items.reduce(
    (n, item) =>
      n + (isCompressionGroup(item) ? 1 + countMessages(item.hidden) : 1),
    0,
  );
}

export function isToolCallView(item: ConsoleItem): item is ToolCallView {
  return "kind" in item && item.kind === "tool_call";
}

export function isCompressionGroup(item: ConsoleItem): item is CompressionGroup {
  return "kind" in item && item.kind === "compression_group";
}

export function itemMessageId(item: ConsoleItem): string | undefined {
  if (isToolCallView(item)) return undefined;
  return isCompressionGroup(item) ? item.event.message_id : item.message_id;
}
