import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { errorText } from "../api/errors";
import type { PluginInfo, PluginServiceRun } from "../api/types";
import SchemaForm from "../components/SchemaForm";
import { LogList, PluginUICard, ServiceRow } from "../components/plugins/PluginWidgets";
import { serviceKey, uiKey, usePluginHostStore } from "../store/pluginHost";

// Plugin management page (backend/app/plugins/README.md §10.3): every
// installed plugin with its state, global switch, global config, services,
// UI card and logs. Live through WS /plugins/stream.

const STATE_STYLE: Record<PluginInfo["state"], string> = {
  loaded: "bg-green-900 text-green-300",
  disabled: "bg-gray-700 text-gray-300",
  error: "bg-red-900 text-red-300",
};

function capabilitySummary(p: PluginInfo): string[] {
  const caps = p.capabilities;
  if (!caps) return [];
  const parts: string[] = [];
  if (caps.tools.length) parts.push(`${caps.tools.length} tool${caps.tools.length > 1 ? "s" : ""}`);
  if (caps.hooks.length) parts.push(`hooks: ${[...new Set(caps.hooks.map((h) => h.event))].join(", ")}`);
  if (caps.services.length) {
    parts.push(`services: ${caps.services.map((s) => `${s.name}${s.scope === "session" ? " (per session)" : ""}`).join(", ")}`);
  }
  if (caps.actions.length) parts.push(`${caps.actions.length} action${caps.actions.length > 1 ? "s" : ""}`);
  return parts;
}

function GlobalConfig({ plugin }: { plugin: PluginInfo }) {
  const [value, setValue] = useState<Record<string, unknown> | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    api.plugins.globalConfig(plugin.id).then(
      (v) => alive && setValue(v),
      (e) => alive && setMessage({ ok: false, text: errorText(e) }),
    );
    return () => {
      alive = false;
    };
  }, [plugin.id]);

  if (!plugin.global_config_schema) return null;
  if (value === null) return <p className="text-xs text-gray-600">Loading settings…</p>;

  const save = async () => {
    setBusy(true);
    setMessage(null);
    try {
      setValue(await api.plugins.setGlobalConfig(plugin.id, value));
      setDirty(false);
      setMessage({ ok: true, text: "Saved. Global services were restarted to pick it up." });
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-2">
      <SchemaForm
        schema={plugin.global_config_schema}
        value={value}
        onChange={(v) => {
          setValue(v);
          setDirty(true);
          setMessage(null);
        }}
      />
      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={!dirty || busy}
          onClick={() => void save()}
          className="rounded bg-indigo-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save settings"}
        </button>
        {message && (
          <span className={`text-[11px] ${message.ok ? "text-green-300" : "text-red-300"}`}>{message.text}</span>
        )}
      </div>
    </div>
  );
}

function RunHistory({ pluginId }: { pluginId: string }) {
  const [runs, setRuns] = useState<PluginServiceRun[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [lines, setLines] = useState<Awaited<ReturnType<typeof api.plugins.logs>> | null>(null);

  useEffect(() => {
    let alive = true;
    api.plugins.serviceRuns(pluginId, { limit: 50 }).then((r) => alive && setRuns(r), () => alive && setRuns([]));
    return () => {
      alive = false;
    };
  }, [pluginId]);

  const open = (runId: string) => {
    setSelected(runId);
    setLines(null);
    api.plugins.logs(pluginId, { runId, limit: 1000 }).then(setLines, () => setLines([]));
  };

  if (runs === null) return <p className="text-xs text-gray-600">Loading runs…</p>;
  if (runs.length === 0) return <p className="text-xs text-gray-600">No service runs yet.</p>;
  return (
    <div className="flex flex-col gap-2">
      <table className="text-[11px] text-gray-300">
        <thead className="text-gray-500">
          <tr>
            <th className="text-left font-normal">Service</th>
            <th className="text-left font-normal">Session</th>
            <th className="text-left font-normal">Started</th>
            <th className="text-left font-normal">Status</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} className={selected === r.id ? "bg-gray-800" : ""}>
              <td className="font-mono pr-2">{r.service}</td>
              <td className="font-mono pr-2">{r.session_id ? r.session_id.slice(0, 8) : "—"}</td>
              <td className="pr-2">{new Date(r.started_at).toLocaleString()}</td>
              <td className="pr-2" title={r.error ?? undefined}>
                {r.status}
                {r.restart_count > 0 ? ` (restart ${r.restart_count})` : ""}
              </td>
              <td>
                <button type="button" className="text-indigo-300 hover:underline" onClick={() => open(r.id)}>
                  logs
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {selected && (
        <div className="max-h-64 overflow-y-auto rounded border border-gray-800 bg-gray-950 p-2">
          {lines === null ? <p className="text-[11px] text-gray-600">Loading…</p> : <LogList lines={lines} />}
        </div>
      )}
    </div>
  );
}

function PluginPanel({ plugin }: { plugin: PluginInfo }) {
  const services = usePluginHostStore((s) => s.services);
  const globalUi = usePluginHostStore((s) => s.ui[uiKey(plugin.id, null)]);
  const allLogs = usePluginHostStore((s) => s.logs);
  const applyPlugin = usePluginHostStore((s) => s.applyPlugin);
  const [tab, setTab] = useState<"overview" | "settings" | "logs" | "runs">("overview");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const logs = useMemo(() => allLogs.filter((l) => l.plugin_id === plugin.id), [allLogs, plugin.id]);
  const caps = plugin.capabilities;
  const globalServices = caps?.services.filter((s) => s.scope === "global") ?? [];
  const sessionInstances = Object.values(services).filter(
    (s) => s.plugin_id === plugin.id && s.session_id !== null,
  );

  const toggle = async () => {
    setBusy(true);
    setError(null);
    try {
      applyPlugin(await api.plugins.setEnabled(plugin.id, !plugin.enabled));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-gray-800 bg-gray-900 p-4">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="text-base font-semibold text-white truncate">{plugin.name ?? plugin.id}</h2>
            <span className="text-xs font-mono text-gray-500">{plugin.id}</span>
            {plugin.version && <span className="text-xs text-gray-500">v{plugin.version}</span>}
            <span className={`text-[10px] px-1.5 py-0.5 rounded ${STATE_STYLE[plugin.state]}`}>{plugin.state}</span>
          </div>
          {plugin.description && <p className="text-sm text-gray-400">{plugin.description}</p>}
          <p className="text-[11px] text-gray-500 font-mono truncate" title={plugin.path}>{plugin.path}</p>
          {capabilitySummary(plugin).length > 0 && (
            <p className="text-[11px] text-gray-500">{capabilitySummary(plugin).join(" · ")}</p>
          )}
          {plugin.public_routes.length > 0 && (
            <p className="text-[11px] text-amber-300">
              Public HTTP routes (no login): {plugin.public_routes.join(", ")}
            </p>
          )}
        </div>
        {plugin.state !== "error" || plugin.enabled ? (
          <label className="flex items-center gap-2 text-sm text-gray-300">
            <input type="checkbox" checked={plugin.enabled} disabled={busy} onChange={() => void toggle()} />
            Enabled
          </label>
        ) : null}
      </div>

      {plugin.error && (
        <div className="rounded border border-red-800 bg-red-950/50 px-3 py-2 text-xs text-red-200 break-words">
          {plugin.error}
          {plugin.missing_dependencies.length > 0 && (
            <div className="mt-1 font-mono text-red-300">
              pip install {plugin.missing_dependencies.map((d) => `"${d}"`).join(" ")}
            </div>
          )}
        </div>
      )}
      {error && <div className="text-xs text-red-300">{error}</div>}

      {plugin.state === "loaded" && (
        <>
          <div className="flex gap-1 border-b border-gray-800 pb-1">
            {(["overview", "settings", "logs", "runs"] as const)
              .filter((t) => t !== "settings" || plugin.global_config_schema)
              .map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => setTab(t)}
                  className={`rounded px-2 py-0.5 text-xs capitalize ${
                    tab === t ? "bg-gray-800 text-white" : "text-gray-400 hover:text-gray-200"
                  }`}
                >
                  {t}
                </button>
              ))}
          </div>

          {tab === "overview" && (
            <div className="flex flex-col gap-3">
              {globalUi && <PluginUICard pluginId={plugin.id} sessionId={null} ui={globalUi} />}
              {globalServices.length > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="text-xs uppercase tracking-wide text-gray-500">Services</div>
                  {globalServices.map((s) => (
                    <ServiceRow
                      key={s.name}
                      pluginId={plugin.id}
                      name={s.name}
                      sessionId={null}
                      state={services[serviceKey({ plugin_id: plugin.id, service: s.name, session_id: null })]}
                      onLogs={() => setTab("logs")}
                    />
                  ))}
                </div>
              )}
              {sessionInstances.length > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="text-xs uppercase tracking-wide text-gray-500">Per-session services</div>
                  {sessionInstances.map((s) => (
                    <div key={serviceKey(s)} className="flex items-center gap-2">
                      <Link to={`/console/${s.session_id}`} className="font-mono text-[11px] text-indigo-300 hover:underline">
                        {s.session_id?.slice(0, 8)}
                      </Link>
                      <div className="flex-1">
                        <ServiceRow pluginId={plugin.id} name={s.service} sessionId={s.session_id} state={s} />
                      </div>
                    </div>
                  ))}
                </div>
              )}
              {caps && caps.tools.length > 0 && (
                <div className="flex flex-col gap-0.5">
                  <div className="text-xs uppercase tracking-wide text-gray-500">Tools</div>
                  {caps.tools.map((t) => (
                    <div key={t.full_name} className="text-[11px] text-gray-300">
                      <span className="font-mono">{t.full_name}</span>
                      <span className="text-gray-500"> — {t.description}</span>
                    </div>
                  ))}
                  <span className="text-[11px] text-gray-600">Select them per agent in the Agent Registry.</span>
                </div>
              )}
            </div>
          )}
          {tab === "settings" && <GlobalConfig plugin={plugin} />}
          {tab === "logs" && (
            <div className="max-h-80 overflow-y-auto rounded border border-gray-800 bg-gray-950 p-2">
              <LogList lines={logs.slice(-500)} empty="No log lines since this page was opened." />
            </div>
          )}
          {tab === "runs" && <RunHistory pluginId={plugin.id} />}
        </>
      )}
    </div>
  );
}

export default function PluginRegistry() {
  const plugins = usePluginHostStore((s) => s.plugins);
  const connected = usePluginHostStore((s) => s.connected);
  const streamError = usePluginHostStore((s) => s.error);
  const connect = usePluginHostStore((s) => s.connect);
  const disconnect = usePluginHostStore((s) => s.disconnect);
  const refresh = usePluginHostStore((s) => s.refresh);
  const [reloading, setReloading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    connect();
    void refresh().catch((e) => setMessage(errorText(e)));
    return disconnect;
  }, [connect, disconnect, refresh]);

  const rescan = async () => {
    setReloading(true);
    setMessage(null);
    try {
      const result = await api.plugins.reload();
      await refresh();
      setMessage(result.changed.length ? `Changed: ${result.changed.join(", ")}` : "No changes.");
    } catch (e) {
      setMessage(errorText(e));
    } finally {
      setReloading(false);
    }
  };

  const list = Object.values(plugins).sort((a, b) => a.id.localeCompare(b.id));

  return (
    <div className="flex-1 overflow-y-auto p-6">
      <div className="max-w-4xl mx-auto flex flex-col gap-4">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-semibold text-white">Plugins</h1>
          <span className={`text-[11px] ${connected ? "text-green-400" : "text-gray-500"}`}>
            {connected ? "● live" : "○ offline"}
          </span>
          <button
            type="button"
            onClick={() => void rescan()}
            disabled={reloading}
            className="ml-auto rounded-lg border border-gray-700 bg-gray-800 px-3 py-1.5 text-sm text-gray-200 hover:border-gray-500 disabled:opacity-50"
          >
            {reloading ? "Scanning…" : "Rescan"}
          </button>
        </div>
        <p className="text-sm text-gray-500">
          Plugins are installed in <span className="font-mono">~/.augentia/plugins/&lt;id&gt;/</span>. Rescan
          picks up new plugins and retries broken ones; to reload a plugin's code, disable and enable it.
        </p>
        {message && <div className="text-sm text-gray-300">{message}</div>}
        {streamError && <div className="text-sm text-amber-300">{streamError}</div>}
        {list.length === 0 && <p className="text-sm text-gray-500">No plugins installed.</p>}
        {list.map((p) => (
          <PluginPanel key={p.id} plugin={p} />
        ))}
      </div>
    </div>
  );
}
