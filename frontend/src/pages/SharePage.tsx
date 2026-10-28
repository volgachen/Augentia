import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, sharedAttachmentUrl } from "../api/client";
import type { SharedSession } from "../api/types";
import { messageToEvent } from "../store/sessions";
import ConsoleItemLine from "../components/console/ConsoleItemLine";
import { AttachmentUrlContext } from "../components/console/attachmentUrl";
import { buildConsoleItems, isToolCallView, itemMessageId } from "../components/console/items";

/** Public, read-only transcript of a session its owner shared. No login. */
export default function SharePage() {
  const { sessionId = "" } = useParams<{ sessionId: string }>();
  const [data, setData] = useState<{ scope: string; value: SharedSession } | null>(null);
  const [error, setError] = useState<{ scope: string; text: string } | null>(null);
  const [showToolCalls, setShowToolCalls] = useState(true);
  const [showToolResults, setShowToolResults] = useState(true);

  const load = useCallback(() => {
    api.share
      .get(sessionId)
      .then((value) => {
        setData({ scope: sessionId, value });
        setError(null);
      })
      .catch((e: Error) =>
        setError({
          scope: sessionId,
          text: e.message.startsWith("404") ? "这个分享链接不存在，或者分享已被关闭。" : `加载失败：${e.message}`,
        }),
      );
  }, [sessionId]);

  useEffect(() => {
    load();
  }, [load]);

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

  if (!shared) {
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
              <span className="rounded-full bg-gray-800 px-2 py-0.5 text-gray-500">只读</span>
            </div>
          </header>
          <main className="flex flex-col gap-1 rounded-xl border border-gray-700 bg-gray-900 p-4">
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
    </AttachmentUrlContext.Provider>
  );
}
