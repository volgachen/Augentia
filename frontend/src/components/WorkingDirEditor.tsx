import { useEffect, useState } from "react";
import type { Session } from "../api/types";
import { useStore } from "../store/sessions";
import PathList from "./PathList";
import { useBrowser } from "./useDirBrowser";

const BUSY_STATUSES = new Set(["RUNNING", "WAITING_CONFIRM"]);

/** Header cwd display that opens a dialog to move the session to another directory. */
export default function WorkingDirEditor({ session }: { session: Session }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState<{ scope: string; text: string } | null>(null);
  const busy = BUSY_STATUSES.has(session.status);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        disabled={busy}
        className="group text-left text-xs text-gray-500 font-mono hover:text-gray-300 disabled:hover:text-gray-500 disabled:cursor-not-allowed"
        title={busy ? "当前轮结束后才能修改工作目录" : "修改工作目录"}
      >
        <span className="text-gray-600">cwd: </span>
        {session.working_dir ?? "—"}
        {!busy && <span className="ml-1 text-gray-600 group-hover:text-indigo-300">✎</span>}
      </button>
      {note?.scope === session.id && <p className="text-xs text-emerald-400">{note.text}</p>}
      {open && (
        <WorkingDirDialog
          session={session}
          onClose={() => setOpen(false)}
          onDone={(text) => {
            setOpen(false);
            setNote({ scope: session.id, text });
            window.setTimeout(() => setNote((n) => (n?.text === text ? null : n)), 4000);
          }}
        />
      )}
    </>
  );
}

function WorkingDirDialog({
  session,
  onClose,
  onDone,
}: {
  session: Session;
  onClose: () => void;
  onDone: (note: string) => void;
}) {
  const updateWorkingDir = useStore((s) => s.updateWorkingDir);
  const browser = useBrowser(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [typed, setTyped] = useState(session.working_dir ?? "");
  const [updatePrompt, setUpdatePrompt] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busy = BUSY_STATUSES.has(session.status);
  const { load } = browser;

  // Start browsing at the current directory (falls back to the default root).
  useEffect(() => {
    load(session.working_dir ?? null);
  }, [load, session.working_dir]);

  const target = typed.trim();
  const unchanged = target === (session.working_dir ?? "");

  const pick = (path: string) => {
    setSelected(path);
    setTyped(path);
  };

  const save = async () => {
    if (!target || unchanged || busy) return;
    setSaving(true);
    setError(null);
    try {
      const r = await updateWorkingDir(session.id, target, updatePrompt);
      onDone(
        updatePrompt && !r.prompt_updated
          ? "工作目录已修改（系统提示词中未找到对应的工作目录行，未改动）"
          : "工作目录已修改",
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-2xl max-h-[90vh] bg-gray-900 border border-gray-700 rounded-xl shadow-2xl flex flex-col">
        <div className="px-5 py-3 border-b border-gray-700 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">修改工作目录</h2>
          <button
            onClick={onClose}
            disabled={saving}
            className="text-gray-500 hover:text-gray-200 disabled:opacity-40 text-lg leading-none"
            title="取消"
          >
            ×
          </button>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4 flex flex-col gap-3 text-xs text-gray-300">
          <p className="text-gray-400">
            修改后会话的运行实例会重建，之后的工具调用都在新目录下执行；对话历史保留。bash 里之前
            <span className="font-mono"> cd </span>
            到的位置和 Node REPL 进程会被重置。只能在两轮之间修改。
          </p>
          <p className="font-mono text-gray-500 break-all">
            当前：{session.working_dir ?? "—（未设置，使用后端进程目录）"}
          </p>
          <PathList
            data={browser.data}
            loading={browser.loading}
            error={browser.error}
            selected={selected}
            onSelect={pick}
            onEnter={(p) => {
              pick(p);
              load(p);
            }}
            onGoParent={() => browser.data?.parent && load(browser.data.parent)}
          />
          <label className="flex flex-col gap-1">
            <span className="text-gray-400">新目录（可直接输入路径，单击上方文件夹选择，双击进入）</span>
            <input
              value={typed}
              onChange={(e) => {
                setTyped(e.target.value);
                setSelected(null);
              }}
              onKeyDown={(e) => e.key === "Enter" && save()}
              className="bg-gray-950 border border-gray-700 rounded px-2 py-1.5 font-mono text-gray-100 focus:outline-none focus:border-indigo-500"
              spellCheck={false}
            />
          </label>
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={updatePrompt}
              onChange={(e) => setUpdatePrompt(e.target.checked)}
              className="mt-0.5"
            />
            <span>
              同时更新系统提示词中的 “Current working directory” 一行（保存为新版本）。
              <span className="text-gray-500">如果这一行被手动改过、与当前目录不一致，则不会改动。</span>
            </span>
          </label>
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
            disabled={!target || unchanged || busy || saving}
            className="px-3 py-1.5 rounded text-xs bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-40"
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}
