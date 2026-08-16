import { useState } from "react";
import type { Session, SessionStatus } from "../api/types";

const STATUS_DOT: Record<SessionStatus, string> = {
  INITIALIZING: "bg-yellow-400",
  RUNNING: "bg-green-400",
  WAITING_USER: "bg-blue-400",
  WAITING_CONFIRM: "bg-orange-400",
  COMPLETED: "bg-gray-500",
  ERROR: "bg-red-400",
};

function isSocketLive(socket: WebSocket | null): boolean {
  return (
    socket != null &&
    (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)
  );
}

export default function SessionListPanel({
  sessions,
  currentSessionId,
  onOpen,
}: {
  sessions: Array<{ session: Session; socket: WebSocket | null }>;
  currentSessionId?: string;
  onOpen: (id: string) => void;
}) {
  const [showActiveOnly, setShowActiveOnly] = useState(true);
  const visible = sessions
    .filter(({ socket }) => !showActiveOnly || isSocketLive(socket))
    .sort((a, b) => b.session.created_at.localeCompare(a.session.created_at));

  return (
    <section className="flex-1 flex flex-col min-h-0">
      <div className="shrink-0 flex items-center justify-between px-2 pb-1.5">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-400">
          Sessions
        </h2>
        <span className="text-[10px] text-gray-500 font-mono">{visible.length}</span>
      </div>
      <ul className="flex-1 min-h-0 overflow-y-auto space-y-0.5">
        {visible.length === 0 ? (
          <li className="px-2 text-xs text-gray-600">
            {showActiveOnly ? "No active sessions." : "No sessions."}
          </li>
        ) : (
          visible.map(({ session }) => {
            const current = session.id === currentSessionId;
            return (
              <li key={session.id}>
                <button
                  type="button"
                  onClick={() => onOpen(session.id)}
                  className={`w-full flex items-center gap-2 rounded px-2 py-1.5 text-left hover:bg-gray-800/60 ${
                    current ? "bg-gray-800/60" : ""
                  }`}
                  title={`Open ${session.id}`}
                >
                  <span
                    className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${STATUS_DOT[session.status]}`}
                  />
                  <span className="min-w-0 flex-1 truncate text-xs text-gray-300">
                    {session.title?.trim() || `${session.id.slice(0, 8)}…`}
                  </span>
                  <span className="shrink-0 text-[10px] text-gray-500">
                    {session.status}
                  </span>
                </button>
              </li>
            );
          })
        )}
      </ul>
      <label className="mt-2 flex shrink-0 cursor-pointer items-center justify-between rounded-lg border border-gray-800 bg-gray-950/60 px-2.5 py-2">
        <span className="text-xs text-gray-300">只显示 active sessions</span>
        <input
          type="checkbox"
          className="h-4 w-4 accent-indigo-500"
          checked={showActiveOnly}
          onChange={(event) => setShowActiveOnly(event.target.checked)}
        />
      </label>
    </section>
  );
}
