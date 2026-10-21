import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { ImageRef } from "../api/types";

// Formats the backend accepts (it re-checks magic bytes server-side).
export const ACCEPTED_IMAGE_TYPES = "image/png,image/jpeg,image/webp,image/gif";
const MAX_IMAGES = 10;

// Unique keys for draft images (React list keys / patch targets).
let draftSeq = 0;
const nextDraftKey = () => `draft-${++draftSeq}`;

export interface PendingImage {
  key: string;
  scope: string; // the session this draft image belongs to
  name: string;
  previewUrl: string; // local object URL, shown while uploading and after
  ref?: string; // set once the upload succeeds
  error?: string;
}

/**
 * Draft image uploads for a composer, scoped per session: images added while
 * viewing one session stay with it (a per-session draft) and are hidden in
 * others, so switching sessions needs no reset. Files upload as soon as they
 * are added; readyImages() returns the refs to send. Object URLs are revoked
 * on removal, clear and unmount.
 */
export function useImageAttachments(scope: string | undefined) {
  const [all, setAll] = useState<PendingImage[]>([]);
  const allRef = useRef<PendingImage[]>([]);
  useEffect(() => {
    allRef.current = all;
  }, [all]);
  useEffect(
    () => () => allRef.current.forEach((item) => item.previewUrl && URL.revokeObjectURL(item.previewUrl)),
    [],
  );

  const items = all.filter((item) => item.scope === scope);

  const patchItem = (key: string, patch: Partial<PendingImage>) =>
    setAll((cur) => cur.map((item) => (item.key === key ? { ...item, ...patch } : item)));

  const addFiles = (files: File[]) => {
    if (!scope) return;
    const images = files.filter((f) => f.type.startsWith("image/"));
    const room = Math.max(0, MAX_IMAGES - items.filter((i) => !i.error).length);
    const accepted = images.slice(0, room);
    const added = accepted.map((file) => ({
      item: {
        key: nextDraftKey(),
        scope,
        name: file.name || "pasted image",
        previewUrl: URL.createObjectURL(file),
      },
      file,
    }));
    const overflow: PendingImage[] =
      images.length > accepted.length
        ? [{
            key: nextDraftKey(),
            scope,
            name: "",
            previewUrl: "",
            error: `At most ${MAX_IMAGES} images per message`,
          }]
        : [];
    setAll((cur) => [...cur, ...added.map((a) => a.item), ...overflow]);
    for (const { item, file } of added) {
      api.attachments
        .upload(file)
        .then((uploaded) => patchItem(item.key, { ref: uploaded.ref }))
        .catch((e: Error) => patchItem(item.key, { error: e.message }));
    }
  };

  const remove = (key: string) => {
    const item = all.find((i) => i.key === key);
    if (item?.previewUrl) URL.revokeObjectURL(item.previewUrl);
    setAll((cur) => cur.filter((i) => i.key !== key));
  };

  // Clears this session's drafts only.
  const clear = () => {
    items.forEach((item) => item.previewUrl && URL.revokeObjectURL(item.previewUrl));
    setAll((cur) => cur.filter((i) => i.scope !== scope));
  };

  const uploading = items.some((i) => !i.ref && !i.error);
  const readyImages = (): ImageRef[] =>
    items.filter((i) => i.ref && !i.error).map((i) => ({ ref: i.ref! }));

  return { items, addFiles, remove, clear, uploading, readyImages };
}
