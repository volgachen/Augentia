import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { PluginInfo } from "../api/types";
import SchemaForm from "./SchemaForm";

// Agent-level plugin defaults (config.plugins), copied into each new session
// of the agent (backend/app/plugins/README.md §9). Existing sessions keep
// their own settings.

type Entry = Record<string, unknown>;
type Defaults = Record<string, Entry>;

const isObj = (v: unknown): v is Entry => !!v && typeof v === "object" && !Array.isArray(v);

export default function AgentPluginDefaults({
  value,
  onChange,
}: {
  value: unknown;
  onChange: (next: Defaults | undefined) => void;
}) {
  const [plugins, setPlugins] = useState<PluginInfo[] | null>(null);
  const defaults: Defaults = isObj(value)
    ? Object.fromEntries(Object.entries(value).filter((e): e is [string, Entry] => isObj(e[1])))
    : {};

  useEffect(() => {
    api.plugins.list().then(
      (list) => setPlugins(list),
      () => setPlugins([]),
    );
  }, []);

  if (plugins === null) return <span className="text-xs text-gray-500">Loading plugins…</span>;

  // Same rule as GET /sessions/{id}/plugins: something to switch or configure per session.
  const configurable = plugins.filter(
    (p) =>
      p.state === "loaded" &&
      (!!p.session_config_schema ||
        (p.capabilities?.hooks.length ?? 0) > 0 ||
        (p.capabilities?.services.some((s) => s.scope === "session") ?? false)),
  );
  // Keep entries for plugins that are not loaded right now visible.
  const orphanIds = Object.keys(defaults).filter((id) => !configurable.some((p) => p.id === id));
  if (configurable.length === 0 && orphanIds.length === 0) {
    return <span className="text-xs text-gray-500">No plugins installed.</span>;
  }

  const setEntry = (id: string, entry: Entry | undefined) => {
    const next = { ...defaults };
    if (entry === undefined) delete next[id];
    else next[id] = entry;
    onChange(Object.keys(next).length ? next : undefined);
  };

  return (
    <div className="flex flex-col gap-2">
      {configurable.map((p) => {
        const entry = defaults[p.id];
        const { enabled, ...config } = entry ?? {};
        const effectiveEnabled = typeof enabled === "boolean" ? enabled : p.session_default_enabled;
        return (
          <div key={p.id} className="flex flex-col gap-2 rounded border border-gray-800 p-2">
            <div className="flex items-center gap-2 text-sm text-gray-200">
              <input
                type="checkbox"
                checked={effectiveEnabled}
                onChange={(e) => setEntry(p.id, { ...(entry ?? {}), enabled: e.target.checked })}
              />
              <span>{p.name ?? p.id}</span>
              <span className="text-[10px] font-mono text-gray-500">{p.id}</span>
              {entry && (
                <button
                  type="button"
                  className="ml-auto text-[11px] text-gray-500 hover:text-gray-300"
                  onClick={() => setEntry(p.id, undefined)}
                  title="Use the plugin's own defaults"
                >
                  Reset
                </button>
              )}
            </div>
            {p.session_config_schema && (
              <SchemaForm
                schema={p.session_config_schema}
                value={config}
                onChange={(next) =>
                  setEntry(p.id, typeof enabled === "boolean" ? { ...next, enabled } : next)
                }
              />
            )}
          </div>
        );
      })}
      {orphanIds.map((id) => (
        <div key={id} className="flex items-center gap-2 rounded border border-gray-800 p-2 text-xs text-gray-500">
          <span className="font-mono">{id}</span>
          <span>plugin not loaded; defaults kept</span>
          <button
            type="button"
            className="ml-auto text-[11px] hover:text-gray-300"
            onClick={() => setEntry(id, undefined)}
          >
            Remove
          </button>
        </div>
      ))}
    </div>
  );
}
