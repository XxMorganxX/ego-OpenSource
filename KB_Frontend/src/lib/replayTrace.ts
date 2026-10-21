import type { ReplayRecord } from "../types";
import type { StageId } from "./replayStages";

/**
 * Per-memory and per-search answers drawn from one replay record: where a single memory
 * stood at every stage (`tracePath`), and where the returned page came from (`replayOutcome`).
 * Both read only what the search recorded; an unrecorded stage says so instead of guessing.
 */
export interface PathStep {
  /** The player stage this step belongs to; every crawl hop maps to "hop". */
  stage: StageId;
  title: string;
  text: string;
  status: "hit" | "miss" | "unrecorded";
}

export type Origin = "both" | "meaning" | "keyword" | "crawl" | "unrecorded";

export interface Outcome {
  returned: number;
  cut: number;
  origins: Record<Origin, number>;
  /** What the crawl did to the page; null for a flat search or an unrecorded final. */
  crawl: { changed: number; admitted: number; admittedOnPage: number } | null;
}

const UNRECORDED = "not recorded";
const pos = (ids: string[], id: string) => ids.indexOf(id) + 1;
const num = (v: number | null | undefined, d: number) => (v == null ? "—" : v.toFixed(d));

const page = (rec: ReplayRecord) => (rec.crawl?.final ?? rec.flat.fused).map((r) => r.id);

function admittedIds(rec: ReplayRecord): Set<string> {
  const s = new Set<string>();
  for (const h of rec.crawl?.hops ?? []) for (const o of h.offered ?? []) if (o.admitted) s.add(o.id);
  return s;
}

export function tracePath(rec: ReplayRecord, id: string): PathStep[] {
  const { flat, crawl, limit } = rec;
  const steps: PathStep[] = [];
  const fts = flat.fts?.map((r) => r.id);
  const knn = flat.knn?.map((r) => r.id);

  if (fts === undefined) steps.push({ stage: "keyword", title: "keyword", text: UNRECORDED, status: "unrecorded" });
  else {
    const p = pos(fts, id);
    steps.push({ stage: "keyword", title: "keyword", text: p ? `#${p} of ${fts.length}` : "not found",
                 status: p ? "hit" : "miss" });
  }

  if (knn === undefined) steps.push({ stage: "meaning", title: "meaning", text: UNRECORDED, status: "unrecorded" });
  else if (flat.embedder === "unavailable")
    steps.push({ stage: "meaning", title: "meaning", text: "skipped: embedder unavailable", status: "miss" });
  else {
    const p = pos(knn, id);
    steps.push({
      stage: "meaning", title: "meaning",
      text: p ? `#${p} of ${knn.length} (sim ${num(flat.knn![p - 1].sim, 3)})` : `not in top ${knn.length}`,
      status: p ? "hit" : "miss",
    });
  }

  // With neither arm recorded, `fused` is only the returned page, not the merge.
  if (fts === undefined && knn === undefined)
    steps.push({ stage: "merge", title: "merge", text: UNRECORDED, status: "unrecorded" });
  else {
    const fused = flat.fused.map((r) => r.id);
    const p = pos(fused, id);
    steps.push({
      stage: "merge", title: "merge",
      text: p ? `#${p} of ${fused.length} (rrf ${num(flat.fused[p - 1].rrf, 4)})` : "not in the merge",
      status: p ? "hit" : "miss",
    });
  }

  if (crawl) {
    const hops = crawl.hops;
    const hit = hops.flatMap((h) => (h.offered ?? []).filter((o) => o.id === id).map((o) => ({ h, o })))[0];
    if (crawl.entry.length === 0)
      steps.push({ stage: "hop", title: "crawl", text: "no crawl: no concept/entity route", status: "miss" });
    else if (hit)
      steps.push({
        stage: "hop", title: "crawl",
        text: `offered in hop ${hit.h.hop} via ${hit.o.parents.map((p) => p.label ?? p.id).join(", ")} — ` +
          (hit.o.admitted ? "admitted" : "rejected"),
        status: hit.o.admitted ? "hit" : "miss",
      });
    else if (hops.some((h) => h.offered === undefined))
      steps.push({ stage: "hop", title: "crawl", text: UNRECORDED, status: "unrecorded" });
    else steps.push({ stage: "hop", title: "crawl", text: "not offered", status: "miss" });

    const l = crawl.lists;
    if (!l) steps.push({ stage: "fusion", title: "final merge", text: UNRECORDED, status: "unrecorded" });
    else {
      const keys = ["flat", "sim", "admit", "corrob"] as const;
      const inAny = keys.some((k) => l[k].includes(id));
      steps.push({
        stage: "fusion", title: "final merge",
        text: inAny ? keys.map((k) => `${k} ${pos(l[k], id) ? `#${pos(l[k], id)}` : "—"}`).join(" · ")
          : "in none of the four lists",
        status: inAny ? "hit" : "miss",
      });
    }
  }

  if (crawl && !crawl.final)
    steps.push({ stage: "cut", title: "outcome", text: UNRECORDED, status: "unrecorded" });
  else {
    const p = pos(page(rec), id);
    const past = p - limit;
    steps.push({
      stage: "cut", title: "outcome",
      text: !p ? "not ranked" : past <= 0 ? `returned at #${p}`
        : `cut at #${p}, ${past} place${past === 1 ? "" : "s"} past the limit (${limit})`,
      status: p && past <= 0 ? "hit" : "miss",
    });
  }
  return steps;
}

export function replayOutcome(rec: ReplayRecord): Outcome {
  const ids = page(rec);
  const top = ids.slice(0, rec.limit);
  const fts = rec.flat.fts ? new Set(rec.flat.fts.map((r) => r.id)) : null;
  const knn = rec.flat.knn ? new Set(rec.flat.knn.map((r) => r.id)) : null;
  const fused = new Set(rec.flat.fused.map((r) => r.id));
  const origins: Record<Origin, number> = { both: 0, meaning: 0, keyword: 0, crawl: 0, unrecorded: 0 };
  for (const id of top) {
    const k = fts?.has(id) ?? false;
    const m = knn?.has(id) ?? false;
    const o: Origin = k && m ? "both" : m ? "meaning" : k ? "keyword"
      : fts === null && knn === null ? "unrecorded"
        : rec.crawl && !fused.has(id) ? "crawl" : "unrecorded";
    origins[o] += 1;
  }
  let crawl: Outcome["crawl"] = null;
  if (rec.crawl?.final) {
    const flatTop = new Set(rec.flat.fused.slice(0, rec.limit).map((r) => r.id));
    const admitted = admittedIds(rec);
    crawl = {
      changed: top.filter((id) => !flatTop.has(id)).length,
      admitted: admitted.size,
      admittedOnPage: top.filter((id) => admitted.has(id)).length,
    };
  }
  return { returned: top.length, cut: Math.max(0, ids.length - rec.limit), origins, crawl };
}

/** One line for the cut stage: e.g. "top 10: 1 both · 9 meaning only · 0 from crawl · …". */
export function outcomeText(o: Outcome): string {
  if (o.returned === 0) return "nothing returned";
  const parts: string[] = [];
  if (o.origins.both) parts.push(`${o.origins.both} both`);
  if (o.origins.meaning) parts.push(`${o.origins.meaning} meaning only`);
  if (o.origins.keyword) parts.push(`${o.origins.keyword} keyword only`);
  if (o.crawl || o.origins.crawl) parts.push(`${o.origins.crawl} from crawl`);
  if (o.origins.unrecorded) parts.push(`${o.origins.unrecorded} origin not recorded`);
  if (o.crawl) {
    const { changed, admitted, admittedOnPage } = o.crawl;
    // `changed` is the final merge's doing (it re-fuses flat with the crawl's lists), not
    // only the crawl's own rows -- so the two facts are stated apart.
    parts.push(`final merge swapped ${changed} of ${o.returned}`);
    parts.push(admitted === 0 ? "crawl admitted none"
      : `crawl admitted ${admitted}, ${admittedOnPage === 0 ? "none" : admittedOnPage} reached the page`);
  }
  parts.push(`${o.cut} cut`);
  return `top ${o.returned}: ${parts.join(" · ")}`;
}
