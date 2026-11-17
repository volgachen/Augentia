import { useEffect, useState } from "react";
import {
  BACKEND_ORIGIN,
  DEFAULT_PORT,
  defaultBackend,
  normalizeBackend,
  saveBackend,
  savedBackend,
} from "../api/backend";

type Probe =
  | { state: "idle" }
  | { state: "testing" }
  | { state: "ok"; detail: string }
  | { state: "fail"; detail: string };

/** Settings dialog: which backend this browser talks to. */
export default function BackendSettings({ onClose }: { onClose: () => void }) {
  const [input, setInput] = useState(savedBackend() ?? "");
  const [probe, setProbe] = useState<Probe>({ state: "idle" });
  const normalized = input.trim() ? normalizeBackend(input) : defaultBackend();
  const changed = normalized !== null && normalized !== BACKEND_ORIGIN;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const test = async () => {
    if (!normalized) return;
    setProbe({ state: "testing" });
    try {
      const res = await fetch(`${normalized}/api/v1/auth/me`, {
        credentials: "include",
        signal: AbortSignal.timeout(5000),
      });
      if (!res.ok) {
        setProbe({ state: "fail", detail: `HTTP ${res.status}` });
        return;
      }
      const body = (await res.json()) as { authenticated?: boolean; auth_enabled?: boolean };
      setProbe({
        state: "ok",
        detail: body.auth_enabled
          ? body.authenticated ? "已连接，已登录" : "已连接，需要登录"
          : "已连接（未开启登录）",
      });
    } catch (e) {
      setProbe({
        state: "fail",
        detail: `连不上：${(e as Error).message}（地址错误、后端没启动，或被 CORS 拦截）`,
      });
    }
  };

  const save = () => {
    if (!normalized) return;
    saveBackend(input.trim() ? normalized : null);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="w-full max-w-md bg-gray-900 border border-gray-700 rounded-xl shadow-2xl flex flex-col text-left">
        <div className="px-5 py-3 border-b border-gray-700 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">设置</h2>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-gray-200 text-lg leading-none"
            title="关闭"
          >
            ×
          </button>
        </div>
        <div className="px-5 py-4 flex flex-col gap-3 text-xs text-gray-300">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="backend-address" className="font-medium text-gray-200">
              后端地址
            </label>
            <input
              id="backend-address"
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                setProbe({ state: "idle" });
              }}
              onKeyDown={(e) => e.key === "Enter" && changed && save()}
              placeholder={defaultBackend()}
              autoFocus
              spellCheck={false}
              className="bg-gray-950 border border-gray-700 rounded px-2 py-1.5 font-mono text-gray-100 focus:outline-none focus:border-indigo-500"
            />
            <span className="text-gray-500">
              可以填端口（如 12597，表示当前主机）、host:port 或完整的 http(s):// 地址。
              留空使用默认：当前主机的 {DEFAULT_PORT} 端口。只保存在这个浏览器里。
            </span>
          </div>

          <div className="font-mono text-gray-400 break-all">
            当前：{BACKEND_ORIGIN}
            {normalized === null ? (
              <div className="text-red-400">无法识别这个地址</div>
            ) : (
              changed && <div className="text-indigo-300">保存后：{normalized}</div>
            )}
          </div>

          {probe.state !== "idle" && (
            <p
              className={
                probe.state === "ok"
                  ? "text-green-400"
                  : probe.state === "fail"
                    ? "text-red-400 break-all"
                    : "text-gray-400"
              }
            >
              {probe.state === "testing" ? "测试中…" : probe.detail}
            </p>
          )}
        </div>
        <div className="px-5 py-3 border-t border-gray-700 flex items-center gap-2">
          <button
            onClick={() => void test()}
            disabled={!normalized || probe.state === "testing"}
            className="px-3 py-1.5 rounded text-xs bg-gray-800 hover:bg-gray-700 text-gray-300 disabled:opacity-40"
          >
            测试连接
          </button>
          <div className="ml-auto flex gap-2">
            <button
              onClick={onClose}
              className="px-3 py-1.5 rounded text-xs bg-gray-800 hover:bg-gray-700 text-gray-300"
            >
              取消
            </button>
            <button
              onClick={save}
              disabled={!changed}
              title="保存后页面会重新加载"
              className="px-3 py-1.5 rounded text-xs bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-40"
            >
              保存并重新加载
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
