import type { Session } from "../api/types";

/** Status text, with the number of dispatched branches still running after
 *  DISPATCHING (dispatch mode), e.g. "DISPATCHING · 3". */
export function statusLabel(session: Pick<Session, "status" | "pending_joins">): string {
  const pending = session.pending_joins ?? 0;
  return session.status === "DISPATCHING" ? `${session.status} · ${pending}` : session.status;
}

export function statusTitle(session: Pick<Session, "status" | "pending_joins">): string | undefined {
  if (session.status !== "DISPATCHING") return undefined;
  const pending = session.pending_joins ?? 0;
  return pending > 0
    ? `派发模式：${pending} 个分支还在处理，结果还没拼回`
    : "派发模式：没有正在处理的分支";
}
