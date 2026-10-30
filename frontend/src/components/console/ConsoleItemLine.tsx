import { useLayoutEffect, useRef, useState } from "react";
import type { ImageRef, StreamEvent } from "../../api/types";
import ToolPreview from "../ToolPreview";
import { ImageThumbs } from "../ImageAttachments";
import { getToolSummary } from "../../config/toolDisplay";
import { useCompressedLoader } from "./compressedLoader";
import {
  buildConsoleItems,
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
  onDelete,
  deleteDisabled = false,
}: {
  item: ToolCallView;
  showToolCalls: boolean;
  showToolResults: boolean;
  onDelete?: () => void;
  deleteDisabled?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const summary = getToolSummary(item.name, item.args);
  if (!showToolCalls) return null;

  return (
    <div className="text-left font-mono text-sm text-yellow-400">
      <div className="group flex items-start gap-1">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="flex-1 min-w-0 text-left flex items-start gap-1 hover:bg-gray-800/40 rounded px-1 -mx-1 cursor-pointer"
          title={`${expanded ? "Collapse" : "Expand"} · call_id: ${item.callId}`}
        >
          <span className="text-gray-500 select-none">{expanded ? "▾" : "▸"}</span>
          <span className="shrink-0">⚙ {item.name}</span>
          {summary && <span className="text-gray-400 truncate">{summary}</span>}
        </button>
        {onDelete && (
          <DeleteButton onDelete={onDelete} disabled={deleteDisabled} title={DELETE_TITLE.tool_call} />
        )}
      </div>
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
  onDelete,
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
  onDelete?: () => void;
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
        <DivergeButton
          onDiverge={onDiverge}
          disabled={deriving}
          title="Diverge: open a new session with the history before this message (this message is not included)"
        />
      )}
      {onDelete && (
        <DeleteButton
          onDelete={onDelete}
          disabled={generating}
          title={DELETE_TITLE[event.type] ?? "删除这条消息"}
        />
      )}
    </div>
  );
}

function DeleteButton({
  onDelete,
  disabled,
  title,
}: {
  onDelete: () => void;
  disabled: boolean;
  title: string;
}) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onDelete();
      }}
      disabled={disabled}
      className="shrink-0 mt-0.5 p-0.5 rounded text-gray-600 opacity-40 group-hover:opacity-100 hover:text-red-400 hover:bg-gray-800 disabled:opacity-20 disabled:cursor-not-allowed"
      title={title}
      aria-label="Delete"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M3 6h18" />
        <path d="M8 6V4h8v2" />
        <path d="M19 6l-1 14H6L5 6" />
      </svg>
    </button>
  );
}

const DELETE_TITLE: Record<string, string> = {
  user: "删除这条用户输入",
  assistant_message: "删除这次模型输出（含其文字、工具调用和工具结果）",
  tool_call: "删除这次模型输出（含其文字、工具调用和工具结果）",
  tool_result: "删除这条工具结果",
  compression: "删除这条摘要（被它替换的原始消息会重新对模型可见）",
};

function DivergeButton({
  onDiverge,
  disabled,
  title,
}: {
  onDiverge: () => void;
  disabled: boolean;
  title: string;
}) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onDiverge();
      }}
      disabled={disabled}
      className="shrink-0 mt-0.5 p-0.5 rounded text-gray-600 opacity-40 group-hover:opacity-100 hover:text-indigo-300 hover:bg-gray-800 disabled:opacity-20 disabled:cursor-wait"
      title={title}
      aria-label="Diverge from this message"
    >
      <DivergeIcon />
    </button>
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

type LoadState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; text: string }
  | { status: "loaded"; items: ConsoleItem[]; count: number };

function CompressionBlock({
  group,
  depth,
  renderItem,
  onDelete,
  deleteDisabled = false,
  onDiverge,
  deriving = false,
}: {
  group: CompressionGroup;
  onDelete?: () => void;
  deleteDisabled?: boolean;
  // Branch from this summary: the new session keeps history through it.
  onDiverge?: () => void;
  deriving?: boolean;
  // How many expanded compression boxes enclose this one.
  depth: number;
  renderItem: (item: ConsoleItem, key: string | number) => React.ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const [load, setLoad] = useState<LoadState>({ status: "idle" });
  const loadCompressed = useCompressedLoader();
  const rootRef = useRef<HTMLDivElement>(null);
  const collapsedFromStickyRef = useRef(false);
  const summaryId = group.event.message_id;
  const compressionKind = group.event.compression?.kind;
  const summary = String(group.event.data ?? "");
  const label =
    compressionKind === "turn" ? "本轮已压缩" : compressionKind === "range" ? "区间已压缩" : "已压缩";
  const canExpand = !!summaryId && !!loadCompressed;
  const count =
    load.status === "loaded" ? load.count : group.event.compression?.replacedCount;

  // Collapsing from the sticky button removes the content being read, so
  // bring this block (now just the toggle + summary) back into view.
  useLayoutEffect(() => {
    if (expanded || !collapsedFromStickyRef.current) return;
    collapsedFromStickyRef.current = false;
    rootRef.current?.scrollIntoView({ block: "nearest" });
  }, [expanded]);

  // The originals are soft-deleted under this summary; fetch them the first
  // time it is expanded (or again after a failure).
  const toggle = () => {
    if (!canExpand) return;
    const next = !expanded;
    setExpanded(next);
    if (!next || load.status === "loading" || load.status === "loaded") return;
    setLoad({ status: "loading" });
    loadCompressed(summaryId)
      .then((events) => setLoad({ status: "loaded", items: buildConsoleItems(events), count: events.length }))
      .catch((e: unknown) =>
        setLoad({ status: "error", text: e instanceof Error ? e.message : String(e) }),
      );
  };

  return (
    <div ref={rootRef} className="my-2 text-left">
      {expanded && (
        <div className="mb-2 rounded-md border border-dashed border-slate-600 bg-slate-800/50 px-2 py-1.5">
          {/* Sticks to the top-left of the visible area while this box is on
              screen; nested blocks stack below their parent's button. */}
          <div
            className="sticky z-20 w-fit"
            style={{ top: `${depth * 1.75}rem` }}
          >
            <button
              type="button"
              onClick={() => {
                collapsedFromStickyRef.current = true;
                setExpanded(false);
              }}
              className="rounded border border-slate-500 bg-slate-900/95 px-2 py-0.5 text-[11px] text-slate-200 shadow hover:border-emerald-600 hover:text-emerald-300"
              title="收起被压缩的原始消息"
            >
              ▴ 收起{depth > 0 ? `（第 ${depth + 1} 层）` : ""}
            </button>
          </div>
          {load.status === "loading" && (
            <div className="mt-1 text-[11px] text-slate-400">加载原始消息…</div>
          )}
          {load.status === "error" && (
            <div className="mt-1 text-[11px] text-red-400">加载失败：{load.text}</div>
          )}
          {load.status === "loaded" && (
            <>
              <div className="mb-1 mt-1 text-[11px] text-slate-400">
                {load.count > 0
                  ? `以下 ${load.count} 条为被压缩的原始消息，模型已看不到它们，只看到下方摘要`
                  : "没有保存被压缩的原始消息"}
              </div>
              <div className="flex flex-col gap-1 opacity-60 saturate-50">
                {load.items.map((item, i) =>
                  renderItem(item, isToolCallView(item) ? item.callId : itemMessageId(item) ?? i),
                )}
              </div>
            </>
          )}
        </div>
      )}
      <button
        type="button"
        onClick={toggle}
        disabled={!canExpand}
        className="w-full flex items-center gap-2 text-[11px] text-emerald-400/80 hover:text-emerald-300 disabled:hover:text-emerald-400/80"
        title={canExpand ? (expanded ? "收起被压缩的原始消息" : "展开被压缩的原始消息") : undefined}
      >
        <span className="flex-1 border-t border-dashed border-emerald-800" />
        <span className="shrink-0">
          {canExpand ? (expanded ? "▾ " : "▸ ") : ""}
          {label}
          {count ? ` ${count} 条消息` : ""}，模型从这里开始只看到摘要
          {canExpand ? (expanded ? "（点击收起）" : "（点击展开原始消息）") : ""}
        </span>
        <span className="flex-1 border-t border-dashed border-emerald-800" />
      </button>
      <div className="group mt-1 flex items-start gap-1">
        <div className="flex-1 min-w-0 rounded border border-emerald-900 bg-emerald-950/20 px-2 py-1.5 font-mono text-xs text-emerald-200/90 whitespace-pre-wrap break-words">
          {summary || "(empty summary)"}
        </div>
        {onDiverge && (
          <DivergeButton
            onDiverge={onDiverge}
            disabled={deriving}
            title="Diverge: open a new session with the history up to and including this summary (it becomes a plain message there)"
          />
        )}
        {onDelete && (
          <DeleteButton onDelete={onDelete} disabled={deleteDisabled} title={DELETE_TITLE.compression} />
        )}
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
  // Delete a transcript entry by its message row id (omit to hide buttons).
  onDelete?: (messageId: string) => void;
  // Internal: nesting level inside expanded compression boxes.
  compressionDepth?: number;
};

// Rows the user deleted, shown on request: dimmed, struck through, inert.
function DeletedWrap({ children }: { children: React.ReactNode }) {
  return (
    <div className="relative opacity-40 line-through decoration-gray-500" title="已删除：模型看不到这条消息">
      <span className="absolute right-0 top-0 rounded bg-red-950 px-1 text-[10px] text-red-300 no-underline">
        已删除
      </span>
      {children}
    </div>
  );
}

export default function ConsoleItemLine(props: ConsoleItemLineProps) {
  const { item } = props;
  const deleted = isCompressionGroup(item) ? !!item.event.deleted : !!item.deleted;
  if (deleted) {
    return (
      <DeletedWrap>
        <ConsoleItemLineInner {...props} readOnly />
      </DeletedWrap>
    );
  }
  return <ConsoleItemLineInner {...props} />;
}

function ConsoleItemLineInner(props: ConsoleItemLineProps) {
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
    onDelete,
    compressionDepth = 0,
  } = props;
  const deleteHandler = (messageId: string | undefined) =>
    !readOnly && onDelete && messageId ? () => onDelete(messageId) : undefined;
  if (isToolCallView(item)) {
    return (
      <ToolCallLine
        item={item}
        showToolCalls={showToolCalls}
        showToolResults={showToolResults}
        onDelete={deleteHandler(item.messageId)}
        deleteDisabled={generating}
      />
    );
  }
  if (isCompressionGroup(item)) {
    return (
      <CompressionBlock
        group={item}
        depth={compressionDepth}
        onDelete={deleteHandler(item.event.message_id)}
        deleteDisabled={generating}
        onDiverge={
          !readOnly && onDiverge && item.event.message_id
            ? () => onDiverge(item.event.message_id!)
            : undefined
        }
        deriving={deriving}
        // Originals are already out of the model's view: show them read-only.
        renderItem={(child, key) => (
          <ConsoleItemLine
            key={key}
            {...props}
            readOnly
            item={child}
            compressionDepth={compressionDepth + 1}
          />
        )}
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
      onDelete={
        ["user", "assistant_message", "tool_result"].includes(item.type)
          ? deleteHandler(item.message_id)
          : undefined
      }
    />
  );
}
