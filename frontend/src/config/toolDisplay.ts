export interface ToolSummaryField {
  path: string;
  label?: string;
  maxLength?: number;
}

export interface ToolDisplayConfig {
  summaryFields?: ToolSummaryField[];
  formatter?: (args: unknown) => string;
}

const DEFAULT_FIELD_MAX_LENGTH = 80;
const DEFAULT_SUMMARY_MAX_FIELDS = 2;

export const TOOL_DISPLAY_CONFIG: Record<string, ToolDisplayConfig> = {
  bash: {
    summaryFields: [{ path: "command", label: "cmd", maxLength: 120 }],
  },
  read: {
    summaryFields: [{ path: "file_path", label: "file", maxLength: 100 }],
  },
  write: {
    summaryFields: [{ path: "file_path", label: "file", maxLength: 100 }],
  },
  edit: {
    summaryFields: [
      { path: "file_path", label: "file", maxLength: 100 },
      { path: "replace_all", label: "all" },
    ],
  },
  task_create: {
    summaryFields: [
      { path: "subject", label: "subject", maxLength: 80 },
      { path: "owner", label: "owner", maxLength: 40 },
    ],
  },
  task_update: {
    summaryFields: [
      { path: "task_id", label: "id", maxLength: 24 },
      { path: "status", label: "status", maxLength: 32 },
      { path: "subject", label: "subject", maxLength: 60 },
    ],
  },
  task_get: {
    summaryFields: [{ path: "task_id", label: "id", maxLength: 24 }],
  },
  subagent: {
    summaryFields: [
      { path: "agent_id", label: "agent", maxLength: 48 },
      { path: "task", label: "task", maxLength: 100 },
    ],
  },
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function getValueByPath(value: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((current, segment) => {
    if (current === undefined || current === null) return undefined;
    if (Array.isArray(current)) {
      const index = Number(segment);
      return Number.isInteger(index) ? current[index] : undefined;
    }
    if (isRecord(current)) {
      return current[segment];
    }
    return undefined;
  }, value);
}

function summarizeValue(value: unknown, maxLength = DEFAULT_FIELD_MAX_LENGTH): string | null {
  if (value === undefined || value === null || value === "") return null;

  let text: string;
  if (typeof value === "string") {
    text = value;
  } else if (typeof value === "number" || typeof value === "boolean") {
    text = String(value);
  } else {
    try {
      text = JSON.stringify(value);
    } catch {
      text = String(value);
    }
  }

  const oneLine = text.replace(/\s+/g, " ").trim();
  const truncated = oneLine.length > maxLength ? `${oneLine.slice(0, maxLength)}…` : oneLine;
  return JSON.stringify(truncated);
}

function formatField(field: ToolSummaryField, args: unknown): string | null {
  const value = getValueByPath(args, field.path);
  const summary = summarizeValue(value, field.maxLength);
  if (!summary) return null;
  return `${field.label ?? field.path}=${summary}`;
}

function fallbackSummary(args: unknown): string {
  if (!isRecord(args)) return summarizeValue(args, 120) ?? "";

  return Object.entries(args)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .slice(0, DEFAULT_SUMMARY_MAX_FIELDS)
    .map(([key, value]) => `${key}=${summarizeValue(value, DEFAULT_FIELD_MAX_LENGTH)}`)
    .filter((item): item is string => !item.endsWith("=null"))
    .join(" ");
}

export function getToolSummary(name: string, args: unknown): string {
  const config = TOOL_DISPLAY_CONFIG[name];

  if (config?.formatter) {
    return config.formatter(args).trim();
  }

  const configuredSummary = config?.summaryFields
    ?.map((field) => formatField(field, args))
    .filter((item): item is string => Boolean(item))
    .join(" ");

  return configuredSummary || fallbackSummary(args);
}
