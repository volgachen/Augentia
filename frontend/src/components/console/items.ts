import type { ImageRef, StreamEvent } from "../../api/types";

// Pure transcript shaping shared by the live console and the read-only share
// page: raw stream events -> render items (tool calls paired with results,
// compression summaries as their own items).

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
  // Row that owns this call: the assistant message (native layout) or the
  // legacy tool_call row. Deleting it removes the call and its result.
  messageId?: string;
  // The owning row was deleted by the user (shown only on request).
  deleted?: boolean;
  result?: unknown;
  // Images the tool returned (e.g. read on an image file).
  resultImages?: ImageRef[];
}

// A compression summary. The rows it replaced are no longer in the
// transcript (soft-deleted, metadata.compressed_by = its id); the console
// fetches them when the summary is expanded.
export interface CompressionGroup {
  kind: "compression_group";
  event: StreamEvent;
}

export type ConsoleItem = StreamEvent | ToolCallView | CompressionGroup;

export function buildConsoleItems(events: StreamEvent[]): ConsoleItem[] {
  const items: ConsoleItem[] = [];
  let run: StreamEvent[] = [];
  const flush = () => {
    items.push(...buildFlatItems(run));
    run = [];
  };
  for (const event of events) {
    if (event.type === "compression") {
      flush();
      items.push({ kind: "compression_group", event });
    } else {
      run.push(event);
    }
  }
  flush();
  return items;
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
        items.push({
          type: "assistant_message", data: content, message_id: event.message_id,
          deleted: event.deleted,
        });
      }
      for (const [index, toolCall] of (payload.tool_calls ?? []).entries()) {
        const name = toolCall.function?.name ?? "unknown";
        const callId = toolCall.id ?? `${items.length}:tool:${index}`;
        const item: ToolCallView = {
          kind: "tool_call",
          callId,
          name,
          args: parseToolArguments(toolCall.function?.arguments),
          messageId: event.message_id,
          deleted: event.deleted,
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
          messageId: event.type === "tool_call" ? event.message_id : undefined,
          deleted: event.deleted,
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
