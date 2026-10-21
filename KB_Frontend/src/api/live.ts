// Live change feed: a resilient WebSocket to the dashboard's /api/ws. The server pushes
// one small {table, op, id} message per committed DB change (see KB_Core migration 0008).
// The UI treats each as a "refetch this view" signal; coalescing is the caller's job.
//
// A single socket is shared across every subscriber (App, IngestionPage, AnalysisToasts,
// ReenrichBanner): the connection opens on the first subscriber and closes on the last,
// so N consumers cost one socket and one backoff loop — not N of each.

export type ChangeEvent = {
  table: string;
  op: "INSERT" | "UPDATE" | "DELETE";
  id: string | number;
};

const MAX_BACKOFF_MS = 30_000;

const subscribers = new Set<(e: ChangeEvent) => void>();
let ws: WebSocket | null = null;
let started = false;
let attempt = 0;
let timer: number | undefined;

const endpoint = () => {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/api/ws`;
};

const connect = () => {
  ws = new WebSocket(endpoint());
  ws.onopen = () => {
    attempt = 0;
  };
  ws.onmessage = (ev) => {
    let parsed: ChangeEvent;
    try {
      parsed = JSON.parse(ev.data) as ChangeEvent;
    } catch {
      return; // ignore malformed frames
    }
    // Snapshot: a subscriber that unsubscribes mid-dispatch must not break iteration.
    for (const fn of [...subscribers]) fn(parsed);
  };
  ws.onclose = () => {
    ws = null;
    if (subscribers.size === 0) {
      started = false;
      return;
    }
    const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** attempt++);
    timer = window.setTimeout(connect, delay);
  };
  ws.onerror = () => ws?.close();
};

const teardown = () => {
  started = false;
  attempt = 0;
  window.clearTimeout(timer);
  timer = undefined;
  const sock = ws;
  ws = null;
  if (sock) {
    sock.onclose = null; // suppress the reconnect that close() would otherwise schedule
    sock.close();
  }
};

/**
 * Subscribe to the change feed. Calls `onChange` for every notification from the
 * shared socket and auto-reconnects with exponential backoff while any subscriber
 * remains. Returns an unsubscribe function; the socket closes once the last
 * subscriber leaves. No-ops gracefully where WebSocket is unavailable (e.g. jsdom).
 */
export function subscribeChanges(onChange: (e: ChangeEvent) => void): () => void {
  if (typeof WebSocket === "undefined") return () => {};

  subscribers.add(onChange);
  if (!started) {
    started = true;
    connect();
  }

  return () => {
    subscribers.delete(onChange);
    if (subscribers.size === 0) teardown();
  };
}
