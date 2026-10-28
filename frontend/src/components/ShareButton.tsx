import { useEffect, useState } from "react";
import { api } from "../api/client";

function shareUrl(sessionId: string): string {
  return `${window.location.origin}/share/${sessionId}`;
}

/** Toggle public read-only sharing of a session and copy its /share link. */
export default function ShareButton({ sessionId }: { sessionId: string }) {
  const [state, setState] = useState<{ scope: string; enabled: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.sessions
      .share(sessionId)
      .then((s) => !cancelled && setState({ scope: sessionId, enabled: s.enabled }))
      .catch(() => !cancelled && setState(null));
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  // Ignore a previous session's state while the new one loads.
  const enabled = state?.scope === sessionId && state.enabled;
  const url = shareUrl(sessionId);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setNote("链接已复制");
    } catch {
      // Clipboard API needs a secure context (https / localhost); LAN http may lack it.
      setNote("无法自动复制，请手动复制上面的链接");
    }
  };

  const toggle = async (next: boolean) => {
    setBusy(true);
    setNote(null);
    try {
      const s = await api.sessions.updateShare(sessionId, next);
      setState({ scope: sessionId, enabled: s.enabled });
      if (s.enabled) await copy();
    } catch (e) {
      setNote(`操作失败：${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => {
          setOpen((v) => !v);
          setNote(null);
        }}
        className={`px-2 py-0.5 rounded-full text-xs font-medium border ${
          enabled
            ? "border-emerald-700 bg-emerald-900/50 text-emerald-300 hover:bg-emerald-900"
            : "border-gray-700 bg-gray-800 text-gray-400 hover:bg-gray-700"
        }`}
        title={enabled ? "此会话已公开分享（只读）" : "分享此会话的只读链接"}
      >
        {enabled ? "Shared" : "Share"}
      </button>
      {open && (
        <div className="absolute right-0 top-7 z-40 w-80 rounded-lg border border-gray-700 bg-gray-900 p-3 text-xs text-gray-300 shadow-xl">
          <p className="mb-2 text-gray-400">
            开启后，任何拿到链接的人都可以不登录、只读地浏览这个会话的对话内容（包括工具调用、结果和图片）。工作目录、系统提示词和配置不会公开。
          </p>
          {enabled && (
            <div className="mb-2 flex gap-1">
              <input
                readOnly
                value={url}
                onFocus={(e) => e.target.select()}
                className="flex-1 min-w-0 rounded border border-gray-700 bg-gray-950 px-2 py-1 font-mono text-[11px] text-gray-200"
              />
              <button
                type="button"
                onClick={() => void copy()}
                className="shrink-0 rounded bg-gray-700 px-2 py-1 hover:bg-gray-600"
              >
                复制
              </button>
            </div>
          )}
          <div className="flex items-center justify-between gap-2">
            {enabled ? (
              <a href={url} target="_blank" rel="noreferrer" className="text-indigo-400 hover:text-indigo-300 underline">
                打开分享页
              </a>
            ) : (
              <span />
            )}
            <button
              type="button"
              disabled={busy}
              onClick={() => void toggle(!enabled)}
              className={`rounded px-3 py-1 font-medium text-white disabled:opacity-40 ${
                enabled ? "bg-red-700 hover:bg-red-600" : "bg-indigo-600 hover:bg-indigo-500"
              }`}
            >
              {busy ? "…" : enabled ? "停止分享" : "开启分享并复制链接"}
            </button>
          </div>
          {note && <p className="mt-2 text-[11px] text-emerald-400 break-words">{note}</p>}
        </div>
      )}
    </div>
  );
}
