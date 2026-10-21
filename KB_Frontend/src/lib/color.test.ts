import { describe, expect, it } from "vitest";

import {
  importanceColor,
  importanceDomain,
  normalize,
  recencyColor,
  updatedDomain,
  withAlpha,
} from "./color";

describe("importanceDomain", () => {
  it("returns the min and max of the values", () => {
    expect(importanceDomain([1, 4, 2, 3])).toEqual([1, 4]);
  });
  it("defaults to [0, 1] when empty", () => {
    expect(importanceDomain([])).toEqual([0, 1]);
  });
});

describe("normalize", () => {
  it("maps value to [0,1] across the range", () => {
    expect(normalize(2, 0, 4)).toBe(0.5);
    expect(normalize(0, 0, 4)).toBe(0);
    expect(normalize(4, 0, 4)).toBe(1);
  });
  it("clamps out-of-range values", () => {
    expect(normalize(-5, 0, 4)).toBe(0);
    expect(normalize(9, 0, 4)).toBe(1);
  });
  it("returns 0.5 for a degenerate range", () => {
    expect(normalize(3, 3, 3)).toBe(0.5);
  });
});

describe("importanceColor", () => {
  it("paints the most important node red and the least purple", () => {
    expect(importanceColor(4, 1, 4)).toBe("hsl(0, 85%, 60%)"); // max -> red
    expect(importanceColor(1, 1, 4)).toBe("hsl(280, 85%, 60%)"); // min -> purple
  });
  it("places mid-importance in the green band", () => {
    expect(importanceColor(2.5, 1, 4)).toBe("hsl(140, 85%, 60%)");
  });
});

describe("updatedDomain", () => {
  it("returns the min and max epoch ms of parseable timestamps", () => {
    expect(
      updatedDomain(["2026-06-12T12:00:00Z", "2026-08-03T12:00:00Z", "2026-07-01T00:00:00Z"]),
    ).toEqual([Date.parse("2026-06-12T12:00:00Z"), Date.parse("2026-08-03T12:00:00Z")]);
  });
  it("ignores missing and unparseable values", () => {
    expect(updatedDomain([undefined, null, "not a date", "2026-06-12T12:00:00Z"])).toEqual([
      Date.parse("2026-06-12T12:00:00Z"),
      Date.parse("2026-06-12T12:00:00Z"),
    ]);
  });
  it("defaults to [0, 1] when nothing parses", () => {
    expect(updatedDomain([undefined, "nope"])).toEqual([0, 1]);
  });
});

describe("recencyColor", () => {
  const min = Date.parse("2026-06-01T00:00:00Z");
  const max = Date.parse("2026-08-01T00:00:00Z");

  it("paints the newest hot amber and the oldest dim slate-blue", () => {
    expect(recencyColor(max, min, max)).toBe("hsl(30, 95%, 60%)");
    expect(recencyColor(min, min, max)).toBe("hsl(220, 25%, 35%)");
  });
  it("passes through magenta (never green) midway", () => {
    expect(recencyColor((min + max) / 2, min, max)).toBe("hsl(305, 60%, 48%)");
  });
  it("renders mid-ramp when the domain is degenerate", () => {
    expect(recencyColor(min, min, min)).toBe("hsl(305, 60%, 48%)");
  });
});

describe("withAlpha", () => {
  it("scales the alpha of an rgba() color", () => {
    expect(withAlpha("rgba(255,255,255,0.16)", 0.5)).toBe("rgba(255,255,255,0.08)");
  });
  it("converts a #rrggbb hex color to rgba at the given alpha", () => {
    expect(withAlpha("#6ea8ff", 0.25)).toBe("rgba(110,168,255,0.25)");
  });
  it("is the identity at factor 1 for hex-equivalent opacity", () => {
    expect(withAlpha("rgba(10,20,30,0.5)", 1)).toBe("rgba(10,20,30,0.5)");
  });
  it("passes through colors it does not recognize", () => {
    expect(withAlpha("hsl(120, 85%, 60%)", 0.5)).toBe("hsl(120, 85%, 60%)");
  });
});
