import { createContext, useContext } from "react";
import { attachmentUrl } from "../../api/client";

// How an "att:<sha256>" image ref becomes a URL. The console uses the
// authenticated download endpoint; the public share page overrides this with
// its session-scoped endpoint.
export const AttachmentUrlContext = createContext<(ref: string) => string>(attachmentUrl);

export function useAttachmentUrl(): (ref: string) => string {
  return useContext(AttachmentUrlContext);
}
