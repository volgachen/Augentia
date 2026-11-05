import { createContext, useContext } from "react";

// Jump-to-message support for transcripts (the share page's ?m=<message id>).
export type MessageFocus = {
  // Row to highlight and scroll to; null: none.
  targetId: string | null;
  // Summaries to open on mount so a compressed target becomes visible.
  expand: ReadonlySet<string>;
  // The first item rendering the target calls this; true means it should
  // scroll (only once per target, so polling re-renders do not jump).
  claimScroll: () => boolean;
  // Point the page at a message (the per-message link button).
  linkTo?: (messageId: string) => void;
};

const NONE: MessageFocus = { targetId: null, expand: new Set(), claimScroll: () => false };

export const MessageFocusContext = createContext<MessageFocus>(NONE);

export function useMessageFocus(): MessageFocus {
  return useContext(MessageFocusContext);
}
