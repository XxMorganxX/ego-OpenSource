import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { useUrlParam } from "./useUrlParam";

describe("useUrlParam", () => {
  beforeEach(() => window.history.replaceState(null, "", "/"));

  it("falls back to the default when the param is absent", () => {
    const { result } = renderHook(() => useUrlParam<string>("view", "graph"));
    expect(result.current[0]).toBe("graph");
  });

  it("reads an existing value from the URL (deep link)", () => {
    window.history.replaceState(null, "", "/?view=database");
    const { result } = renderHook(() => useUrlParam<string>("view", "graph"));
    expect(result.current[0]).toBe("database");
  });

  it("writes the value to the URL and drops the param at its default", () => {
    const { result } = renderHook(() => useUrlParam<string>("view", "graph"));
    act(() => result.current[1]("ingest"));
    expect(new URLSearchParams(window.location.search).get("view")).toBe("ingest");
    expect(result.current[0]).toBe("ingest");
    act(() => result.current[1]("graph")); // back to default → param removed for a clean URL
    expect(window.location.search).toBe("");
    expect(result.current[0]).toBe("graph");
  });

  it("responds to back/forward navigation (popstate)", () => {
    const { result } = renderHook(() => useUrlParam<string>("view", "graph"));
    act(() => {
      window.history.replaceState(null, "", "/?view=merges");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(result.current[0]).toBe("merges");
  });
});
