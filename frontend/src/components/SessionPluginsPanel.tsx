import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { PluginUIState, Session, SessionPluginInfo } from "../api/types";
import SchemaForm from "./SchemaForm";
import { serviceKey, uiKey, usePluginHostStore } from "../store/pluginHost";
import { LogList, PluginUICard, ServiceRow } from "./plugins/PluginWidgets";

// Per-session plugin switches and config (backend/app/plugins/README.md §10.2).
// Each card saves through PUT /sessions/{id}/plugins/{plugin_id}, which only
// touches that plugin's entry. The plugin's UI card, this session's services
// and its recent logs stay live through the shared plugin stream.

function PluginCard({
  sessionId,
  plugin,
  onSaved,
}: {
  sessionId: string;
  plugin: SessionPluginInfo;
  onSaved: (p: SessionPluginInfo) => void;
}) {
  const [draft, setDraft] = useState<Record<string, unknown>>(plugin.config);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [open, setOpen] = useState(false);

  const save = async (body: { enabled?: boolean; config?: Record<string, unknown> }) => {
    setBusy(true);
    setErrors([]);
    try {
      const updated = await api.sessions.updatePlugin(sessionId, plugin.id, body);
      setDraft(updated.config);
      setDirty(false);
      onSaved(updated);
    } catch (e) {
      setErrors(parseErrors((e as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const unavailable = plugin.state !== "loaded";
  const capabilities = [
    plugin.hooks.length > 0 && `hooks: ${plugin.hooks.join(", ")}`,
    plugin.session_services.length > 0 && `services: ${plugin.session_services.join(", ")}`,
    plugin.tools.length > 0 && `${plugin.tools.length} tool(s), enabled per agent`,
  ].filter(Boolean);

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-950/60 p-2 flex flex-col gap-2">
      <div className="flex items-start gap-2">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={plugin.enabled}
          disabled={busy}
          onChange={(e) => save({ enabled: e.target.checked })}
          title={plugin.enabled ? "Disable for this session" : "Enable for this session"}
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-gray-200 truncate">{plugin.name}</span>
            <span className="text-[10px] font-mono text-gray-500">{plugin.id}</span>
            {unavailable && (
              <span
                className="text-[10px] px-1 rounded bg-amber-900/60 text-amber-300"
                title={plugin.error ?? undefined}
              >
                {plugin.state}
              </span>
            )}
          </div>
          {plugin.description && (
            <div className="text-[11px] text-gray-500 line-clamp-2">{plugin.description}</div>
          )}
          {capabilities.length > 0 && (
            <div className="text-[10px] text-gray-600">{capabilities.join(" · ")}</div>
          )}
          {unavailable && plugin.error && (
            <div className="text-[11px] text-amber-400 break-words">{plugin.error}</div>
          )}
        </div>
        {plugin.schema && (
          <button
            type="button"
            className="text-[11px] text-gray-400 hover:text-gray-200"
            onClick={() => setOpen((o) => !o)}
          >
            {open ? "Hide" : "Settings"}
          </button>
        )}
      </div>

      <LiveSection sessionId={sessionId} plugin={plugin} />

      {plugin.config_errors.length > 0 && (
        <div className="rounded border border-amber-800 bg-amber-950/40 px-2 py-1 text-[11px] text-amber-200">
          Saved settings no longer match this plugin's schema:
          <ul className="list-disc pl-4">
            {plugin.config_errors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </div>
      )}

      {open && plugin.schema && (
        <div className="flex flex-col gap-2 border-t border-gray-800 pt-2">
          <SchemaForm
            schema={plugin.schema}
            value={draft}
            onChange={(v) => {
              setDraft(v);
              setDirty(true);
            }}
          />
          <div className="flex justify-end gap-2">
            <button
              type="button"
              disabled={!dirty || busy}
              onClick={() => {
                setDraft(plugin.config);
                setDirty(false);
                setErrors([]);
              }}
              className="rounded border border-gray-700 bg-gray-900 px-2.5 py-1 text-xs text-gray-300 hover:border-gray-500 disabled:opacity-50"
            >
              Reset
            </button>
            <button
              type="button"
              disabled={!dirty || busy}
              onClick={() => save({ config: draft })}
              className="rounded bg-indigo-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              {busy ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      )}

      {errors.length > 0 && (
        <div className="rounded border border-red-800 bg-red-950/50 px-2 py-1 text-[11px] text-red-200">
          {errors.map((e) => (
            <div key={e}>{e}</div>
          ))}
        </div>
      )}
    </div>
  );
}

// request() throws Error("<status> <body>"); a 400 body is
// {"detail": {"message", "errors": [...]}}.
function parseErrors(message: string): string[] {
  const body = message.replace(/^\d+\s/, "");
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    const detail = parsed.detail;
    if (detail && typeof detail === "object" && Array.isArray((detail as { errors?: unknown }).errors)) {
      return (detail as { errors: string[] }).errors;
    }
    if (typeof detail === "string") return [detail];
  } catch {
    // not JSON
  }
  return [message];
}

// An idle card with nothing to show (e.g. after ctx.ui.clear()) is hidden.
const hasContent = (ui: PluginUIState) =>
  ui.status !== "idle" || !!ui.message || !!ui.link || !!ui.qr || ui.actions.length > 0;

function LiveSection({ sessionId, plugin }: { sessionId: string; plugin: SessionPluginInfo }) {
  // Live values win; the GET response covers the time before the stream's snapshot.
  const ui = usePluginHostStore((s) => s.ui[uiKey(plugin.id, sessionId)]) ?? plugin.ui ?? undefined;
  const services = usePluginHostStore((s) => s.services);
  const allLogs = usePluginHostStore((s) => s.logs);
  const [showLogs, setShowLogs] = useState(false);
  if (plugin.state !== "loaded") return null;
  const logs = showLogs
    ? allLogs.filter((l) => l.plugin_id === plugin.id && l.session_id === sessionId).slice(-200)
    : [];
  return (
    <>
      {ui && hasContent(ui) && <PluginUICard pluginId={plugin.id} sessionId={sessionId} ui={ui} />}
      {plugin.session_services.length > 0 && (
        <div className="flex flex-col gap-1">
          {plugin.session_services.map((name) => (
            <ServiceRow
              key={name}
              pluginId={plugin.id}
              name={name}
              sessionId={sessionId}
              state={services[serviceKey({ plugin_id: plugin.id, service: name, session_id: sessionId })]
                ?? plugin.services.find((sv) => sv.service === name)}
              onLogs={() => setShowLogs((v) => !v)}
            />
          ))}
        </div>
      )}
      {showLogs && (
        <div className="max-h-40 overflow-y-auto rounded border border-gray-800 bg-gray-950 p-1.5">
          <LogList lines={logs} empty="No log lines for this session yet." />
        </div>
      )}
    </>
  );
}

export default function SessionPluginsPanel({ session }: { session: Session }) {
  const connect = usePluginHostStore((s) => s.connect);
  const disconnect = usePluginHostStore((s) => s.disconnect);
  useEffect(() => {
    connect();
    return disconnect;
  }, [connect, disconnect]);
  const [plugins, setPlugins] = useState<SessionPluginInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchPlugins = useCallback(
    () =>
      api.sessions.plugins(session.id).then(
        (list) => {
          setPlugins(list);
          setError(null);
        },
        (e) => setError((e as Error).message),
      ),
    [session.id],
  );

  // The parent re-keys this panel per session, so state starts fresh here.
  useEffect(() => {
    void fetchPlugins();
  }, [fetchPlugins]);

  return (
    <section className="flex-1 flex flex-col min-h-0">
      <div className="shrink-0 flex items-center justify-between px-2 pb-1.5">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-400">Plugins</h2>
        <button type="button" onClick={() => void fetchPlugins()} className="text-[11px] text-gray-500 hover:text-gray-300">
          Refresh
        </button>
      </div>
      <div className="px-2 pb-2 text-[11px] leading-4 text-gray-500">
        Enabling a plugin turns on its hooks and services for this session. Plugin tools are
        chosen per agent and work regardless of this switch.
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto flex flex-col gap-2">
        {error && (
          <div className="rounded border border-red-800 bg-red-950/50 px-2 py-1.5 text-[11px] text-red-200">
            {error}
          </div>
        )}
        {plugins === null && !error && <p className="px-2 text-xs text-gray-600">Loading plugins…</p>}
        {plugins?.length === 0 && (
          <p className="px-2 text-xs text-gray-600">No plugin has per-session settings.</p>
        )}
        {plugins?.map((p) => (
          <PluginCard
            key={p.id}
            sessionId={session.id}
            plugin={p}
            onSaved={(updated) =>
              setPlugins((list) => list?.map((x) => (x.id === updated.id ? updated : x)) ?? null)
            }
          />
        ))}
      </div>
    </section>
  );
}
