import { useCallback, useEffect, useRef, useState } from "react";

import { fetchRetrievalHeatmap } from "../api/client";
import type { RetrievedMemory } from "../lib/folderHeat";
import { browserTimeZone } from "../lib/heat";
import { useChangeFeed } from "./useChangeFeed";

// The server's row cap. Rows come back most-retrieved first, so anything past the cap is
// the least-retrieved tail — those memories would drop out of their folders' totals.
const MAX_ROWS = 1000;

/**
 * Every memory agents retrieved, with its all-time surfaced/opened counts and path, for the
 * graph's "By retrieval" folder heat. Fetched only while `enabled` (the scheme is active),
 * refreshed live when new MCP traces land; null until the first response. Only the newest
 * request may land.
 */
export function useRetrievalCounts(enabled: boolean): RetrievedMemory[] | null {
  const [memories, setMemories] = useState<RetrievedMemory[] | null>(null);
  const generation = useRef(0);

  const load = useCallback(async () => {
    const gen = ++generation.current;
    try {
      const data = await fetchRetrievalHeatmap({
        days: "all",
        bucket: "week",
        tz: browserTimeZone(),
        path: null,
        limit: MAX_ROWS,
      });
      if (gen !== generation.current) return;
      setMemories(
        data.rows.map(({ id, path, title, surfaced, opened }) => ({ id, path, title, surfaced, opened })),
      );
    } catch (e) {
      // Keep the last good counts; the next trace event retries.
      console.warn("retrieval counts could not be loaded", e);
    }
  }, []);

  useEffect(() => {
    if (enabled) load();
    else generation.current++; // drop any in-flight response
  }, [enabled, load]);

  useChangeFeed((tables) => {
    if (enabled && tables.has("mcp_trace")) load();
  });

  return enabled ? memories : null;
}
