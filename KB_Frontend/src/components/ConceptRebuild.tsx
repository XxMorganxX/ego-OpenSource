import { useCallback, useEffect, useRef, useState } from "react";

import { fetchConsolidationStatus, requestConsolidation } from "../api/client";
import { useChangeFeed } from "../hooks/useChangeFeed";
import type { ConsolidationStatus } from "../types";

/** How long a queued request may sit before we re-check for the stalled verdict. The
 *  server decides `stalled`; this only makes sure we ask again, since no DB event fires
 *  when a request merely gets old. */
const STALL_RECHECK_MS = 95_000;
/** Safety net for a dropped WebSocket: the change feed is the primary signal, but if it
 *  is gone nothing would ever arrive. Only runs while something is in flight. */
const SAFETY_POLL_MS = 10_000;

const clock = (iso: string) =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/** Rebuild the derived concept layer on demand, with live status.
 *
 *  Consolidation normally runs itself when writes go quiet; this is the "now" button for
 *  when you have just curated or ingested. The daemon owns every pass — this only queues a
 *  request and watches. A pass takes minutes (one pinned-Sonnet label call per community),
 *  so the in-flight state is not decoration: without it the button would look broken. */
export function ConceptRebuild({ onFinished }: { onFinished: () => void }) {
  const [status, setStatus] = useState<ConsolidationStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  const wasBusyRef = useRef(false);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const refresh = useCallback(() => {
    fetchConsolidationStatus()
      .then((s) => {
        if (!aliveRef.current) return;
        setStatus(s);
        // A pass we were watching has ended — pull the new layer in.
        const busy = s.state === "running" || s.state === "queued";
        if (wasBusyRef.current && !busy) onFinished();
        wasBusyRef.current = busy;
      })
      .catch(() => undefined);
  }, [onFinished]);

  useEffect(() => refresh(), [refresh]);

  // Primary signal: the 0041 triggers put concept_runs / concept_run_requests on the
  // shared change feed, so every transition pushes instead of being polled for.
  useChangeFeed((tables) => {
    if (tables.has("concept_runs") || tables.has("concept_run_requests")) refresh();
  });

  const state = status?.state ?? "idle";
  const busy = state === "running" || state === "queued";

  useEffect(() => {
    if (!busy) return;
    const timer = window.setInterval(refresh, SAFETY_POLL_MS);
    return () => window.clearInterval(timer);
  }, [busy, refresh]);

  useEffect(() => {
    if (state !== "queued") return;
    const timer = window.setTimeout(refresh, STALL_RECHECK_MS);
    return () => window.clearTimeout(timer);
  }, [state, refresh]);

  const start = async () => {
    setError(null);
    try {
      const s = await requestConsolidation();
      if (!aliveRef.current) return;
      setStatus(s);
      wasBusyRef.current = s.state === "running" || s.state === "queued";
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not queue a rebuild");
    }
  };

  const label =
    state === "running" ? "Consolidating…" : state === "queued" ? "Queued…" : "Rebuild concepts";

  return (
    <div className="concept-rebuild">
      <button
        className="btn"
        onClick={start}
        disabled={busy}
        aria-busy={busy}
      >
        {label}
      </button>
      <div className="concept-rebuild-note" role="status" aria-live="polite">
        {error ? (
          <span className="warn">{error}</span>
        ) : (
          <Note status={status} />
        )}
      </div>
    </div>
  );
}

function Note({ status }: { status: ConsolidationStatus | null }) {
  if (!status) return null;
  if (status.state === "running" && status.running) {
    return (
      <>
        started {clock(status.running.started_at)}
        {status.running.phase ? ` · ${status.running.phase}` : null}
      </>
    );
  }
  if (status.state === "queued") return <>waiting for the consolidation daemon</>;
  if (status.state === "stalled") {
    return (
      <span className="warn">
        daemon not responding
        {status.request ? ` — queued at ${clock(status.request.requested_at)}` : null}
      </span>
    );
  }
  const last = status.last;
  if (!last) return <>never run</>;
  if (last.status === "failed") {
    const why = last.error ?? last.label_error ?? last.embed_error;
    return <span className="warn">last run failed{why ? `: ${why}` : ""}</span>;
  }
  return (
    <>
      last run {clock(last.finished_at)} · {last.kept} concepts
      {last.created || last.updated || last.retired
        ? ` · +${last.created ?? 0} new, ${last.updated ?? 0} updated, ${last.retired ?? 0} retired`
        : null}
      {status.backlog > 0 ? ` · ${status.backlog} memories still enriching` : null}
      {last.label_failures || last.embed_failures ? (
        <span className="warn">
          {" "}
          · {last.label_failures ?? 0} label / {last.embed_failures ?? 0} embed failure(s)
        </span>
      ) : null}
    </>
  );
}
