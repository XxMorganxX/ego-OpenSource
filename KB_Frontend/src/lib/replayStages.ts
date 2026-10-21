import type { ReplayRecord } from "../types";
import { hubId } from "./replayLayout";
import { outcomeText, replayOutcome } from "./replayTrace";

/**
 * Turns one replay record into the ordered stages the player steps through.
 * Visibility is CUMULATIVE — each stage shows everything earlier
 * stages revealed plus its own additions. A stage whose data was never recorded
 * (a partial replay) stays in the list, unavailable, so the stage bar shows the gap.
 */
export type StageId = "query" | "keyword" | "meaning" | "merge" | "entry" | "hop" | "fusion" | "cut";

export const NOT_RECORDED = "not recorded (traced before replay capture)";
const NO_ROUTE = "no concept/entity route — flat only";
const FILTER_KEYS = ["under", "source", "type", "tags", "kind", "since", "until"] as const;

export interface StripRow {
  id: string;
  rank: number;
  chips: string[];
  cut: boolean;
}

export interface Stage {
  id: StageId;
  key: string;
  title: string;
  caption: string;
  available: boolean;
  nodes: ReadonlySet<string>;
  edges: ReadonlySet<string>;
  highlight: ReadonlySet<string>;
  dimmed: ReadonlySet<string>;
  strip: StripRow[];
  limitLine: number | null;
}

export const posMap = (ids: string[]) => new Map(ids.map((id, i) => [id, i + 1] as const));

export function filterSummary(args: Record<string, unknown>): string {
  const parts = FILTER_KEYS.filter((k) => args[k] != null).map((k) => {
    const v = args[k];
    return `${k}=${Array.isArray(v) ? v.join(",") : String(v)}`;
  });
  if (args.include_dead === false) parts.push("include_dead=false");
  return parts.join(", ");
}

type Draft = Pick<Stage, "id" | "key" | "title" | "caption" | "available"> &
  Partial<Pick<Stage, "highlight" | "dimmed" | "strip" | "limitLine">> & {
    addNodes?: string[];
    addEdges?: string[];
  };

export function buildStages(rec: ReplayRecord, args: Record<string, unknown>): Stage[] {
  const { flat, crawl, limit } = rec;
  const fts = flat.fts?.map((r) => r.id);
  const knn = flat.knn?.map((r) => r.id);
  const fused = flat.fused.map((r) => r.id);
  const final = crawl?.final?.map((r) => r.id);
  const page = final ?? fused;
  const kwPos = posMap(fts ?? []);
  const meanPos = posMap(knn ?? []);
  const admitted = new Set<string>();
  const rejected = new Set<string>();
  for (const h of crawl?.hops ?? [])
    for (const o of h.offered ?? []) (o.admitted ? admitted : rejected).add(o.id);

  const chips = (id: string) => {
    const c: string[] = [];
    if (kwPos.has(id)) c.push(`kw#${kwPos.get(id)}`);
    if (meanPos.has(id)) c.push(`mean#${meanPos.get(id)}`);
    if (admitted.has(id)) c.push("crawl");
    return c;
  };
  const rows = (ids: string[], chipFn: (id: string) => string[], cutAt: number | null = null): StripRow[] =>
    ids.map((id, i) => ({ id, rank: i + 1, chips: chipFn(id), cut: cutAt !== null && i >= cutAt }));

  const none: ReadonlySet<string> = new Set();
  const stages: Stage[] = [];
  let nodes = new Set<string>(["query"]);
  let edges = new Set<string>();
  const push = ({ addNodes = [], addEdges = [], ...s }: Draft) => {
    if (s.available) {
      nodes = new Set([...nodes, ...addNodes]);
      edges = new Set([...edges, ...addEdges]);
    }
    stages.push({ highlight: none, dimmed: none, strip: [], limitLine: null, ...s, nodes, edges });
  };

  const filters = filterSummary(args);
  push({
    id: "query", key: "query", title: "query", available: true,
    caption: [
      `searched "${flat.fts_query ?? String(args.query ?? "")}"`,
      flat.or_fallback ? "any-word fallback" : null,
      filters ? `filters: ${filters}` : null,
    ].filter(Boolean).join(" · "),
  });
  push({
    id: "keyword", key: "keyword", title: "keyword search", available: fts !== undefined,
    caption: fts ? `${fts.length} keyword hits` : NOT_RECORDED,
    addNodes: fts, addEdges: fts?.map((id) => `fts:${id}`),
    strip: fts ? rows(fts, (id) => [`kw#${kwPos.get(id)}`]) : [],
  });
  push({
    id: "meaning", key: "meaning", title: "meaning search", available: knn !== undefined,
    caption: knn === undefined ? NOT_RECORDED
      : flat.embedder === "unavailable" ? "meaning search skipped: embedder unavailable"
        : `${knn.length} meaning hits`,
    addNodes: knn, addEdges: knn?.map((id) => `knn:${id}`),
    strip: knn ? rows(knn, (id) => [`mean#${meanPos.get(id)}`]) : [],
  });
  const armsRecorded = fts !== undefined || knn !== undefined;
  const merged = rows(fused, chips);
  push({
    id: "merge", key: "merge", title: "merge (RRF)", available: armsRecorded,
    caption: armsRecorded ? `${fused.length} candidates merged (RRF)` : NOT_RECORDED,
    highlight: new Set((fts ?? []).filter((id) => meanPos.has(id))), strip: merged,
  });

  if (crawl) {
    const routed = crawl.entry.length > 0;
    push({
      id: "entry", key: "entry", title: "starting points", available: true,
      caption: routed ? crawl.entry.map((e) => `${e.label ?? e.id} (${e.via})`).join(", ") : NO_ROUTE,
      addNodes: crawl.entry.map((e) => hubId(e.id)),
      addEdges: crawl.entry.map((e) => `entry:${e.id}`), strip: merged,
    });
    if (!routed) {
      push({ id: "hop", key: "hop", title: "crawl hop", available: false, caption: `skipped: ${NO_ROUTE}` });
    } else {
      const dimmedSoFar = new Set<string>();
      for (const h of crawl.hops) {
        const offered = h.offered ?? [];
        for (const o of offered) if (!o.admitted) dimmedSoFar.add(o.id);
        push({
          id: "hop", key: `hop-${h.hop}`, title: `crawl hop ${h.hop}`, available: true,
          caption: `${h.offered_count} offered → ${h.admitted_count} admitted` +
            (h.note ? ` (${h.note})` : "") +
            (h.offered === undefined ? " · rows not recorded (traced before replay capture)" : ""),
          addNodes: offered.flatMap((o) => [...o.parents.map((p) => hubId(p.id)), o.id]),
          addEdges: offered.flatMap((o) => o.parents.map((p) => `parent:${p.id}:${o.id}`)),
          dimmed: new Set(dimmedSoFar), strip: merged,
        });
      }
    }
    const l = crawl.lists;
    const recorded = l !== undefined && final !== undefined;
    push({
      id: "fusion", key: "fusion", title: "final merge", available: routed && recorded,
      caption: !routed ? `skipped: ${NO_ROUTE}` : !recorded ? NOT_RECORDED
        : `lists: flat ${l!.flat.length} · sim ${l!.sim.length} · admit ${l!.admit.length}` +
          `${l!.admit.length === 0 ? " (empty: deterministic judge)" : ""} · corrob ${l!.corrob.length}`,
      highlight: new Set((final ?? []).slice(0, limit)), dimmed: rejected,
      strip: final ? rows(final, chips) : [],
    });
  }

  const top = new Set(page.slice(0, limit));
  push({
    id: "cut", key: "cut", title: "cut", available: true,
    caption: outcomeText(replayOutcome(rec)),
    addNodes: page, highlight: top,
    dimmed: new Set([...nodes, ...page].filter((id) => id !== "query" && !id.startsWith("hub:") && !top.has(id))),
    strip: rows(page, chips, limit), limitLine: page.length > limit ? limit : null,
  });
  return stages;
}
