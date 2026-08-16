import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Session } from "../api/types";

const DEFAULT_TOOL_PERMISSIONS = {
  default: "ask",
  rules: [
    {
      id: "allow-read-workspace",
      effect: "allow",
      tool: "read",
      paths: ["./**"],
    },
    {
      id: "deny-sensitive-files",
      effect: "deny",
      tool: "*",
      paths: ["./.env", "./.env.*", "./secrets/**", "./.git/**"],
    },
    {
      id: "allow-safe-bash",
      effect: "allow",
      tool: "bash",
      commands: ["rg *", "git status", "git diff *"],
    },
  ],
};

function pretty(value: unknown): string {
  return JSON.stringify(value ?? DEFAULT_TOOL_PERMISSIONS, null, 2);
}

function permissionsFromConfig(config: Record<string, unknown> | null): unknown {
  return config?.tool_permissions ?? DEFAULT_TOOL_PERMISSIONS;
}

export default function ToolPermissionsPanel({
  session,
  showToolCalls,
  showToolResults,
  onShowToolCallsChange,
  onShowToolResultsChange,
}: {
  session: Session;
  showToolCalls: boolean;
  showToolResults: boolean;
  onShowToolCallsChange: (show: boolean) => void;
  onShowToolResultsChange: (show: boolean) => void;
}) {
  const [sessionConfig, setSessionConfig] = useState<Record<string, unknown> | null>(null);
  const [draft, setDraft] = useState(pretty(DEFAULT_TOOL_PERMISSIONS));
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [testTool, setTestTool] = useState("edit");
  const [testPath, setTestPath] = useState("./README.md");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{
    action: "allow" | "deny" | "ask";
    reason: string;
    rule_id: string | null;
    resolved_path: string | null;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSaved(false);
    api.sessions
      .config(session.id)
      .then((config) => {
        if (cancelled) return;
        setSessionConfig(config);
        setDraft(pretty(permissionsFromConfig(config)));
      })
      .catch((e) => {
        if (!cancelled) setError((e as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [session.id]);

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const parsed = JSON.parse(draft) as unknown;
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("tool_permissions must be a JSON object");
      }
      const nextConfig = {
        ...(sessionConfig ?? { version: 1 }),
        tool_permissions: parsed,
      };
      const updated = await api.sessions.updateConfig(session.id, nextConfig);
      setSessionConfig(updated);
      setDraft(pretty(permissionsFromConfig(updated)));
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const handleReset = () => {
    setDraft(pretty(permissionsFromConfig(sessionConfig)));
    setError(null);
    setSaved(false);
  };

  const handleTest = async () => {
    const path = testPath.trim();
    if (!path) return;
    setTesting(true);
    setError(null);
    setTestResult(null);
    try {
      const result = await api.sessions.testToolPermission(session.id, {
        tool: testTool,
        path,
      });
      setTestResult(result);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setTesting(false);
    }
  };

  return (
    <section className="flex-1 flex flex-col min-h-0">
      <div className="shrink-0 flex items-center justify-between px-2 pb-1.5">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-400">
          Tools
        </h2>
        <span className="text-[10px] text-gray-500 font-mono truncate max-w-[9rem]" title={session.id}>
          {session.id.slice(0, 8)}…
        </span>
      </div>

      <div className="px-2 pb-2 text-[11px] leading-4 text-gray-500">
        Saved for this session in <span className="font-mono text-gray-400">$AUGENTIA_HOME/sessions/{session.id}/config.json</span>.
        Saving applies to the current live adapter immediately.
      </div>

      {loading ? (
        <p className="px-2 text-xs text-gray-600">Loading session config…</p>
      ) : (
        <>
          <textarea
            className="flex-1 min-h-0 w-full rounded-lg border border-gray-700 bg-gray-950 p-2 font-mono text-[11px] leading-4 text-gray-200 outline-none focus:border-indigo-500"
            spellCheck={false}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              setSaved(false);
            }}
          />

          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              onClick={handleReset}
              disabled={saving}
              className="rounded border border-gray-700 bg-gray-900 px-2.5 py-1 text-xs text-gray-300 hover:border-gray-500 disabled:opacity-50"
            >
              Reset
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={saving}
              className="rounded bg-indigo-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              {saving ? "Saving…" : "Save"}
            </button>
          </div>

          <div className="mt-2 rounded-lg border border-gray-800 bg-gray-950/60 p-2">
            <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-gray-500">
              Test rule
            </div>
            <div className="flex gap-1.5">
              <select
                className="rounded border border-gray-700 bg-gray-900 px-1.5 py-1 text-xs text-gray-200 outline-none focus:border-indigo-500"
                value={testTool}
                onChange={(e) => setTestTool(e.target.value)}
              >
                <option value="read">read</option>
                <option value="write">write</option>
                <option value="edit">edit</option>
              </select>
              <input
                className="min-w-0 flex-1 rounded border border-gray-700 bg-gray-900 px-2 py-1 font-mono text-xs text-gray-200 outline-none focus:border-indigo-500"
                value={testPath}
                onChange={(e) => setTestPath(e.target.value)}
                placeholder="./src/file.ts"
              />
              <button
                type="button"
                onClick={handleTest}
                disabled={testing || !testPath.trim()}
                className="rounded bg-gray-800 px-2 py-1 text-xs text-gray-200 hover:bg-gray-700 disabled:opacity-50"
              >
                {testing ? "Testing…" : "Test"}
              </button>
            </div>
            {testResult && (
              <div className="mt-2 space-y-0.5 rounded border border-gray-800 bg-gray-900 px-2 py-1.5 font-mono text-[11px] leading-4 text-gray-300">
                <div>
                  action: <span className={testResult.action === "allow" ? "text-green-300" : testResult.action === "deny" ? "text-red-300" : "text-orange-300"}>{testResult.action}</span>
                </div>
                <div>reason: {testResult.reason}</div>
                <div>rule: {testResult.rule_id ?? "—"}</div>
                <div className="break-all">resolved: {testResult.resolved_path ?? "—"}</div>
              </div>
            )}
          </div>

          {error && (
            <div className="mt-2 rounded border border-red-800 bg-red-950/50 px-2 py-1.5 text-[11px] leading-4 text-red-200">
              {error}
            </div>
          )}
          {saved && !error && (
            <div className="mt-2 rounded border border-green-800 bg-green-950/40 px-2 py-1.5 text-[11px] text-green-200">
              Saved and applied to this session.
            </div>
          )}

          <div className="mt-3 shrink-0 space-y-2 rounded-lg border border-gray-800 bg-gray-950/60 p-2.5">
            <label className="flex cursor-pointer items-center justify-between">
              <span className="text-xs text-gray-300">显示ToolCall</span>
              <input
                type="checkbox"
                className="h-4 w-4 accent-indigo-500"
                checked={showToolCalls}
                onChange={(e) => onShowToolCallsChange(e.target.checked)}
              />
            </label>
            <label className={`flex items-center justify-between ${showToolCalls ? "cursor-pointer" : "cursor-not-allowed opacity-50"}`}>
              <span className="text-xs text-gray-300">显示ToolResults</span>
              <input
                type="checkbox"
                className="h-4 w-4 accent-indigo-500"
                checked={showToolResults}
                disabled={!showToolCalls}
                onChange={(e) => onShowToolResultsChange(e.target.checked)}
              />
            </label>
          </div>
        </>
      )}
    </section>
  );
}
