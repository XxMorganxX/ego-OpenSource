import { describe, expect, it } from "vitest";

import { SPRITE_MAX_ZOOM, spriteDensity, zoomBucket } from "./nodeSprites";

describe("zoomBucket", () => {
  it("quantizes a zoom up to the next power of two", () => {
    expect(zoomBucket(0.9)).toBe(1);
    expect(zoomBucket(1)).toBe(1);
    expect(zoomBucket(1.5)).toBe(2);
    expect(zoomBucket(2)).toBe(2);
    expect(zoomBucket(3)).toBe(4);
    expect(zoomBucket(5)).toBe(8);
  });

  it("never buckets BELOW the live zoom (sprites must not upscale within a bucket)", () => {
    for (const k of [0.3, 0.7, 1.1, 2.2, 6.5, 15]) {
      expect(zoomBucket(k)).toBeGreaterThanOrEqual(k);
    }
  });

  it("clamps to the supported bucket range", () => {
    expect(zoomBucket(0.001)).toBe(0.25);
    expect(zoomBucket(500)).toBe(SPRITE_MAX_ZOOM);
  });

  it("falls back to 1 for degenerate zooms", () => {
    expect(zoomBucket(0)).toBe(1);
    expect(zoomBucket(-3)).toBe(1);
    expect(zoomBucket(NaN)).toBe(1);
  });
});

describe("spriteDensity", () => {
  it("scales the bucket by devicePixelRatio", () => {
    const dpr = window.devicePixelRatio || 1;
    expect(spriteDensity(1)).toBe(dpr * 1);
    expect(spriteDensity(3)).toBe(dpr * 4);
  });

  it("steps between buckets so a zoom change re-resolves sprites", () => {
    expect(spriteDensity(1.9)).toBe(spriteDensity(1.2)); // same bucket → cache hit
    expect(spriteDensity(2.1)).not.toBe(spriteDensity(1.9)); // crossed a bucket
  });
});
