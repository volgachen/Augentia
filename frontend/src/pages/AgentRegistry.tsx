import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { AgentTemplate } from "../api/types";
import { useStore } from "../store/sessions";
import WorkingDirPicker from "../components/WorkingDirPicker";
import AgentDetailModal from "../components/AgentDetailModal";

const AGENT_TYPE_LABEL: Record<string, string> = {
  tool_use: "Tool Use",
};

export default function AgentRegistry() {
  const [agents, setAgents] = useState<AgentTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [launching, setLaunching] = useState<string | null>(null);
  const [launchError, setLaunchError] = useState<string | null>(null);
  const [pickerFor, setPickerFor] = useState<string | null>(null);
  // detail modal: undefined = closed, null = create, agent = edit
  const [editing, setEditing] = useState<AgentTemplate | null | undefined>(undefined);
  const launchSession = useStore((s) => s.launchSession);
  const setActive = useStore((s) => s.setActiveSession);
  const navigate = useNavigate();

  const reload = useCallback(() => {
    return api.agents.list().then((data) => {
      setAgents(data);
      setLoading(false);
    });
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const handleDelete = async (agentId: string) => {
    if (!confirm("Delete this agent template?")) return;
    setLaunchError(null);
    try {
      await api.agents.delete(agentId);
      await reload();
    } catch (e) {
      setLaunchError((e as Error).message);
    }
  };

  const activeAgent = agents.find((agent) => agent.id === pickerFor) ?? null;

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400">
        Loading agents...
      </div>
    );
  }

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold text-white">Agent Registry</h1>
        <button
          onClick={() => setEditing(null)}
          className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium"
        >
          + New Agent
        </button>
      </div>
      {launchError && (
        <div className="mb-4 px-3 py-2 rounded-lg bg-red-900/40 border border-red-800 text-red-200 text-sm font-mono">
          {launchError}
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {agents.map((agent) => (
          <div
            key={agent.id}
            className="bg-gray-800 border border-gray-700 rounded-xl p-5 flex flex-col gap-3"
          >
            <div className="flex items-start justify-between gap-2">
              <h2 className="text-lg font-medium text-white">{agent.name}</h2>
              <span className="text-xs px-2 py-0.5 rounded-full bg-indigo-900 text-indigo-300 whitespace-nowrap">
                {AGENT_TYPE_LABEL[agent.agent_type] ?? agent.agent_type}
              </span>
            </div>
            <p className="text-sm text-gray-400 flex-1">{agent.description}</p>

            <div className="flex items-center gap-3 text-xs">
              <button
                onClick={() => setEditing(agent)}
                className="text-indigo-400 hover:text-indigo-300"
              >
                Edit / Details
              </button>
              <button
                onClick={() => handleDelete(agent.id)}
                className="text-gray-500 hover:text-red-400"
              >
                Delete
              </button>
            </div>

            <button
              onClick={() => {
                setLaunchError(null);
                setPickerFor(agent.id);
              }}
              disabled={launching === agent.id}
              className="w-full py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-sm font-medium transition-colors"
            >
              {launching === agent.id ? "Launching..." : "Launch"}
            </button>
          </div>
        ))}
      </div>

      <WorkingDirPicker
        open={pickerFor !== null}
        launching={launching === pickerFor}
        agentName={activeAgent?.name}
        onClose={() => {
          if (!launching) setPickerFor(null);
        }}
        onConfirm={async (value) => {
          if (!pickerFor) return;
          const agentId = pickerFor;
          setLaunching(agentId);
          setLaunchError(null);
          try {
            const sessionId = await launchSession(
              agentId,
              value.sourceDir,
              value.createMode,
              value.sessionName,
              value.additionalPrompt,
              value.additionalPromptPath,
            );
            setPickerFor(null);
            setActive(sessionId);
            navigate(`/console/${sessionId}`);
          } catch (e) {
            setLaunchError((e as Error).message);
          } finally {
            setLaunching(null);
          }
        }}
      />

      <AgentDetailModal
        open={editing !== undefined}
        agent={editing ?? null}
        onClose={() => setEditing(undefined)}
        onSaved={reload}
      />
    </div>
  );
}
