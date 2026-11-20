import { useEffect } from "react";
import { createPortal } from "react-dom";

/**
 * Full-size image over the page. Clicking outside the image (or Esc) closes
 * it. Rendered into <body> so it sits above the console and any dialog; it
 * stays a scrollable page underneath, so nothing is unmounted.
 */
export default function ImageLightbox({
  src,
  alt,
  onClose,
}: {
  src: string;
  alt?: string;
  onClose: () => void;
}) {
  useEffect(() => {
    // Capture phase: closes on Esc before the console's own shortcuts see it.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      onClose();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={alt || "image"}
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/80 p-6 cursor-zoom-out"
      // mousedown, not click: only the backdrop itself, so a drag that starts
      // on the image and ends outside does not close it.
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <img
        src={src}
        alt={alt ?? ""}
        className="max-h-full max-w-full rounded-md border border-gray-700 shadow-2xl cursor-default"
      />
      <button
        type="button"
        onClick={onClose}
        title="关闭（Esc）"
        aria-label="关闭"
        className="absolute top-3 right-4 text-2xl leading-none text-gray-400 hover:text-white"
      >
        ×
      </button>
    </div>,
    document.body,
  );
}
