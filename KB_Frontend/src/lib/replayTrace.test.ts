import { describe, expect, it } from "vitest";

import { CRAWL, HYBRID, PARTIAL } from "../test/replayFixtures";
import type { ReplayRecord } from "../types";
import { outcomeText, replayOutcome, tracePath } from "./replayTrace";

const text = (rec: ReplayRecord, id: string) =>
  Object.fromEntries(tracePath(rec, id).map((s) => [s.title, s.text]));
const status = (rec: ReplayRecord, id: string) =>
  Object.fromEntries(tracePath(rec, id).map((s) => [s.title, s.status]));

describe("tracePath", () => {
  it("follows a keyword-only memory that the crawl pushed past the limit", () => {
    expect(text(CRAWL, "a")).toEqual({
      "keyword": "#1 of 2",
      "meaning": "not in top 2",
      "merge": "#2 of 3 (rrf 0.0160)",
      "crawl": "not offered",
      "final merge": "flat #2 · sim — · admit — · corrob —",
      "outcome": "cut at #3, 1 place past the limit (2)",
    });
    expect(status(CRAWL, "a")).toMatchObject({ keyword: "hit", meaning: "miss", outcome: "miss" });
  });

  it("follows a crawl-admitted memory onto the page", () => {
    expect(text(CRAWL, "x")).toMatchObject({
      "keyword": "not found",
      "merge": "not in the merge",
      "crawl": "offered in hop 1 via Postgres — admitted",
      "final merge": "flat — · sim #2 · admit — · corrob —",
      "outcome": "returned at #2",
    });
    expect(status(CRAWL, "x").outcome).toBe("hit");
  });

  it("follows a crawl-rejected memory that was never ranked", () => {
    expect(text(CRAWL, "y")).toMatchObject({
      "crawl": "offered in hop 1 via Postgres — rejected",
      "final merge": "in none of the four lists",
      "outcome": "not ranked",
    });
    expect(status(CRAWL, "y").crawl).toBe("miss");
  });

  it("reports the meaning-search similarity", () => {
    expect(text(HYBRID, "c").meaning).toBe("#2 of 2 (sim 0.500)");
  });

  it("has no crawl steps for a flat search", () => {
    expect(tracePath(HYBRID, "b").map((s) => s.title)).toEqual(["keyword", "meaning", "merge", "outcome"]);
  });

  it("marks stages a partial replay never recorded", () => {
    expect(status(PARTIAL, "a")).toEqual({
      keyword: "unrecorded", meaning: "unrecorded", merge: "unrecorded", outcome: "hit",
    });
    expect(text(PARTIAL, "a").outcome).toBe("returned at #1");
  });

  it("says when the meaning search was skipped", () => {
    const rec: ReplayRecord = { ...HYBRID, flat: { ...HYBRID.flat, knn: [], embedder: "unavailable" } };
    expect(text(rec, "a").meaning).toBe("skipped: embedder unavailable");
  });

  it("ties each step to the stage the player shows it on", () => {
    expect(tracePath(CRAWL, "x").map((s) => s.stage))
      .toEqual(["keyword", "meaning", "merge", "hop", "fusion", "cut"]);
  });
});

describe("replayOutcome", () => {
  it("counts where each returned row came from and what the crawl changed", () => {
    const o = replayOutcome(CRAWL);
    expect(o).toEqual({
      returned: 2, cut: 2,
      origins: { both: 1, meaning: 0, keyword: 0, crawl: 1, unrecorded: 0 },
      crawl: { changed: 1, admitted: 1, admittedOnPage: 1 },
    });
    expect(outcomeText(o)).toBe(
      "top 2: 1 both · 1 from crawl · final merge swapped 1 of 2 · crawl admitted 1, 1 reached the page · 2 cut");
  });

  it("leaves the crawl out of a flat search", () => {
    const o = replayOutcome(HYBRID);
    expect(o.crawl).toBeNull();
    expect(outcomeText(o)).toBe("top 2: 1 both · 1 keyword only · 1 cut");
  });

  it("says plainly when the crawl changed nothing", () => {
    const rec: ReplayRecord = {
      ...CRAWL,
      crawl: { ...CRAWL.crawl!, final: [{ id: "b" }, { id: "a" }, { id: "x" }, { id: "c" }] },
    };
    expect(outcomeText(replayOutcome(rec))).toBe(
      "top 2: 1 both · 1 keyword only · 0 from crawl · final merge swapped 0 of 2 · crawl admitted 1, none reached the page · 2 cut");
  });

  it("says when the crawl admitted nothing", () => {
    const hop = { ...CRAWL.crawl!.hops[0], admitted_count: 0,
                  offered: CRAWL.crawl!.hops[0].offered!.map((o) => ({ ...o, admitted: false })) };
    const rec: ReplayRecord = {
      ...CRAWL,
      crawl: { ...CRAWL.crawl!, hops: [hop], final: [{ id: "b" }, { id: "a" }] },
    };
    expect(outcomeText(replayOutcome(rec))).toContain("final merge swapped 0 of 2 · crawl admitted none");
  });

  it("cannot name origins the replay never recorded", () => {
    expect(outcomeText(replayOutcome(PARTIAL))).toBe("top 2: 2 origin not recorded · 0 cut");
  });

  it("says when nothing came back", () => {
    const rec: ReplayRecord = { v: 1, limit: 5, flat: { fts: [], knn: [], fused: [] } };
    expect(outcomeText(replayOutcome(rec))).toBe("nothing returned");
  });
});
