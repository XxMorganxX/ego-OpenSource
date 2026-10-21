import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { UNKNOWN_ME } from "../lib/identity";
import { fetchMe } from "./client";

const OWNER = { email: "m@x", tenant: "owner", is_owner: true };

function reply(status: number, body: string, redirected = false): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    redirected,
    text: () => Promise.resolve(body),
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("fetchMe", () => {
  it("retries a 503 and settles on the identity the next attempt returns", async () => {
    fetchMock
      .mockResolvedValueOnce(reply(503, "unavailable"))
      .mockResolvedValueOnce(reply(200, JSON.stringify(OWNER)));
    const p = fetchMe();
    await vi.advanceTimersByTimeAsync(1_000);
    await expect(p).resolves.toEqual(OWNER);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("fails closed after retrying 1 s, 3 s and 9 s", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const p = fetchMe();
    await vi.advanceTimersByTimeAsync(1_000 + 3_000 + 9_000);
    await expect(p).resolves.toBe(UNKNOWN_ME);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("reads a 404 as a pre-tenancy backend (owner), without retrying", async () => {
    fetchMock.mockResolvedValue(reply(404, "Not Found"));
    await expect(fetchMe()).resolves.toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reads a 200 non-JSON body as the SPA fallback (owner), unless it came via a redirect", async () => {
    fetchMock.mockResolvedValueOnce(reply(200, "<!doctype html>"));
    await expect(fetchMe()).resolves.toBeNull();
    fetchMock.mockResolvedValueOnce(reply(200, "<html>Sign in</html>", true));
    await expect(fetchMe()).resolves.toBe(UNKNOWN_ME);
  });

  it("settles a 401/403 or a malformed answer as unknown at once", async () => {
    for (const r of [reply(403, "owner only"), reply(401, ""), reply(200, '{"email":1}')]) {
      fetchMock.mockReset().mockResolvedValue(r);
      await expect(fetchMe()).resolves.toBe(UNKNOWN_ME);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    }
  });

  it("aborts a hung request after 10 s and fails closed once retries are spent", async () => {
    const signals: AbortSignal[] = [];
    fetchMock.mockImplementation((_url: string, init: RequestInit) => {
      const signal = init.signal as AbortSignal;
      signals.push(signal);
      return new Promise((_resolve, reject) =>
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))),
      );
    });
    const p = fetchMe();
    await vi.advanceTimersByTimeAsync(4 * 10_000 + 1_000 + 3_000 + 9_000);
    await expect(p).resolves.toBe(UNKNOWN_ME);
    expect(signals).toHaveLength(4);
    expect(signals.every((s) => s.aborted)).toBe(true);
  });
});
