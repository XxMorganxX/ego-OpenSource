import { useEffect, useRef } from "react";

import { subscribeChanges } from "../api/live";

/**
 * Subscribe to the live DB change feed and invoke `onChange` at most once per
 * `debounceMs`, passing the set of tables that changed in the window. A full Obsidian
 * sync fires one notification per note, so coalescing turns that burst into a single
 * refetch. `onChange` is read through a ref, so changing it won't re-open the socket.
 */
export function useChangeFeed(
  onChange: (changedTables: Set<string>) => void,
  debounceMs = 250,
): void {
  const cb = useRef(onChange);
  cb.current = onChange;

  useEffect(() => {
    const pending = new Set<string>();
    let timer: number | undefined;
    const flush = () => {
      const tables = new Set(pending);
      pending.clear();
      cb.current(tables);
    };
    const unsubscribe = subscribeChanges((e) => {
      pending.add(e.table);
      window.clearTimeout(timer);
      timer = window.setTimeout(flush, debounceMs);
    });
    return () => {
      window.clearTimeout(timer);
      unsubscribe();
    };
  }, [debounceMs]);
}
