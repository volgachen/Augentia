import type { Session } from "../api/types";

type StatusFields = Pick<Session, "status" | "pending_joins" | "failed_joins">;

/** Status text, with the number of dispatched branches still running after
 *  DISPATCHING (dispatch mode), e.g. "DISPATCHING · 3", and unacknowledged
 *  failed branches after any status, e.g. "WAITING_USER · 2 失败". */
export function statusLabel(session: StatusFields): string {
  const pending = session.pending_joins ?? 0;
  const failed = session.failed_joins ?? 0;
  const base = session.status === "DISPATCHING" ? `${session.status} · ${pending}` : session.status;
  return failed > 0 ? `${base} · ${failed} 失败` : base;
}

export function statusTitle(session: StatusFields): string | undefined {
  const failed = session.failed_joins ?? 0;
  const failedNote = failed > 0 ? `${failed} 个并行分支失败了，结果没有拼回` : undefined;
  if (session.status !== "DISPATCHING") return failedNote;
  const pending = session.pending_joins ?? 0;
  const dispatchNote = pending > 0
    ? `派发模式：${pending} 个分支还在处理，结果还没拼回`
    : "派发模式：没有正在处理的分支";
  return failedNote ? `${dispatchNote}；${failedNote}` : dispatchNote;
}

/** Whether to mark a session as having failed dispatched branches. */
export function hasFailedJoins(session: Pick<Session, "failed_joins">): boolean {
  return (session.failed_joins ?? 0) > 0;
}
