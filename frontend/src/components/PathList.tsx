import type { BrowseResponse } from "../api/client";

export default function PathList({
  data,
  loading,
  error,
  selected,
  onSelect,
  onEnter,
  onGoParent,
}: {
  data: BrowseResponse | null;
  loading: boolean;
  error: string | null;
  selected: string | null;
  onSelect: (path: string) => void;
  onEnter: (path: string) => void;
  onGoParent: () => void;
}) {
  return (
    <div className="flex flex-col min-h-[12rem] border border-gray-700 rounded-lg bg-gray-900">
      <div className="px-3 py-2 border-b border-gray-700 flex items-center gap-2 text-xs">
        <button
          onClick={onGoParent}
          disabled={!data?.parent}
          className="px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-300"
          title="Parent directory"
        >
          ↑
        </button>
        <span className="font-mono text-gray-400 truncate" title={data?.path ?? ""}>
          {data?.path ?? "…"}
        </span>
      </div>
      <div className="flex-1 overflow-y-auto">
        {loading && <div className="p-3 text-xs text-gray-500">Loading…</div>}
        {error && <div className="p-3 text-xs text-red-400 font-mono">{error}</div>}
        {!loading && !error && data && data.entries.length === 0 && (
          <div className="p-3 text-xs text-gray-500">No subdirectories.</div>
        )}
        {!loading && !error && data?.entries.map((e) => {
          const isSelected = selected === e.path;
          return (
            <button
              key={e.path}
              onClick={() => onSelect(e.path)}
              onDoubleClick={() => onEnter(e.path)}
              className={`w-full text-left px-3 py-1.5 text-sm font-mono border-b border-gray-800/50 last:border-0 transition-colors ${
                isSelected
                  ? "bg-indigo-900/60 text-indigo-100"
                  : "text-gray-300 hover:bg-gray-800/60"
              }`}
            >
              <span className="text-gray-500 mr-2">▸</span>
              {e.name}
            </button>
          );
        })}
      </div>
    </div>
  );
}
