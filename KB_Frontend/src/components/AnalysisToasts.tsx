import { useCallback, useEffect, useRef, useState } from "react";

import { fetchProcessing } from "../api/client";
import { useChangeFeed } from "../hooks/useChangeFeed";

/** How long a "began analysis" toast stays up before auto-dismissing. */
const TOAST_MS = 4500;
/** Cap the stack so a burst (a sync flagging many notes) can't flood the screen. */
const MAX_TOASTS = 4;

type Toast = { key: string; label: string };

/**
 * Transient top-of-screen toasts announcing when a memory enters enrichment. The change feed
 * only says "nodes changed" (not which, nor the new status), so on each nodes change we poll
 * the set of currently-processing memories and diff it against the previous poll — the newly
 * appearing ones just began analysis. The first poll only establishes a baseline, so opening
 * the app doesn't announce whatever was already mid-analysis.
 */
export function AnalysisToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  // Node ids seen processing on the previous poll; null until the baseline poll runs.
  const seenRef = useRef<Set<string> | null>(null);
  const timersRef = useRef<Map<string, number>>(new Map());

  const dismiss = useCallback((key: string) => {
    const timer = timersRef.current.get(key);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timersRef.current.delete(key);
    }
    setToasts((ts) => ts.filter((t) => t.key !== key));
  }, []);

  const poll = useCallback(async () => {
    let nodes: { id: string; title: string | null; slug: string }[];
    try {
      ({ nodes } = await fetchProcessing());
    } catch {
      return; // a failed poll just skips a beat; the next change re-polls
    }
    const current = new Set(nodes.map((n) => n.id));
    const seen = seenRef.current;
    seenRef.current = current;
    if (seen === null) return; // baseline only — don't announce what was already running
    const started = nodes.filter((n) => !seen.has(n.id));
    if (started.length === 0) return;
    const additions: Toast[] = started.map((n) => ({
      key: `${n.id}:${Date.now()}:${Math.random().toString(36).slice(2)}`,
      label: n.title || n.slug,
    }));
    setToasts((prev) => [...prev, ...additions].slice(-MAX_TOASTS));
    for (const a of additions) {
      timersRef.current.set(a.key, window.setTimeout(() => dismiss(a.key), TOAST_MS));
    }
  }, [dismiss]);

  // Baseline on mount; re-poll on every live nodes change.
  useEffect(() => {
    poll();
    const timers = timersRef.current;
    return () => {
      timers.forEach((t) => window.clearTimeout(t));
      timers.clear();
    };
  }, [poll]);

  useChangeFeed((tables) => {
    if (tables.has("nodes")) poll();
  });

  if (toasts.length === 0) return null;
  return (
    <div className="toast-stack" role="status" aria-live="polite">
      {toasts.map((t) => (
        <button key={t.key} className="toast" onClick={() => dismiss(t.key)} title="Dismiss">
          <span className="spinner toast-spin" />
          <span className="toast-text">
            Analyzing <b>{t.label}</b>…
          </span>
        </button>
      ))}
    </div>
  );
}
