import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchConcepts, fetchEntities, primeConcepts, primeEntities } from "./client";

function reply(status: number, body: unknown): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
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

describe("primed graph layers", () => {
  it("hands the primed entity payload to the next fetch without a second request", async () => {
    fetchMock.mockResolvedValueOnce(reply(200, { nodes: ["primed"], links: [] }));
    primeEntities();
    await expect(fetchEntities()).resolves.toEqual({ nodes: ["primed"], links: [] });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith("/api/entities");
  });

  it("uses a primed payload once: the following fetch goes back to the network", async () => {
    fetchMock
      .mockResolvedValueOnce(reply(200, { concepts: ["primed"] }))
      .mockResolvedValueOnce(reply(200, { concepts: ["fresh"] }));
    primeConcepts();
    await expect(fetchConcepts()).resolves.toEqual({ concepts: ["primed"] });
    await expect(fetchConcepts()).resolves.toEqual({ concepts: ["fresh"] });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("ignores a primed payload that has gone stale", async () => {
    fetchMock
      .mockResolvedValueOnce(reply(200, { concepts: ["primed"] }))
      .mockResolvedValueOnce(reply(200, { concepts: ["fresh"] }));
    primeConcepts();
    await vi.advanceTimersByTimeAsync(61_000);
    await expect(fetchConcepts()).resolves.toEqual({ concepts: ["fresh"] });
  });

  it("falls back to a real request when the primed one failed", async () => {
    fetchMock
      .mockResolvedValueOnce(reply(503, "unavailable"))
      .mockResolvedValueOnce(reply(200, { nodes: ["fresh"], links: [] }));
    primeEntities();
    await expect(fetchEntities()).resolves.toEqual({ nodes: ["fresh"], links: [] });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
