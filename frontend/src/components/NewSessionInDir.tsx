import { useState } from "react";
import type { AgentTemplate } from "../api/types";

interface Props {
  dir: string | null;
  agents: AgentTemplate[];
  // Agent preselected in the dropdown (e.g. the one last used in this folder).
  defaultAgentId: string | null;
  onCreate: (agentId: string) => Promise<void>;
}

/**
 * "+ 新建会话" on a working-directory group header. Expands in place into an
 * agent dropdown; creating starts a session directly in that folder
 * (use_existing_directory). Clicks are kept from toggling the group.
 */
export default function NewSessionInDir({ dir, agents, defaultAgentId, onCreate }: Props) {
  const [open, setOpen] = useState(false);
  const [agentId, setAgentId] = useState<string>("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fallback = agents.some((a) => a.id === defaultAgentId) ? defaultAgentId! : (agents[0]?.id ?? "");
  const selected = agents.some((a) => a.id === agentId) ? agentId : fallback;

  const create = async () => {
    if (!selected || creating) return;
    setCreating(true);
    setError(null);
    try {
      await onCreate(selected);
      setOpen(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setCreating(false);
    }
  };

  return (
    <span
      className="ml-auto flex shrink-0 items-center gap-1.5"
      onClick={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()}
    >
      {!open ? (
        <button
          type="button"
          onClick={() => {
            setOpen(true);
            setAgentId("");
            setError(null);
          }}
          disabled={agents.length === 0}
          title={dir ? `在 ${dir} 下新建会话` : "新建一个不指定工作目录的会话"}
          className="rounded px-2 py-0.5 text-gray-400 hover:bg-gray-700 hover:text-gray-100 disabled:opacity-40"
        >
          + 新建会话
        </button>
      ) : (
        <>
          {error && (
            <span className="max-w-xs truncate text-red-400" title={error}>
              {error}
            </span>
          )}
          <select
            autoFocus
            value={selected}
            onChange={(e) => setAgentId(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") create();
              if (e.key === "Escape") setOpen(false);
            }}
            disabled={creating}
            className="rounded border border-gray-700 bg-gray-950 px-1.5 py-0.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          >
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={create}
            disabled={!selected || creating}
            className="rounded bg-indigo-600 px-2 py-0.5 text-white hover:bg-indigo-500 disabled:opacity-40"
          >
            {creating ? "创建中…" : "创建"}
          </button>
          <button
            type="button"
            onClick={() => setOpen(false)}
            disabled={creating}
            className="rounded px-2 py-0.5 text-gray-400 hover:bg-gray-700 hover:text-gray-100 disabled:opacity-40"
          >
            取消
          </button>
        </>
      )}
    </span>
  );
}
