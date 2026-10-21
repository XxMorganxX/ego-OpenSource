import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { STAGE_MS, nextAvailable, useReplayPlayer } from "./useReplayPlayer";

const AV = [true, false, true, true];

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("nextAvailable", () => {
  it("skips unavailable stages and stays put at either end", () => {
    expect(nextAvailable(AV, 0, 1)).toBe(2);
    expect(nextAvailable(AV, 2, -1)).toBe(0);
    expect(nextAvailable(AV, 3, 1)).toBe(3);
    expect(nextAvailable(AV, 0, -1)).toBe(0);
  });
});

describe("useReplayPlayer", () => {
  it("steps forward and back over unavailable stages", () => {
    const { result } = renderHook(() => useReplayPlayer(AV));
    expect(result.current.index).toBe(0);
    act(() => result.current.next());
    expect(result.current.index).toBe(2);
    act(() => result.current.prev());
    expect(result.current.index).toBe(0);
  });

  it("goTo ignores an unavailable stage", () => {
    const { result } = renderHook(() => useReplayPlayer(AV));
    act(() => result.current.goTo(1));
    expect(result.current.index).toBe(0);
    act(() => result.current.goTo(3));
    expect(result.current.index).toBe(3);
  });

  it("plays through the available stages, then stops", () => {
    const { result } = renderHook(() => useReplayPlayer(AV));
    act(() => result.current.play());
    expect(result.current.playing).toBe(true);
    act(() => vi.advanceTimersByTime(STAGE_MS));
    expect(result.current.index).toBe(2);
    act(() => vi.advanceTimersByTime(STAGE_MS));
    expect(result.current.index).toBe(3);
    act(() => vi.advanceTimersByTime(STAGE_MS));
    expect(result.current.playing).toBe(false);
  });

  it("2× speed halves the stage time", () => {
    const { result } = renderHook(() => useReplayPlayer(AV));
    act(() => result.current.setSpeed(2));
    act(() => result.current.play());
    act(() => vi.advanceTimersByTime(STAGE_MS / 2));
    expect(result.current.index).toBe(2);
  });

  it("play at the last stage restarts from the first", () => {
    const { result } = renderHook(() => useReplayPlayer(AV));
    act(() => result.current.goTo(3));
    act(() => result.current.play());
    expect(result.current.index).toBe(0);
  });

  it("pause stops advancing", () => {
    const { result } = renderHook(() => useReplayPlayer(AV));
    act(() => result.current.play());
    act(() => result.current.pause());
    act(() => vi.advanceTimersByTime(STAGE_MS * 3));
    expect(result.current.index).toBe(0);
  });

  it("moves onto the first available stage when the stage list loads", () => {
    const { result, rerender } = renderHook(({ av }) => useReplayPlayer(av),
      { initialProps: { av: [] as boolean[] } });
    rerender({ av: [false, true, true] });
    expect(result.current.index).toBe(1);
  });
});
