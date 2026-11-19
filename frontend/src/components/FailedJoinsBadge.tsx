import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { FailedJoin, Session } from "../api/types";
import { useStore } from "../store/sessions";

/** "N 失败" next to a session's status: dispatched branches whose turn failed
 *  and were kept out of its context. Click to list them; "知道了" clears it. */
export default function FailedJoinsBadge({ session }: { session: Session }) {
  const failed = session.failed_joins ?? 0;
  const ackFailedJoins = useStore((s) => s.ackFailedJoins);
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<FailedJoin[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setItems(null);
    setError(null);
    api.sessions
      .failedJoins(session.id)
      .then((list) => alive && setItems(list))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [open, session.id, failed]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  if (failed <= 0) return null;

  return (
    <span ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900 text-amber-300 hover:bg-amber-800"
        title={`${failed} 个并行分支失败了，结果没有拼回这个会话。点击查看`}
      >
        {failed} 失败
      </button>
      {open && (
        <div className="absolute left-0 top-full z-30 mt-1 w-96 rounded border border-amber-900 bg-gray-900 p-2 text-xs shadow-lg">
          <div className="mb-1 text-amber-300">失败的并行分支（没有拼回这个会话的上下文）</div>
          {error && <div className="text-red-400">{error}</div>}
          {!error && items === null && <div className="text-gray-500">加载中…</div>}
          {items?.length === 0 && <div className="text-gray-500">没有未确认的失败分支</div>}
          <ul className="max-h-72 space-y-1.5 overflow-y-auto">
            {items?.map((it) => (
              <li key={it.session_id} className="rounded border border-gray-800 px-2 py-1">
                <div className="truncate text-gray-200" title={it.task}>
                  {it.task || "（无任务文本）"}
                </div>
                <div className="break-words text-amber-200/80">
                  {[it.error.type, it.error.message].filter(Boolean).join(": ") || "unknown error"}
                </div>
                <a
                  href={`/console/${it.session_id}`}
                  className="text-[11px] text-sky-400/80 hover:text-sky-300 no-underline"
                >
                  ⑂ 打开分支会话 {it.session_id.slice(0, 8)}…
                </a>
              </li>
            ))}
          </ul>
          <div className="mt-2 flex justify-end">
            <button
              type="button"
              onClick={() => {
                void ackFailedJoins(session.id)
                  .then(() => setOpen(false))
                  .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
              }}
              className="rounded border border-amber-800 px-2 py-0.5 text-amber-200 hover:bg-amber-900/50"
            >
              知道了
            </button>
          </div>
        </div>
      )}
    </span>
  );
}
