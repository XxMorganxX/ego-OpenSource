import { useCallback, useEffect, useState } from "react";

import { getParam, setParams, subscribeUrl } from "../lib/url";

/**
 * Bind a piece of UI state to a `?key=` query param, so it survives refresh, is
 * shareable, and responds to the browser's back/forward buttons. The value is a
 * string union `T`; setting it to `fallback` removes the param (clean default URLs).
 * Returns a `[value, setValue]` pair shaped like `useState`.
 */
export function useUrlParam<T extends string>(
  key: string,
  fallback: T,
): [T, (next: T) => void] {
  const read = useCallback((): T => (getParam(key) as T | null) ?? fallback, [key, fallback]);
  const [value, setValue] = useState<T>(read);

  useEffect(() => {
    const onChange = () => setValue(read());
    const unsub = subscribeUrl(onChange);
    window.addEventListener("popstate", onChange);
    onChange(); // re-sync in case the URL moved between render and effect
    return () => {
      unsub();
      window.removeEventListener("popstate", onChange);
    };
  }, [read]);

  const set = useCallback(
    (next: T) => setParams({ [key]: next === fallback ? null : next }),
    [key, fallback],
  );

  return [value, set];
}
