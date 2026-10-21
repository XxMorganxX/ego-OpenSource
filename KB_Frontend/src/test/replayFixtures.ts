import type { ReplayLabel, ReplayRecord } from "../types";

/** a: keyword-only · b: both arms · c: meaning-only · limit 2 → c is cut. */
export const HYBRID: ReplayRecord = {
  v: 1,
  limit: 2,
  flat: {
    fts_query: "postgres",
    or_fallback: false,
    fts: [{ id: "a", score: 0.4 }, { id: "b", score: 0.3 }],
    knn: [{ id: "b", sim: 0.9 }, { id: "c", sim: 0.5 }],
    fused: [{ id: "b", rrf: 0.032 }, { id: "a", rrf: 0.016 }, { id: "c", rrf: 0.016 }],
    embedder: "ok",
  },
};

/** HYBRID plus one hop: x admitted, y rejected, both under concept k1. */
export const CRAWL: ReplayRecord = {
  ...HYBRID,
  crawl: {
    entry: [{ kind: "concept", id: "k1", label: "Postgres", via: "label-cosine", rank: 1 }],
    hops: [{
      hop: 1, by: "none", note: "deterministic: first 1 of 2 offered",
      offered_count: 2, admitted_count: 1,
      offered: [
        { id: "x", family: "concept", admitted: true,
          parents: [{ kind: "concept", id: "k1", label: "Postgres" }] },
        { id: "y", family: "concept", admitted: false,
          parents: [{ kind: "concept", id: "k1", label: "Postgres" }] },
      ],
    }],
    lists: { flat: ["b", "a", "c"], sim: ["b", "x", "c"], admit: [], corrob: [] },
    final: [
      { id: "b", rrf: 0.05, sim: 0.9 }, { id: "x", rrf: 0.03, sim: 0.7 },
      { id: "a", rrf: 0.02, sim: null }, { id: "c", rrf: 0.01, sim: 0.5 },
    ],
    stop_reason: "judge-stop",
  },
};

/** A pre-capture search_memories trace: final results only. */
export const PARTIAL: ReplayRecord = {
  v: 1, limit: 2, flat: { fused: [{ id: "a", sim: 0.8 }, { id: "b" }] },
};

const label = (title: string, deleted = false): ReplayLabel =>
  ({ title, path: deleted ? null : `notes.${title}`, doc_kind: null, deleted });

export const LABELS: Record<string, ReplayLabel> = {
  a: label("Alpha"), b: label("Beta"), c: label("Gamma"),
  x: label("Crawled"), y: label("(deleted)", true),
};
