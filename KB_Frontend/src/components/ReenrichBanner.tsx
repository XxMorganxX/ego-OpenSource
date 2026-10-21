import { useCallback, useEffect, useRef, useState } from "react";

import { fetchIngestStatus } from "../api/client";
import { useChangeFeed } from "../hooks/useChangeFeed";

/** A small live indicator of background re-enrichment: how many memories are queued
 *  ('pending') for the worker. Re-checks when nodes change (enrichment writes node rows),
 *  so curation actions that requeue voters (merge/prune) are visibly reflected. Renders
 *  nothing when the queue is empty. */
export function ReenrichBanner() {
  const [pending, setPending] = useState(0);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const refresh = useCallback(() => {
    fetchIngestStatus()
      .then((s) => {
        if (aliveRef.current) setPending(s.enrichment.pending ?? 0);
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => refresh(), [refresh]);

  // Re-check the queue only when nodes change, via the shared change feed — not on
  // every unrelated DB event.
  useChangeFeed((tables) => {
    if (tables.has("nodes")) refresh();
  });

  if (pending <= 0) return null;
  return (
    <div className="reenrich-banner" role="status">
      {pending === 1 ? "1 memory re-enriching…" : `${pending} memories re-enriching…`}
    </div>
  );
}
