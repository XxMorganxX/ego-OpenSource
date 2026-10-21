import { afterEach, describe, expect, it, vi } from "vitest";

import { browserTimeZone, HEAT_RAMP, heatFraction, heatStep } from "./heat";

describe("heatStep", () => {
  it("is -1 for zero and for an empty domain", () => {
    expect(heatStep(0, 10)).toBe(-1);
    expect(heatStep(3, 0)).toBe(-1);
  });
  it("puts the max in the top step and any hit in at least step 0", () => {
    expect(heatStep(10, 10)).toBe(HEAT_RAMP.length - 1);
    expect(heatStep(1, 1000)).toBe(0);
  });
  it("is monotone in value", () => {
    const steps = [1, 2, 4, 8, 16, 32].map((v) => heatStep(v, 32));
    expect([...steps].sort((a, b) => a - b)).toEqual(steps);
  });
  it("keeps heavy-tailed totals off the bottom step (log, not sqrt)", () => {
    // Heavy-tailed shape: a vault root of 2,000 dwarfs typical folders of 20–50 retrievals.
    expect(heatStep(50, 2000)).toBeGreaterThanOrEqual(2);
    expect(heatStep(20, 2000)).toBeGreaterThanOrEqual(1);
  });
});

describe("heatFraction", () => {
  it("is 0 for no hits, 1 at the max, and clamps above it", () => {
    expect(heatFraction(0, 100)).toBe(0);
    expect(heatFraction(100, 100)).toBe(1);
    expect(heatFraction(500, 100)).toBe(1);
    expect(heatFraction(5, 0)).toBe(0);
  });
});

describe("browserTimeZone", () => {
  afterEach(() => vi.restoreAllMocks());
  it("returns the resolved zone", () => {
    vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
      timeZone: "Europe/London",
    } as Intl.ResolvedDateTimeFormatOptions);
    expect(browserTimeZone()).toBe("Europe/London");
  });
  it("falls back to UTC", () => {
    vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue(
      {} as Intl.ResolvedDateTimeFormatOptions,
    );
    expect(browserTimeZone()).toBe("UTC");
  });
});
