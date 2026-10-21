import { useEffect, useState } from "react";

const canMatch = () =>
  typeof window !== "undefined" && typeof window.matchMedia === "function";

/** Reactive `window.matchMedia`. Environments without matchMedia (jsdom) report false. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () => canMatch() && window.matchMedia(query).matches,
  );
  useEffect(() => {
    if (!canMatch()) return;
    const mql = window.matchMedia(query);
    setMatches(mql.matches);
    const onChange = (event: MediaQueryListEvent) => setMatches(event.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

/** Breakpoint below which the dashboard collapses to the graph-only mobile layout.
    Must stay in sync with the mobile block in index.css. */
export const MOBILE_QUERY = "(max-width: 760px)";

export function useIsMobile(): boolean {
  return useMediaQuery(MOBILE_QUERY);
}
