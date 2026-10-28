import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Session } from "../api/types";

const BUSY_STATUSES = new Set(["RUNNING", "WAITING_CONFIRM"]);

export default function SystemPromptPanel({ session }: { session: Session }) {
  // `prompt` is the last value loaded from / saved to the backend; `draft` is
  // what's in the editor. They differ only while the user has unsaved edits.
  const [prompt, setPrompt] = useState("");
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  // Starts true: the parent keys this panel by session id, so every session
  // gets a fresh instance and the effect below only has to clear it.
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const busy = BUSY_STATUSES.has(session.status);
  const dirty = draft !== prompt;

  useEffect(() => {
    let cancelled = false;
    api.sessions
      .systemPrompt(session.id)
      .then((result) => {
        if (!cancelled) {
          setPrompt(result.system_prompt);
          setDraft(result.system_prompt);
        }
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
    if (!draft.trim()) {
      setError("System prompt must not be empty");
      return;
    }
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const result = await api.sessions.updateSystemPrompt(session.id, draft);
      setPrompt(result.system_prompt);
      setDraft(result.system_prompt);
      setEditing(false);
      setSaved(true);
    } catch (e) {
      const message = (e as Error).message;
      setError(
        message.startsWith("409")
          ? "Session is running. Save again after the current turn finishes."
          : message,
      );
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    setDraft(prompt);
    setEditing(false);
    setError(null);
  };

  if (loading) {
    return (
      <section className="flex-1 flex flex-col min-h-0">
        <p className="px-2 text-xs text-gray-600">Loading system prompt…</p>
      </section>
    );
  }

  return (
    <section className="flex-1 flex flex-col min-h-0">
      {editing ? (
        <>
          <div className="shrink-0 px-2 pb-2 text-[11px] leading-4 text-gray-500">
            Replaces the whole system prompt of this session. It takes effect on the next
            model request; message history is kept.
          </div>
          <textarea
            className="flex-1 min-h-0 w-full rounded-lg border border-gray-700 bg-gray-950 p-2 font-mono text-[11px] leading-4 text-gray-200 outline-none focus:border-indigo-500"
            spellCheck={false}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              setSaved(false);
            }}
            autoFocus
          />
        </>
      ) : (
        <pre className="flex-1 min-h-0 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-gray-700 bg-gray-950 p-2 font-mono text-[11px] leading-4 text-gray-200">
          {prompt || "(empty system prompt)"}
        </pre>
      )}

      <div className="shrink-0 mt-2 flex items-center justify-between gap-2 px-2">
        <span className="font-mono text-[10px] text-gray-500">
          {(editing ? draft : prompt).length} chars
          {editing && dirty && <span className="ml-1.5 text-amber-400">· unsaved</span>}
        </span>
        <div className="flex gap-2">
          {editing ? (
            <>
              <button
                type="button"
                onClick={handleCancel}
                disabled={saving}
                className="rounded border border-gray-700 bg-gray-900 px-2.5 py-1 text-xs text-gray-300 hover:border-gray-500 disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleSave}
                disabled={saving || busy || !dirty}
                title={busy ? "Wait for the current turn to finish" : undefined}
                className="rounded bg-indigo-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                {saving ? "Saving…" : "Save"}
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => {
                setEditing(true);
                setSaved(false);
                setError(null);
              }}
              disabled={!!error && !prompt}
              className="rounded border border-gray-700 bg-gray-900 px-2.5 py-1 text-xs text-gray-300 hover:border-gray-500 disabled:opacity-50"
            >
              Edit
            </button>
          )}
        </div>
      </div>

      {editing && busy && !error && (
        <div className="mt-2 rounded border border-amber-800 bg-amber-950/40 px-2 py-1.5 text-[11px] leading-4 text-amber-200">
          A turn is in progress. You can keep editing; Save is enabled once it finishes.
        </div>
      )}
      {error && (
        <div className="mt-2 rounded border border-red-800 bg-red-950/50 px-2 py-1.5 text-[11px] leading-4 text-red-200">
          {error}
        </div>
      )}
      {saved && !error && (
        <div className="mt-2 rounded border border-green-800 bg-green-950/40 px-2 py-1.5 text-[11px] text-green-200">
          Saved. The next model request uses the new prompt.
        </div>
      )}
    </section>
  );
}
