import { attachmentUrl } from "../api/client";
import type { ImageRef } from "../api/types";
import type { PendingImage } from "../hooks/useImageAttachments";

/** Thumbnails of images waiting to be sent, each removable. */
export function AttachmentTray({
  items,
  onRemove,
}: {
  items: PendingImage[];
  onRemove: (key: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {items.map((item) => (
        <div
          key={item.key}
          className={`relative h-16 ${item.previewUrl ? "w-16" : "px-2"} rounded-md border ${
            item.error ? "border-red-700" : "border-gray-600"
          } bg-gray-900 overflow-hidden flex items-center justify-center`}
          title={item.error ?? item.name}
        >
          {item.previewUrl && (
            <img src={item.previewUrl} alt={item.name} className="h-full w-full object-cover" />
          )}
          {!item.ref && !item.error && (
            <div className="absolute inset-0 bg-black/50 flex items-center justify-center text-[10px] text-gray-200">
              uploading…
            </div>
          )}
          {item.error && (
            <div className="absolute inset-0 bg-red-950/80 flex items-center justify-center p-1 text-[10px] leading-tight text-red-200 text-center">
              {item.error}
            </div>
          )}
          <button
            type="button"
            onClick={() => onRemove(item.key)}
            className="absolute top-0.5 right-0.5 h-4 w-4 rounded-full bg-black/70 text-[10px] leading-4 text-gray-200 hover:bg-red-700"
            aria-label="Remove image"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

/** Images attached to a sent user message; click opens the full image. */
export function ImageThumbs({ images }: { images?: ImageRef[] | null }) {
  if (!images || images.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-2 mt-1">
      {images.map((image, index) => {
        const url = attachmentUrl(image.ref);
        return (
          <a key={`${image.ref}-${index}`} href={url} target="_blank" rel="noreferrer">
            <img
              src={url}
              alt={`image ${index + 1}`}
              loading="lazy"
              className="h-24 max-w-[12rem] rounded-md border border-indigo-900 object-cover hover:border-indigo-500"
            />
          </a>
        );
      })}
    </div>
  );
}
