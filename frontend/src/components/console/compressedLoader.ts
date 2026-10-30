import { createContext, useContext } from "react";
import type { StreamEvent } from "../../api/types";

// Fetches the rows a compression summary replaced (soft-deleted with
// metadata.compressed_by = summaryId), as stream events. The live console uses
// the authenticated endpoint; the share page its public one. null: summaries
// cannot be expanded.
export type CompressedLoader = (summaryId: string) => Promise<StreamEvent[]>;

export const CompressedLoaderContext = createContext<CompressedLoader | null>(null);

export function useCompressedLoader(): CompressedLoader | null {
  return useContext(CompressedLoaderContext);
}
