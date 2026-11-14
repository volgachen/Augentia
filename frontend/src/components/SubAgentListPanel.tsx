import type { Session, SessionStatus } from "../api/types";

const STATUS_DOT: Record<SessionStatus, string> = {
  INITIALIZING: "bg-yellow-400",
  RUNNING: "bg-green-400",
  WAITING_USER: "bg-blue-400",
  WAITING_CONFIRM: "bg-orange-400",
  COMPLETED: "bg-gray-500",
  ERROR: "bg-red-400",
  DISPATCHING: "bg-sky-400",
};

export default function SubAgentListPanel({
  agents,
  onOpen,
}: {
  agents: Session[];
  onOpen: (id: string) => void;
}) {
  // Oldest first, so the spawn order reads top-to-bottom.
  const sorted = [...agents].sort((a, b) =>
    a.created_at.localeCompare(b.created_at),
  );

  return (
    <section className="flex-1 flex flex-col min-h-0">
      <div className="shrink-0 flex items-center justify-between px-2 pb-1.5">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-400">
          Sub-agents
        </h2>
        {sorted.length > 0 && (
          <span className="text-[10px] text-gray-500 font-mono">
            {sorted.length}
          </span>
        )}
      </div>
      {sorted.length === 0 ? (
        <p className="px-2 text-xs text-gray-600">No sub-agents.</p>
      ) : (
        <ul className="flex flex-col gap-0.5 overflow-y-auto min-h-0">
          {sorted.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                onClick={() => onOpen(s.id)}
                className="w-full flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-800/60 text-left"
                title={`Open ${s.id}`}
              >
                <span
                  className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${STATUS_DOT[s.status]}`}
                />
                <span className="font-mono text-xs text-gray-300 truncate flex-1">
                  {s.id.slice(0, 8)}…
                </span>
                <span className="text-[10px] text-gray-500 shrink-0">
                  {s.status}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
