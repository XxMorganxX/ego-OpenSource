import { beforeEach, describe, expect, it } from "vitest";

import {
  clampGravity,
  clampSpacing,
  DEFAULT_GRAPH_SETTINGS,
  forceParams,
  GRAVITY_MAX,
  GRAVITY_MIN,
  gravityStrength,
  loadSettings,
  saveSettings,
  SPACING_MAX,
  SPACING_MIN,
} from "./settings";

describe("clampSpacing", () => {
  it("keeps values inside the allowed range", () => {
    expect(clampSpacing(SPACING_MIN - 50)).toBe(SPACING_MIN);
    expect(clampSpacing(SPACING_MAX + 50)).toBe(SPACING_MAX);
    expect(clampSpacing(60)).toBe(60);
  });

  it("falls back to the default for non-finite input", () => {
    expect(clampSpacing(Number.NaN)).toBe(DEFAULT_GRAPH_SETTINGS.spacing);
  });
});

describe("clampGravity", () => {
  it("keeps signed values inside the allowed range and clamps both boundaries", () => {
    expect(GRAVITY_MIN).toBe(-5);
    expect(clampGravity(GRAVITY_MIN - 50)).toBe(GRAVITY_MIN);
    expect(clampGravity(GRAVITY_MAX + 50)).toBe(GRAVITY_MAX);
    expect(clampGravity(-6)).toBe(-5);
    expect(clampGravity(-3)).toBe(-3);
    expect(clampGravity(35)).toBe(35);
  });

  it("falls back to the default for non-finite input", () => {
    expect(clampGravity(Number.NaN)).toBe(DEFAULT_GRAPH_SETTINGS.gravity);
  });
});

describe("forceParams", () => {
  it("derives link distance, charge, and a capped charge reach from spacing", () => {
    expect(forceParams(40)).toEqual({
      linkDistance: 40,
      chargeStrength: -120,
      chargeDistanceMax: 120,
    });
  });
});

describe("gravityStrength", () => {
  it("scales the signed knob into a positional force strength", () => {
    expect(gravityStrength(-5)).toBeCloseTo(-0.01);
    expect(gravityStrength(0)).toBe(0);
    expect(gravityStrength(40)).toBeCloseTo(0.08);
    expect(gravityStrength(100)).toBeCloseTo(0.2);
  });

  it("clamps out-of-range input before scaling", () => {
    expect(gravityStrength(GRAVITY_MIN - 200)).toBeCloseTo(GRAVITY_MIN / 500);
    expect(gravityStrength(GRAVITY_MAX + 200)).toBeCloseTo(GRAVITY_MAX / 500);
  });
});

describe("settings persistence", () => {
  beforeEach(() => localStorage.clear());

  it("returns defaults when nothing is stored", () => {
    expect(loadSettings()).toEqual(DEFAULT_GRAPH_SETTINGS);
  });

  it("round-trips a saved value, clamping on load", () => {
    saveSettings({ spacing: 999, gravity: 30, colorScheme: "type" });
    expect(loadSettings().spacing).toBe(SPACING_MAX);
  });

  it("round-trips the gravity value, clamping on load", () => {
    saveSettings({ spacing: 50, gravity: 999, colorScheme: "type" });
    expect(loadSettings().gravity).toBe(GRAVITY_MAX);
  });

  it("round-trips an allowed negative gravity value", () => {
    saveSettings({ spacing: 50, gravity: -3, colorScheme: "type" });
    expect(loadSettings().gravity).toBe(-3);
  });

  it("clamps repulsion to negative five before persisting", () => {
    saveSettings({ spacing: 50, gravity: -75, colorScheme: "type" });
    expect(JSON.parse(localStorage.getItem("kb.graph-settings")!).gravity).toBe(-5);
    expect(loadSettings().gravity).toBe(-5);
  });

  it("round-trips the chosen color scheme", () => {
    saveSettings({ spacing: 50, gravity: 30, colorScheme: "depth" });
    expect(loadSettings().colorScheme).toBe("depth");
  });

  it("round-trips the recency scheme", () => {
    saveSettings({ spacing: 50, gravity: 30, colorScheme: "recency" });
    expect(loadSettings().colorScheme).toBe("recency");
  });

  it("round-trips the retrieval scheme", () => {
    saveSettings({ spacing: 50, gravity: 30, colorScheme: "retrieval" });
    expect(loadSettings().colorScheme).toBe("retrieval");
  });

  it("falls back to the default scheme for an unknown id", () => {
    localStorage.setItem(
      "kb.graph-settings",
      JSON.stringify({ spacing: 50, gravity: 30, colorScheme: "bogus" }),
    );
    expect(loadSettings().colorScheme).toBe(DEFAULT_GRAPH_SETTINGS.colorScheme);
  });

  it("migrates the legacy colorByImportance flag to the importance scheme", () => {
    localStorage.setItem(
      "kb.graph-settings",
      JSON.stringify({ spacing: 50, gravity: 30, colorByImportance: true }),
    );
    expect(loadSettings().colorScheme).toBe("importance");
  });

  it("recovers from corrupt storage", () => {
    localStorage.setItem("kb.graph-settings", "{not json");
    expect(loadSettings()).toEqual(DEFAULT_GRAPH_SETTINGS);
  });
});
