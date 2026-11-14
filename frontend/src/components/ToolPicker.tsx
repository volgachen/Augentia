import type { ToolInfo } from "../api/types";

interface Props {
  // null while loading; [] when the request failed or nothing is registered.
  tools: ToolInfo[] | null;
  selected: string[];
  onToggle: (name: string) => void;
}

interface Group {
  key: string;
  title: string;
  tools: { name: string; info: ToolInfo | null }[];
}

// Plugin tools are named "<plugin_id>__<tool>".
const pluginIdOf = (name: string) => {
  const i = name.indexOf("__");
  return i > 0 ? name.slice(0, i) : null;
};

function buildGroups(tools: ToolInfo[], selected: string[]): Group[] {
  const builtin: Group = { key: "builtin", title: "Built-in", tools: [] };
  const plugins = new Map<string, Group>();
  const pluginGroup = (id: string, name: string | null) => {
    let group = plugins.get(id);
    if (!group) {
      group = { key: `plugin:${id}`, title: name ? `${name} (${id})` : id, tools: [] };
      plugins.set(id, group);
    }
    return group;
  };
  for (const info of tools) {
    if (info.source === "plugin" && info.plugin_id) {
      pluginGroup(info.plugin_id, info.plugin_name).tools.push({ name: info.name, info });
    } else {
      builtin.tools.push({ name: info.name, info });
    }
  }
  // Selected tools the backend no longer offers (plugin disabled, broken or
  // removed): keep them visible so they can be unticked.
  const known = new Set(tools.map((t) => t.name));
  for (const name of selected) {
    if (known.has(name)) continue;
    const id = pluginIdOf(name);
    (id ? pluginGroup(id, null) : builtin).tools.push({ name, info: null });
  }
  const pluginGroups = [...plugins.values()].sort((a, b) => a.key.localeCompare(b.key));
  return [builtin, ...pluginGroups].filter((g) => g.tools.length > 0);
}

export default function ToolPicker({ tools, selected, onToggle }: Props) {
  if (tools === null) {
    return <span className="text-xs text-gray-500">Loading tools…</span>;
  }
  const groups = buildGroups(tools, selected);
  if (groups.length === 0) {
    return <span className="text-xs text-gray-500">No tools available.</span>;
  }
  return (
    <div className="flex flex-col gap-3">
      {groups.map((group) => (
        <div key={group.key} className="flex flex-col gap-1">
          <div className="text-xs uppercase tracking-wide text-gray-500">{group.title}</div>
          {group.tools.map(({ name, info }) => (
            <label
              key={name}
              className={`flex items-start gap-2 text-sm ${info ? "text-gray-200" : "text-gray-500"}`}
              title={info?.description || undefined}
            >
              <input
                type="checkbox"
                className="mt-1"
                checked={selected.includes(name)}
                onChange={() => onToggle(name)}
              />
              <span className="flex flex-col min-w-0">
                <span className="flex items-center gap-2">
                  <span className="font-mono">{name}</span>
                  {info && !info.requires_approval && (
                    <span className="text-[10px] px-1 rounded bg-gray-700 text-gray-300">auto</span>
                  )}
                  {!info && (
                    <span className="text-[10px] px-1 rounded bg-amber-900/60 text-amber-300">
                      unavailable
                    </span>
                  )}
                </span>
                {info?.description && (
                  <span className="text-xs text-gray-500 truncate">{info.description}</span>
                )}
                {!info && (
                  <span className="text-xs text-gray-500">
                    {pluginIdOf(name)
                      ? "Plugin is disabled, failed to load, or not installed. Sessions start without this tool."
                      : "No longer provided by the backend."}
                  </span>
                )}
              </span>
            </label>
          ))}
        </div>
      ))}
    </div>
  );
}
