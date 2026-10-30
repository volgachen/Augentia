import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { messageToEvent, useStore } from "../store/sessions";
import { CompressedLoaderContext } from "../components/console/compressedLoader";
import { api } from "../api/client";
import { agentAcceptsImages } from "../api/types";
import type { AgentTemplate } from "../api/types";
import TaskListPanel from "../components/TaskListPanel";
import SubAgentListPanel from "../components/SubAgentListPanel";
import SessionListPanel from "../components/SessionListPanel";
import ToolConfirmPanel from "../components/ToolConfirmPanel";
import ToolPermissionsPanel from "../components/ToolPermissionsPanel";
import SystemPromptPanel from "../components/SystemPromptPanel";
import ShareButton from "../components/ShareButton";
import { AttachmentTray } from "../components/ImageAttachments";
import { ACCEPTED_IMAGE_TYPES, useImageAttachments } from "../hooks/useImageAttachments";
import ConsoleItemLine from "../components/console/ConsoleItemLine";
import { buildConsoleItems, isToolCallView, itemMessageId } from "../components/console/items";

export default function LiveConsole() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const sessions = useStore((s) => s.sessions);
  const sendMessage = useStore((s) => s.sendMessage);
  const cancelGeneration = useStore((s) => s.cancelGeneration);
  const retryMessage = useStore((s) => s.retryMessage);
  const divergeFromMessage = useStore((s) => s.divergeFromMessage);
  const deleteMessage = useStore((s) => s.deleteMessage);
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
  // Optional manual-compression options: only the last N hours (range
  // compression, earlier history kept) and a replacement instruction.
  const [compressHours, setCompressHours] = useState("");
  const [compressInstruction, setCompressInstruction] = useState("");
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

  const loadCompressed = useCallback(
    async (summaryId: string) =>
      sessionId
        ? (await api.sessions.compressedMessages(sessionId, summaryId)).map(messageToEvent)
        : [],
    [sessionId],
  );

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

  const handleDelete = async (messageId: string) => {
    if (!sessionId || generating || session.status === "WAITING_CONFIRM") return;
    if (!window.confirm("删除这条消息？模型之后将看不到它。")) return;
    try {
      await deleteMessage(sessionId, messageId);
    } catch (e) {
      window.alert(`删除失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const handleCompress = async () => {
    if (!sessionId || generating || compressing || !isCagent) return;
    const hours = compressHours.trim() ? Number(compressHours) : undefined;
    if (hours !== undefined && !(Number.isFinite(hours) && hours > 0)) {
      setCompressNote({ scope: sessionId, ok: false, text: "小时数需为正数" });
      return;
    }
    const instruction = compressInstruction.trim() || undefined;
    setCompressing(true);
    setCompressNote(null);
    try {
      const r = await compressSession(
        sessionId,
        hours !== undefined || instruction ? { hours, instruction } : undefined,
      );
      const est = r.token_count_is_estimate ? "≈" : "";
      setCompressNote({
        scope: sessionId,
        ok: r.status === "compressed",
        text:
          r.status === "compressed"
            ? `${r.kind === "range" ? "已压缩区间" : "已压缩"}：上下文 ${est}${r.num_tokens_before ?? "?"} → ${est}${r.num_tokens_after ?? "?"} tokens`
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
    <CompressedLoaderContext.Provider value={loadCompressed}>
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
          <ShareButton sessionId={session.id} />
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
                onDelete={handleDelete}
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
                          : "Summarize history through the last completed turn, in place (or only the last N hours, keeping earlier history). Original messages stay visible but the model sees only the summary."
                    }
                    className="w-full rounded bg-indigo-600 px-3 py-2 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-40"
                  >
                    {compressing ? "Compressing…" : "Compress"}
                  </button>
                  {isCagent && (
                    <div className="mt-2 flex flex-col gap-1.5">
                      <label className="flex items-center gap-1.5 text-[11px] text-gray-400">
                        只压缩最近
                        <input
                          type="number"
                          min={0}
                          step="any"
                          value={compressHours}
                          onChange={(e) => setCompressHours(e.target.value)}
                          placeholder="全部"
                          disabled={compressing}
                          className="w-16 rounded border border-gray-700 bg-gray-900 px-1.5 py-0.5 text-[11px] text-gray-200 placeholder:text-gray-600"
                        />
                        小时
                      </label>
                      <textarea
                        value={compressInstruction}
                        onChange={(e) => setCompressInstruction(e.target.value)}
                        placeholder="自定义压缩指令（可选，整个替换默认指令）"
                        rows={2}
                        disabled={compressing}
                        className="w-full resize-y rounded border border-gray-700 bg-gray-900 px-1.5 py-1 text-[11px] text-gray-200 placeholder:text-gray-600"
                      />
                    </div>
                  )}
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
    </CompressedLoaderContext.Provider>
  );
}
