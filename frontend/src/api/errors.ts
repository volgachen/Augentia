// Turn an api/client request error ("<status> <body>") into a readable message.
// Bodies are FastAPI errors: {"detail": "..."} or {"detail": {"message", "errors": [...]}}.
export function errorText(e: unknown): string {
  const message = e instanceof Error ? e.message : String(e);
  const body = message.replace(/^\d+\s/, "");
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
    const detail = parsed.detail as { errors?: string[]; message?: string } | undefined;
    if (detail?.errors) return detail.errors.join("; ");
    if (detail?.message) return detail.message;
  } catch {
    // not JSON
  }
  return message;
}
