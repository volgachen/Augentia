import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { CreateAgentPayload } from "../api/client";
import type { AgentTemplate, AgentType } from "../api/types";

interface Props {
  open: boolean;
  // null => create mode; an agent => edit mode
  agent: AgentTemplate | null;
  onClose: () => void;
  onSaved: () => void;
}

const EMPTY: CreateAgentPayload = {
  name: "",
  description: "",
  agent_type: "tool_use",
  system_prompt: "",
  tool_names: [],
  openai_model: "gpt-4o",
  config: {},
  openai_base_url: null,
};

// config.model_params is edited as JSON text; reserved keys mirror the backend
// (app/adapters/model_params.py), which rejects them at session start.
const RESERVED_PARAM_KEYS = [
  "model", "messages", "tools", "tool_choice", "functions", "function_call",
  "stream", "stream_options", "n",
];

const paramsToText = (value: unknown) =>
  value && typeof value === "object" && Object.keys(value).length > 0
    ? JSON.stringify(value, null, 2)
    : "";

// Returns the parsed object (undefined for empty text) or an error message.
function parseParams(text: string): { value?: Record<string, unknown>; error?: string } {
  if (!text.trim()) return {};
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (e) {
    return { error: `Invalid JSON: ${(e as Error).message}` };
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { error: "Model params must be a JSON object." };
  }
  const reserved = Object.keys(value).filter((k) => RESERVED_PARAM_KEYS.includes(k));
  if (reserved.length > 0) {
    return { error: `Reserved keys are set by the engine: ${reserved.join(", ")}` };
  }
  return { value: value as Record<string, unknown> };
}

const inputClass =
  "w-full px-3 py-2 rounded-lg bg-gray-900 border border-gray-700 focus:border-indigo-500 outline-none text-sm text-gray-100";

export default function AgentDetailModal({ open, agent, onClose, onSaved }: Props) {
  const isEdit = agent !== null;
  const [form, setForm] = useState<CreateAgentPayload>(EMPTY);
  const [availableTools, setAvailableTools] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [paramsText, setParamsText] = useState("");

  useEffect(() => {
    if (!open) return;
    setError(null);
    setParamsText(paramsToText(agent?.config?.model_params));
    setForm(
      agent
        ? {
            name: agent.name,
            description: agent.description,
            agent_type: agent.agent_type,
            system_prompt: agent.system_prompt,
            tool_names: agent.tool_names,
            config: agent.config ?? {},
            openai_model: agent.openai_model,
            openai_base_url: agent.openai_base_url,
          }
        : EMPTY,
    );
    api.tools.list().then(setAvailableTools).catch(() => setAvailableTools([]));
  }, [open, agent]);

  if (!open) return null;

  const isToolUse = form.agent_type === "tool_use";
  const paramsResult = parseParams(paramsText);

  const patch = (p: Partial<CreateAgentPayload>) => setForm((f) => ({ ...f, ...p }));

  const toggleTool = (name: string) =>
    patch({
      tool_names: form.tool_names.includes(name)
        ? form.tool_names.filter((t) => t !== name)
        : [...form.tool_names, name],
    });

  const handleSave = async () => {
    if (paramsResult.error) {
      setError(paramsResult.error);
      return;
    }
    setSaving(true);
    setError(null);
    // Empty text removes model_params; otherwise store the parsed object.
    const config = { ...(form.config ?? {}) };
    if (paramsResult.value) config.model_params = paramsResult.value;
    else delete config.model_params;
    const payload: CreateAgentPayload = { ...form, config };
    try {
      if (isEdit && agent) {
        const { agent_type: _t, ...rest } = payload;
        await api.agents.update(agent.id, rest);
      } else {
        await api.agents.create(payload);
      }
      onSaved();
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="bg-gray-800 border border-gray-700 rounded-xl w-full max-w-lg max-h-[90vh] overflow-y-auto p-6 flex flex-col gap-4">
        <h2 className="text-xl font-semibold text-white">
          {isEdit ? "Edit Agent" : "New Agent"}
        </h2>

        {error && (
          <div className="px-3 py-2 rounded-lg bg-red-900/40 border border-red-800 text-red-200 text-sm font-mono">
            {error}
          </div>
        )}

        <label className="flex flex-col gap-1 text-sm text-gray-300">
          Name
          <input
            className={inputClass}
            value={form.name}
            onChange={(e) => patch({ name: e.target.value })}
          />
        </label>

        <label className="flex flex-col gap-1 text-sm text-gray-300">
          Description
          <input
            className={inputClass}
            value={form.description}
            onChange={(e) => patch({ description: e.target.value })}
          />
        </label>

        <label className="flex flex-col gap-1 text-sm text-gray-300">
          Agent type
          {isEdit ? (
            <div className="px-3 py-2 rounded-lg bg-gray-900 border border-gray-800 text-sm text-gray-400">
              {form.agent_type} <span className="text-gray-600">(immutable)</span>
            </div>
          ) : (
            <select
              className={inputClass}
              value={form.agent_type}
              onChange={(e) => patch({ agent_type: e.target.value as AgentType })}
            >
              <option value="tool_use">Tool Use</option>
            </select>
          )}
        </label>

        <label className="flex flex-col gap-1 text-sm text-gray-300">
          System prompt
          <textarea
            className={`${inputClass} min-h-[80px] font-mono`}
            value={form.system_prompt}
            onChange={(e) => patch({ system_prompt: e.target.value })}
          />
        </label>

        {isToolUse && (
          <>
            <div className="flex flex-col gap-1 text-sm text-gray-300">
              Tools
              <div className="flex flex-col gap-1 bg-gray-900 border border-gray-700 rounded-lg p-2">
                {availableTools.length === 0 && (
                  <span className="text-xs text-gray-500">No tools available.</span>
                )}
                {availableTools.map((t) => (
                  <label key={t} className="flex items-center gap-2 text-sm text-gray-200">
                    <input
                      type="checkbox"
                      checked={form.tool_names.includes(t)}
                      onChange={() => toggleTool(t)}
                    />
                    <span className="font-mono">{t}</span>
                  </label>
                ))}
              </div>
            </div>

            <label className="flex flex-col gap-1 text-sm text-gray-300">
              OpenAI model
              <input
                className={inputClass}
                value={form.openai_model}
                onChange={(e) => patch({ openai_model: e.target.value })}
              />
            </label>

            <label className="flex flex-col gap-1 text-sm text-gray-300">
              OpenAI base URL (optional)
              <input
                className={inputClass}
                value={form.openai_base_url ?? ""}
                onChange={(e) =>
                  patch({ openai_base_url: e.target.value || null })
                }
              />
            </label>

            <label className="flex flex-col gap-1 text-sm text-gray-300">
              Model params (optional JSON)
              <textarea
                className={`${inputClass} min-h-[72px] font-mono`}
                placeholder={'{"temperature": 0.7, "reasoning_effort": "high"}'}
                value={paramsText}
                onChange={(e) => setParamsText(e.target.value)}
              />
              <span className={`text-xs ${paramsResult.error ? "text-red-400" : "text-gray-500"}`}>
                {paramsResult.error ??
                  "Merged into every model request. Provider-specific fields go under \"extra_body\". Applies to new sessions."}
              </span>
            </label>

            <div className="flex flex-col gap-1 text-sm text-gray-300">
              <label className="flex items-center gap-2 text-sm text-gray-200">
                <input
                  type="checkbox"
                  checked={form.config?.vision === true}
                  disabled={form.config?.engine === "legacy"}
                  onChange={(e) =>
                    patch({ config: { ...form.config, vision: e.target.checked } })
                  }
                />
                Accept image input (vision)
              </label>
              <span className="text-xs text-gray-500">
                {form.config?.engine === "legacy"
                  ? "Not available on the legacy engine."
                  : "The model above must support image input."}
              </span>
            </div>
          </>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-lg bg-gray-900 border border-gray-600 hover:border-gray-400 text-sm text-gray-200"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving || !form.name.trim() || !!paramsResult.error}
            className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-sm text-white font-medium"
          >
            {saving ? "Saving..." : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
