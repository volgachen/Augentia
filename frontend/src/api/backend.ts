// Which backend the app talks to. Chosen in the settings dialog (click
// "Augentia" top-left), stored per browser in localStorage, read once at load:
// changing it reloads the page so every request and WebSocket switches over.

const STORAGE_KEY = "augentia.backend";
export const DEFAULT_PORT = "12598";

// Default: the same host the browser connected to, so the app works on any
// machine in the LAN.
export function defaultBackend(): string {
  return `http://${window.location.hostname}:${DEFAULT_PORT}`;
}

/**
 * Normalize user input into an origin like "http://host:port".
 * Accepts "12597" (port on the current host), "host:port", "host",
 * or a full "http(s)://host:port" URL (any path is dropped).
 * Returns null when the input cannot be parsed.
 */
export function normalizeBackend(input: string): string | null {
  let value = input.trim().replace(/\/+$/, "");
  if (!value) return null;
  if (/^\d{1,5}$/.test(value)) value = `${window.location.hostname}:${value}`;
  if (!/^https?:\/\//i.test(value)) value = `http://${value}`;
  try {
    const url = new URL(value);
    if (!url.hostname) return null;
    return url.origin;
  } catch {
    return null;
  }
}

/** The saved backend, or null when none is saved (the default is used). */
export function savedBackend(): string | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? normalizeBackend(raw) : null;
  } catch {
    return null;
  }
}

/** Save a backend (null clears it, back to the default) and reload. */
export function saveBackend(origin: string | null): void {
  if (origin === null || origin === defaultBackend()) localStorage.removeItem(STORAGE_KEY);
  else localStorage.setItem(STORAGE_KEY, origin);
  window.location.reload();
}

export const BACKEND_ORIGIN = savedBackend() ?? defaultBackend();
export const API_BASE = `${BACKEND_ORIGIN}/api/v1`;
export const WS_API_BASE = `${BACKEND_ORIGIN.replace(/^http/i, "ws")}/api/v1`;
