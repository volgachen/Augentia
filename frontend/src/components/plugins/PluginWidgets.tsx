import { useState } from "react";
import { api } from "../../api/client";
import { errorText } from "../../api/errors";
import type { PluginLogEntry, PluginUIAction, PluginUISection, PluginUIState, ServiceState } from "../../api/types";

// Shared plugin widgets: the UI card a plugin pushes (ctx.ui.update), a
// service row with start / stop / restart, and a log list.

const UI_STYLE: Record<PluginUIState["status"], string> = {
  idle: "border-gray-700 bg-gray-900/60",
  waiting: "border-amber-700 bg-amber-950/30",
  ok: "border-green-800 bg-green-950/30",
  error: "border-red-800 bg-red-950/40",
};

const DOT_STYLE: Record<PluginUIState["status"], string> = {
  idle: "bg-gray-500",
  waiting: "bg-amber-400",
  ok: "bg-green-500",
  error: "bg-red-500",
};

const BUTTON_STYLE = {
  default: "border border-gray-600 bg-gray-800 text-gray-200 hover:border-gray-400",
  primary: "bg-indigo-600 text-white hover:bg-indigo-500",
  danger: "bg-red-700 text-white hover:bg-red-600",
};

// Buttons are told apart by id + data (several sections may show the same id).
const actionKey = (a: PluginUIAction) => `${a.id}:${JSON.stringify(a.data ?? {})}`;

function CardBody({
  block,
  busy,
  run,
}: {
  block: Pick<PluginUISection, "message" | "qr" | "link" | "actions">;
  busy: string | null;
  run: (a: PluginUIAction, text?: string) => void;
}) {
  const plain = block.actions.filter((a) => !a.input);
  const withInput = block.actions.filter((a) => a.input);
  return (
    <>
      {block.message && <div className="text-gray-200 whitespace-pre-wrap break-words">{block.message}</div>}
      {block.qr && (
        // No QR library yet (README §10.4): show the QR payload as a link.
        <a href={block.qr} target="_blank" rel="noreferrer" className="text-indigo-300 underline break-all">
          Open QR code
        </a>
      )}
      {block.link && (
        <a href={block.link} target="_blank" rel="noreferrer" className="text-indigo-300 underline break-all">
          {block.link}
        </a>
      )}
      {withInput.map((a) => (
        <InputAction key={`${actionKey(a)}:${a.input?.value ?? ""}`} action={a} busy={busy} run={run} />
      ))}
      {plain.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {plain.map((a) => <ActionButton key={actionKey(a)} action={a} busy={busy} run={run} />)}
        </div>
      )}
    </>
  );
}

function ActionButton({
  action: a,
  busy,
  run,
}: {
  action: PluginUIAction;
  busy: string | null;
  run: (a: PluginUIAction, text?: string) => void;
}) {
  if (a.disabled) {
    return <span className="px-1 py-0.5 text-[11px] text-gray-500 whitespace-nowrap">{a.label}</span>;
  }
  return (
    <button
      type="button"
      disabled={busy !== null}
      onClick={() => run(a)}
      className={`rounded px-2 py-0.5 text-[11px] whitespace-nowrap disabled:opacity-50 ${BUTTON_STYLE[a.style ?? "default"]}`}
    >
      {busy === actionKey(a) ? "…" : a.label}
    </button>
  );
}

// One section as a compact row: status dot, title, message, buttons; a QR
// code or link (and input buttons) go on the lines below.
function SectionRow({
  section: s,
  busy,
  run,
}: {
  section: PluginUISection;
  busy: string | null;
  run: (a: PluginUIAction, text?: string) => void;
}) {
  const plain = s.actions.filter((a) => !a.input);
  const withInput = s.actions.filter((a) => a.input);
  return (
    <div className="flex flex-col gap-1 py-1">
      <div className="flex items-center gap-1.5 min-w-0">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT_STYLE[s.status]}`} title={s.status} />
        {s.title && <span className="font-semibold text-gray-100 truncate">{s.title}</span>}
        {s.message && <span className="text-gray-400 truncate" title={s.message}>{s.message}</span>}
        <span className="ml-auto flex shrink-0 gap-1">
          {plain.map((a) => <ActionButton key={actionKey(a)} action={a} busy={busy} run={run} />)}
        </span>
      </div>
      {s.qr && (
        <a href={s.qr} target="_blank" rel="noreferrer" className="pl-3 text-indigo-300 underline break-all">
          Open QR code
        </a>
      )}
      {s.link && (
        <a href={s.link} target="_blank" rel="noreferrer" className="pl-3 text-indigo-300 underline break-all">
          {s.link}
        </a>
      )}
      {withInput.map((a) => (
        <InputAction key={`${actionKey(a)}:${a.input?.value ?? ""}`} action={a} busy={busy} run={run} />
      ))}
    </div>
  );
}

function InputAction({
  action,
  busy,
  run,
}: {
  action: PluginUIAction;
  busy: string | null;
  run: (a: PluginUIAction, text?: string) => void;
}) {
  const [text, setText] = useState(action.input?.value ?? "");
  const submit = () => run(action, text);
  return (
    <form
      className="flex gap-1.5"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={action.input?.placeholder}
        className="flex-1 min-w-0 rounded border border-gray-600 bg-gray-950 px-1.5 py-0.5 text-[11px] text-gray-100"
      />
      <button
        type="submit"
        disabled={busy !== null}
        className={`rounded px-2 py-0.5 text-[11px] disabled:opacity-50 ${BUTTON_STYLE[action.style ?? "default"]}`}
      >
        {busy === actionKey(action) ? "…" : action.label}
      </button>
    </form>
  );
}

export function PluginUICard({
  pluginId,
  sessionId,
  ui,
}: {
  pluginId: string;
  sessionId: string | null;
  ui: PluginUIState;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (a: PluginUIAction, text?: string) => {
    setBusy(actionKey(a));
    setError(null);
    const data: Record<string, unknown> = { ...(a.data ?? {}) };
    if (a.input) data[a.input.name] = text ?? "";
    try {
      await api.plugins.action(pluginId, a.id, sessionId, data);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };
  const runVoid = (a: PluginUIAction, text?: string) => void run(a, text);

  return (
    <div className={`rounded border px-2 py-1.5 flex flex-col gap-1.5 text-[11px] ${UI_STYLE[ui.status]}`}>
      <CardBody block={ui} busy={busy} run={runVoid} />
      {(ui.sections?.length ?? 0) > 0 && (
        <div className="flex flex-col divide-y divide-gray-800">
          {ui.sections!.map((s, i) => (
            <SectionRow key={`${s.title ?? ""}-${i}`} section={s} busy={busy} run={runVoid} />
          ))}
        </div>
      )}
      {error && <div className="text-red-300">{error}</div>}
    </div>
  );
}

const SERVICE_STYLE: Record<ServiceState["status"], string> = {
  stopped: "bg-gray-700 text-gray-300",
  starting: "bg-yellow-900 text-yellow-200",
  running: "bg-green-900 text-green-300",
  stopping: "bg-yellow-900 text-yellow-200",
  exited: "bg-blue-900 text-blue-300",
  errored: "bg-red-900 text-red-300",
  backoff: "bg-orange-900 text-orange-200",
};

export function ServiceStatusBadge({ status }: { status: ServiceState["status"] }) {
  return <span className={`text-[10px] px-1.5 py-0.5 rounded ${SERVICE_STYLE[status]}`}>{status}</span>;
}

export function ServiceRow({
  pluginId,
  name,
  sessionId,
  state,
  onLogs,
}: {
  pluginId: string;
  name: string;
  sessionId: string | null;
  state: ServiceState | undefined;
  onLogs?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = state && ["starting", "running", "stopping", "backoff"].includes(state.status);

  const op = async (kind: "start" | "stop" | "restart") => {
    setBusy(true);
    setError(null);
    try {
      await api.plugins.service(pluginId, name, kind, sessionId);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-0.5">
      <div className="flex items-center gap-2 text-[11px]">
        <span className="font-mono text-gray-300">{name}</span>
        <ServiceStatusBadge status={state?.status ?? "stopped"} />
        {state && state.restart_count > 0 && (
          <span className="text-gray-500" title="automatic restarts">↻{state.restart_count}</span>
        )}
        <span className="ml-auto flex gap-1">
          {!active && (
            <button type="button" disabled={busy} onClick={() => void op("start")}
                    className="text-gray-400 hover:text-gray-100 disabled:opacity-50">Start</button>
          )}
          {active && (
            <>
              <button type="button" disabled={busy} onClick={() => void op("restart")}
                      className="text-gray-400 hover:text-gray-100 disabled:opacity-50">Restart</button>
              <button type="button" disabled={busy} onClick={() => void op("stop")}
                      className="text-gray-400 hover:text-red-300 disabled:opacity-50">Stop</button>
            </>
          )}
          {onLogs && (
            <button type="button" onClick={onLogs} className="text-gray-400 hover:text-gray-100">Logs</button>
          )}
        </span>
      </div>
      {state?.error && <div className="text-[11px] text-red-300 break-words">{state.error}</div>}
      {state?.status === "backoff" && state.next_restart_at && (
        <div className="text-[11px] text-orange-300">
          restarting at {new Date(state.next_restart_at).toLocaleTimeString()}
        </div>
      )}
      {error && <div className="text-[11px] text-red-300">{error}</div>}
    </div>
  );
}

const LEVEL_STYLE: Record<PluginLogEntry["level"], string> = {
  debug: "text-gray-500",
  info: "text-gray-200",
  warning: "text-amber-300",
  error: "text-red-300",
};

export function LogList({ lines, empty = "No log lines." }: { lines: PluginLogEntry[]; empty?: string }) {
  if (lines.length === 0) return <p className="text-[11px] text-gray-600">{empty}</p>;
  return (
    <div className="font-mono text-[11px] leading-4 flex flex-col">
      {lines.map((l, i) => (
        <div key={l.id ?? `${l.ts}-${i}`} className={`whitespace-pre-wrap break-words ${LEVEL_STYLE[l.level]}`}>
          <span className="text-gray-600">{new Date(l.ts).toLocaleTimeString()} </span>
          {l.line}
        </div>
      ))}
    </div>
  );
}
