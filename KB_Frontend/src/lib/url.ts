// Lightweight URL query-state — no router dependency. Reads/writes the query string
// via the History API and notifies in-app subscribers on programmatic changes. Back/
// forward (popstate) is handled by the hook layer. State is stored in `?key=value`
// pairs; a value equal to its default is omitted so the URL stays clean.

type Listener = () => void;
const listeners = new Set<Listener>();

function notify(): void {
  for (const fn of [...listeners]) fn();
}

export function getParam(key: string): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get(key);
}

/** Apply a batch of param updates (null/"" deletes the key) and notify subscribers. */
export function setParams(updates: Record<string, string | null>): void {
  if (typeof window === "undefined") return;
  const params = new URLSearchParams(window.location.search);
  for (const [key, value] of Object.entries(updates)) {
    if (value == null || value === "") params.delete(key);
    else params.set(key, value);
  }
  const qs = params.toString();
  const { pathname, hash } = window.location;
  window.history.replaceState(null, "", `${pathname}${qs ? `?${qs}` : ""}${hash}`);
  notify();
}

export function subscribeUrl(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
