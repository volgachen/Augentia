import { useState } from "react";
import type { ImageRef, StreamEvent } from "../../api/types";
import ToolPreview from "../ToolPreview";
import { ImageThumbs } from "../ImageAttachments";
import { getToolSummary } from "../../config/toolDisplay";
import {
  countMessages,
  interleaveResult,
  isCompressionGroup,
  isToolCallView,
  itemMessageId,
  resultToText,
  toOneLine,
} from "./items";
import type { CompressionGroup, ConsoleItem, ToolCallView } from "./items";

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
  readOnly = false,
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
  readOnly?: boolean;
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
  const editable = !readOnly && event.type === "user" && !!event.message_id;
  const canDiverge =
    !readOnly && (event.type === "user" || event.type === "assistant_message") && !!event.message_id;
  return (
    <div className="group relative flex items-start gap-1">
      <div
        className={`flex-1 min-w-0 text-left font-mono text-sm whitespace-pre-wrap break-all ${style} ${
          editable ? "cursor-text hover:bg-indigo-950/30 rounded px-1 -mx-1" : ""
        }`}
        onDoubleClick={editable && !generating ? onStartEdit : undefined}
        title={editable && !generating ? "Double-click to edit and resend from here" : undefined}
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

export type ConsoleItemLineProps = {
  item: ConsoleItem;
  showToolCalls: boolean;
  showToolResults: boolean;
  // Read-only transcripts (the share page) omit everything below: no
  // editing, rewriting or diverging.
  readOnly?: boolean;
  editingMessageId?: string | null;
  editingContent?: string;
  generating?: boolean;
  deriving?: boolean;
  onStartEdit?: (event: StreamEvent) => void;
  onEditChange?: (value: string) => void;
  onCancelEdit?: () => void;
  onSubmitEdit?: () => void;
  onDiverge?: (messageId: string) => void;
};

export default function ConsoleItemLine(props: ConsoleItemLineProps) {
  const {
    item,
    showToolCalls,
    showToolResults,
    readOnly = false,
    editingMessageId = null,
    editingContent = "",
    generating = false,
    deriving = false,
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
      readOnly={readOnly}
      editing={!readOnly && !!item.message_id && editingMessageId === item.message_id}
      editValue={editingContent}
      generating={generating}
      deriving={deriving}
      onStartEdit={() => onStartEdit?.(item)}
      onEditChange={onEditChange}
      onCancelEdit={onCancelEdit}
      onSubmitEdit={onSubmitEdit}
      onDiverge={() => item.message_id && onDiverge?.(item.message_id)}
    />
  );
}
