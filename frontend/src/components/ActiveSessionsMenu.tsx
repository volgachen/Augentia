import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useStore } from "../store/sessions";
import type { SessionStatus } from "../api/types";

const STATUS_STYLE: Record<SessionStatus, string> = {
  INITIALIZING: "bg-yellow-900 text-yellow-300",
  RUNNING: "bg-green-900 text-green-300",
  WAITING_USER: "bg-blue-900 text-blue-300",
  WAITING_CONFIRM: "bg-orange-900 text-orange-300",
  COMPLETED: "bg-gray-700 text-gray-400",
  ERROR: "bg-red-900 text-red-300",
  DISPATCHING: "bg-sky-900 text-sky-300",
};

// A session is "active" when it holds a live WebSocket to the backend stream.
function isSocketLive(socket: WebSocket | null): boolean {
  return (
    socket != null &&
    (socket.readyState === WebSocket.OPEN ||
      socket.readyState === WebSocket.CONNECTING)
  );
}

// Hoverable menu listing every session with a live socket, so a worker can jump
// between concurrent sessions without going back to the dashboard.
export default function ActiveSessionsMenu({
  currentSessionId,
}: {
  currentSessionId?: string;
}) {
  const sessions = useStore((s) => s.sessions);
  const setActive = useStore((s) => s.setActiveSession);
  const disconnectSession = useStore((s) => s.disconnectSession);
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);

  const active = Object.values(sessions)
    .filter((e) => isSocketLive(e.socket))
    .sort((a, b) => {
      const aUnread =
        a.attentionUnread &&
        a.session.id !== currentSessionId &&
        (a.session.status === "WAITING_USER" ||
          a.session.status === "WAITING_CONFIRM");
      const bUnread =
        b.attentionUnread &&
        b.session.id !== currentSessionId &&
        (b.session.status === "WAITING_USER" ||
          b.session.status === "WAITING_CONFIRM");
      if (aUnread !== bUnread) return Number(bUnread) - Number(aUnread);
      return a.session.created_at.localeCompare(b.session.created_at);
    });

  const unreadAttention = active.filter(
    (e) =>
      e.attentionUnread &&
      e.session.id !== currentSessionId &&
      (e.session.status === "WAITING_USER" ||
        e.session.status === "WAITING_CONFIRM"),
  );

  const jump = (id: string) => {
    setOpen(false);
    setActive(id);
    navigate(`/console/${id}`);
  };

  return (
    <div
      className="relative"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-gray-800 hover:bg-gray-700 text-xs font-medium text-gray-300 border border-gray-700"
      >
        <span className="inline-block w-1.5 h-1.5 rounded-full bg-green-400" />
        Active sessions
        <span className="text-gray-500">{active.length}</span>
        {unreadAttention.length > 0 && (
          <span className="rounded-full bg-orange-700 px-1.5 py-0.5 text-[10px] text-white">
            {unreadAttention.length} new
          </span>
        )}
        <span className="text-gray-500 text-[10px]">▾</span>
      </button>

      {open && (
        <div className="absolute right-0 top-full pt-1 w-72 z-[60]">
          <div className="rounded-lg border border-gray-700 bg-gray-900 shadow-xl py-1">
          {active.length === 0 ? (
            <p className="px-3 py-2 text-xs text-gray-500">No active sessions.</p>
          ) : (
            active.map((entry) => {
              const s = entry.session;
              const isCurrent = s.id === currentSessionId;
              const isUnreadAttention =
                entry.attentionUnread &&
                !isCurrent &&
                (s.status === "WAITING_USER" ||
                  s.status === "WAITING_CONFIRM");
              return (
                <div
                  key={s.id}
                  className={`flex items-center gap-1 px-2 py-1.5 hover:bg-gray-800 ${
                    isCurrent
                      ? "bg-gray-800/60"
                      : isUnreadAttention
                        ? "bg-orange-950/40"
                        : ""
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => jump(s.id)}
                    className="min-w-0 flex-1 flex items-center justify-between gap-2 text-left"
                  >
                    <span className="min-w-0 text-xs text-gray-300 truncate">
                      {isUnreadAttention && (
                        <span className="mr-1 text-orange-400">●</span>
                      )}
                      {s.title ?? `${s.id.slice(0, 8)}…`}
                      {isCurrent && (
                        <span className="ml-1 text-[10px] text-indigo-400">
                          current
                        </span>
                      )}
                    </span>
                    <span
                      className={`shrink-0 px-2 py-0.5 rounded-full text-[10px] font-medium ${STATUS_STYLE[s.status]}`}
                    >
                      {s.status}
                    </span>
                  </button>
                  <button
                    type="button"
                    aria-label={`Disconnect ${s.title ?? s.id}`}
                    title="Disconnect WebSocket"
                    onClick={(event) => {
                      event.stopPropagation();
                      disconnectSession(s.id);
                    }}
                    className="shrink-0 rounded px-1.5 py-0.5 text-gray-500 hover:bg-red-900/60 hover:text-red-300 text-sm leading-none"
                  >
                    ×
                  </button>
                </div>
              );
            })
          )}
          </div>
        </div>
      )}
    </div>
  );
}
