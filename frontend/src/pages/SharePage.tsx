import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api, sharedAttachmentUrl } from "../api/client";
import type { SharedSession } from "../api/types";
import { messageToEvent } from "../store/sessions";
import ConsoleItemLine from "../components/console/ConsoleItemLine";
import { AttachmentUrlContext } from "../components/console/attachmentUrl";
import { CompressedLoaderContext } from "../components/console/compressedLoader";
import { MessageFocusContext, type MessageFocus } from "../components/console/messageFocus";
import { buildConsoleItems, isToolCallView, itemMessageId } from "../components/console/items";

/** Public, read-only transcript of a session its owner shared. No login. */
export default function SharePage() {
  const { sessionId = "" } = useParams<{ sessionId: string }>();
  // ?m=<message id>: scroll to that message, expanding the summaries it was
  // compressed under (and showing deleted rows if it was deleted).
  const [searchParams] = useSearchParams();
  const targetId = searchParams.get("m");
  const [located, setLocated] = useState<
    { key: string; expand: string[]; note: string | null } | null
  >(null);
  const scrolledFor = useRef<string | null>(null);
  const [linkNote, setLinkNote] = useState<string | null>(null);
  useEffect(() => {
    if (!linkNote) return;
    const timer = setTimeout(() => setLinkNote(null), 4000);
    return () => clearTimeout(timer);
  }, [linkNote]);
  const [data, setData] = useState<{ scope: string; value: SharedSession } | null>(null);
  const [error, setError] = useState<{ scope: string; text: string } | null>(null);
  const [showToolCalls, setShowToolCalls] = useState(true);
  const [showToolResults, setShowToolResults] = useState(true);
  const [showDeleted, setShowDeleted] = useState(false);
  // Toggling while a request is in flight: only the newest response counts.
  const latestLoad = useRef(0);

  const load = useCallback(() => {
    const seq = ++latestLoad.current;
    api.share
      .get(sessionId, showDeleted)
      .then((value) => {
        if (seq !== latestLoad.current) return;
        setData({ scope: sessionId, value });
        setError(null);
      })
      .catch((e: Error) => {
        if (seq !== latestLoad.current) return;
        setError({
          scope: sessionId,
          text: e.message.startsWith("404") ? "这个分享链接不存在，或者分享已被关闭。" : `加载失败：${e.message}`,
        });
      });
  }, [sessionId, showDeleted]);

  const locateKey = targetId ? `${sessionId}/${targetId}` : null;
  useEffect(() => {
    if (!targetId || !locateKey) return;
    let cancelled = false;
    api.share
      .locate(sessionId, targetId)
      .then((loc) => {
        if (cancelled) return;
        if (loc.deleted) setShowDeleted(true);
        if (loc.role === "tool" || loc.role === "tool_call" || loc.role === "agent") {
          setShowToolCalls(true);
          if (loc.role === "tool") setShowToolResults(true);
        }
        setLocated({ key: locateKey, expand: loc.expand, note: null });
      })
      .catch(() => {
        if (!cancelled) setLocated({ key: locateKey, expand: [], note: "链接指向的消息不存在或不可见" });
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, targetId, locateKey]);
  const location = located?.key === locateKey ? located : null;
  // Hold the transcript until the target is located, so its summaries mount open.
  const waitingForLocation = !!locateKey && !location;

  useEffect(() => {
    load();
  }, [load]);

  const focus = useMemo<MessageFocus>(
    () => ({
      targetId: location && !location.note ? targetId : null,
      expand: new Set(location?.expand ?? []),
      claimScroll: () => {
        if (!locateKey || scrolledFor.current === locateKey) return false;
        scrolledFor.current = locateKey;
        return true;
      },
      linkTo: (messageId: string) => {
        const url = `${window.location.origin}/share/${sessionId}?m=${encodeURIComponent(messageId)}`;
        // Update the address bar only: going through the router would re-run
        // the lookup and remount the transcript, collapsing open summaries.
        window.history.replaceState(window.history.state, "", url);
        navigator.clipboard
          .writeText(url)
          .then(() => setLinkNote("已复制指向这条消息的链接"))
          .catch(() => setLinkNote(`无法自动复制，请从地址栏复制：${url}`));
      },
    }),
    [location, targetId, locateKey, sessionId],
  );

  const shared = data?.scope === sessionId ? data.value : null;
  const err = error?.scope === sessionId ? error.text : null;
  const running = shared?.session.status === "RUNNING";

  // A running session keeps growing; poll while it does.
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [running, load]);

  const resolveUrl = useCallback((ref: string) => sharedAttachmentUrl(sessionId, ref), [sessionId]);
  const loadCompressed = useCallback(
    async (summaryId: string) =>
      (await api.share.compressedMessages(sessionId, summaryId)).map(messageToEvent),
    [sessionId],
  );

  if (!shared || waitingForLocation) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-950 text-sm text-gray-400">
        {err ?? "加载中…"}
      </div>
    );
  }

  const items = buildConsoleItems(shared.messages.map(messageToEvent));
  const { session } = shared;

  return (
    <AttachmentUrlContext.Provider value={resolveUrl}>
    <CompressedLoaderContext.Provider value={loadCompressed}>
    <MessageFocusContext.Provider value={focus}>
      <div className="min-h-screen bg-gray-950 text-gray-100">
        <div className="mx-auto flex max-w-5xl flex-col gap-3 p-4">
          <header className="flex flex-wrap items-end justify-between gap-2 border-b border-gray-800 pb-3">
            <div>
              <h1 className="text-lg font-semibold text-white">{session.title ?? "Shared session"}</h1>
              <p className="text-xs text-gray-500">
                {session.agent_name ? `${session.agent_name} · ` : ""}
                {new Date(session.created_at).toLocaleString()}
                {running && <span className="ml-2 text-indigo-300">进行中，自动刷新</span>}
              </p>
            </div>
            <div className="flex items-center gap-3 text-xs text-gray-400">
              <label className="flex items-center gap-1.5 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={showToolCalls}
                  onChange={(e) => {
                    setShowToolCalls(e.target.checked);
                    if (!e.target.checked) setShowToolResults(false);
                  }}
                />
                工具调用
              </label>
              <label className="flex items-center gap-1.5 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={showToolResults}
                  disabled={!showToolCalls}
                  onChange={(e) => setShowToolResults(e.target.checked)}
                />
                工具结果
              </label>
              <label className="flex items-center gap-1.5 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={showDeleted}
                  onChange={(e) => setShowDeleted(e.target.checked)}
                />
                显示已删除消息
              </label>
              <span className="rounded-full bg-gray-800 px-2 py-0.5 text-gray-500">只读</span>
            </div>
          </header>
          {linkNote && (
            <p className="fixed bottom-4 left-1/2 z-50 -translate-x-1/2 rounded border border-gray-700 bg-gray-900 px-3 py-1.5 text-xs text-emerald-300 shadow-xl">
              {linkNote}
            </p>
          )}
          {location?.note && (
            <p className="rounded border border-amber-800 bg-amber-950/40 px-3 py-1.5 text-xs text-amber-300">
              {location.note}
            </p>
          )}
          <main className="flex flex-col gap-1 rounded-xl border border-gray-700 bg-gray-900 p-4 pl-6">
            {items.length === 0 && <p className="font-mono text-sm text-gray-600">（暂无消息）</p>}
            {items.map((item, i) => (
              <div key={isToolCallView(item) ? item.callId : itemMessageId(item) ?? i}>
                <ConsoleItemLine
                  item={item}
                  readOnly
                  showToolCalls={showToolCalls}
                  showToolResults={showToolResults}
                />
              </div>
            ))}
          </main>
        </div>
      </div>
    </MessageFocusContext.Provider>
    </CompressedLoaderContext.Provider>
    </AttachmentUrlContext.Provider>
  );
}
