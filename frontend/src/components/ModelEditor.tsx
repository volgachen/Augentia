import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Session, SessionModelView } from "../api/types";
import { useStore } from "../store/sessions";

const BUSY_STATUSES = new Set(["RUNNING", "WAITING_CONFIRM"]);

function pretty(value: unknown): string {
  return JSON.stringify(value ?? {}, null, 2);
}

/** Header model display that opens a dialog to give this session its own model. */
export default function ModelEditor({ session }: { session: Session }) {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<{ scope: string; data: SessionModelView } | null>(null);
  const busy = BUSY_STATUSES.has(session.status);

  // Re-read when the session's own values change (also after saving here).
  const paramsKey = JSON.stringify(session.model_params ?? null);
  useEffect(() => {
    let cancelled = false;
    api.sessions
      .model(session.id)
      .then((data) => !cancelled && setView({ scope: session.id, data }))
      .catch(() => !cancelled && setView(null));
    return () => {
      cancelled = true;
    };
  }, [session.id, session.model, paramsKey]);

  const data = view?.scope === session.id ? view.data : null;
  const overridden = session.model != null || session.model_params != null;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        disabled={busy || !data}
        className="group block text-left text-xs text-gray-500 font-mono hover:text-gray-300 disabled:hover:text-gray-500 disabled:cursor-not-allowed"
        title={busy ? "当前轮结束后才能修改模型" : "修改这个会话使用的模型"}
      >
        <span className="text-gray-600">model: </span>
        {data?.effective_model ?? session.model ?? "…"}
        {overridden && <span className="ml-1 text-amber-400/80">（会话设置）</span>}
        {!busy && data && <span className="ml-1 text-gray-600 group-hover:text-indigo-300">✎</span>}
      </button>
      {open && data && (
        <ModelDialog session={session} view={data} onClose={() => setOpen(false)} />
      )}
    </>
  );
}

function ModelDialog({
  session,
  view,
  onClose,
}: {
  session: Session;
  view: SessionModelView;
  onClose: () => void;
}) {
  const updateModel = useStore((s) => s.updateModel);
  const [useOwnModel, setUseOwnModel] = useState(view.model != null);
  const [model, setModel] = useState(view.model ?? view.agent_model);
  const [useOwnParams, setUseOwnParams] = useState(view.model_params != null);
  const [paramsText, setParamsText] = useState(
    pretty(view.model_params ?? view.agent_model_params),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busy = BUSY_STATUSES.has(session.status);

  const save = async () => {
    let params: Record<string, unknown> | null = null;
    if (useOwnParams) {
      try {
        const parsed = JSON.parse(paramsText.trim() || "{}") as unknown;
        if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("model_params 必须是 JSON 对象");
        }
        params = parsed as Record<string, unknown>;
      } catch (e) {
        setError(e instanceof SyntaxError ? `JSON 格式错误：${e.message}` : (e as Error).message);
        return;
      }
    }
    const name = useOwnModel ? model.trim() : "";
    if (useOwnModel && !name) {
      setError("模型名不能为空");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await updateModel(session.id, useOwnModel ? name : null, params);
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-xl max-h-[90vh] bg-gray-900 border border-gray-700 rounded-xl shadow-2xl flex flex-col">
        <div className="px-5 py-3 border-b border-gray-700 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">修改会话模型</h2>
          <button
            onClick={onClose}
            disabled={saving}
            className="text-gray-500 hover:text-gray-200 disabled:opacity-40 text-lg leading-none"
            title="取消"
          >
            ×
          </button>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4 flex flex-col gap-4 text-xs text-gray-300">
          <p className="text-gray-400">
            只影响这个会话，agent 模板不变。保存后从下一轮开始生效，对话历史保留；每条回复的 metadata 里记录了实际使用的模型。只能在两轮之间修改。
          </p>

          <div className="flex flex-col gap-1.5">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={useOwnModel}
                onChange={(e) => setUseOwnModel(e.target.checked)}
              />
              <span>使用会话自己的模型</span>
            </label>
            <input
              value={useOwnModel ? model : view.agent_model}
              onChange={(e) => setModel(e.target.value)}
              disabled={!useOwnModel}
              spellCheck={false}
              className="bg-gray-950 border border-gray-700 rounded px-2 py-1.5 font-mono text-gray-100 focus:outline-none focus:border-indigo-500 disabled:text-gray-500"
            />
            {!useOwnModel && (
              <span className="text-gray-500">使用 agent 的模型：{view.agent_model}</span>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={useOwnParams}
                onChange={(e) => setUseOwnParams(e.target.checked)}
              />
              <span>使用会话自己的 model_params</span>
            </label>
            <textarea
              value={useOwnParams ? paramsText : pretty(view.agent_model_params)}
              onChange={(e) => setParamsText(e.target.value)}
              disabled={!useOwnParams}
              rows={6}
              spellCheck={false}
              className="bg-gray-950 border border-gray-700 rounded px-2 py-1.5 font-mono text-gray-100 focus:outline-none focus:border-indigo-500 disabled:text-gray-500"
            />
            <span className="text-gray-500">
              {useOwnParams
                ? "整个替换 agent 的 model_params（不合并），{} 表示不带额外参数。网关特有字段放在 extra_body 里。"
                : "使用 agent 的 model_params。"}
            </span>
          </div>

          {busy && <p className="text-orange-300">会话正在运行，请等这一轮结束后再修改。</p>}
          {error && <p className="text-red-400 font-mono break-all">{error}</p>}
        </div>
        <div className="px-5 py-3 border-t border-gray-700 flex justify-end gap-2">
          <button
            onClick={onClose}
            disabled={saving}
            className="px-3 py-1.5 rounded text-xs bg-gray-800 hover:bg-gray-700 text-gray-300 disabled:opacity-40"
          >
            取消
          </button>
          <button
            onClick={save}
            disabled={busy || saving}
            className="px-3 py-1.5 rounded text-xs bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-40"
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}
