import type { IngestEvent } from "../types";

/** The ingest_log event the failures funnel filters to. */
export const FAILED_EVENT = "enrichment_failed";

/** Human labels for ingest_log event types. */
export const EVENT_LABELS: Record<string, string> = {
  node_created: "created",
  node_updated: "updated",
  node_deleted: "deleted",
  enrichment_done: "enriched",
  enrichment_deferred: "deferred",
  enrichment_failed: "failed",
  // A record the engine refused to store (pull or push) — NOT an enrichment failure, so
  // FAILED_EVENT / groupFailures deliberately ignore it: retry cannot fix it.
  ingest_failed: "ingest failed",
  ingest_resolved: "ingest resolved",
};

/** Order event-type rollup chips by pipeline relevance; failures last so they stand out. */
const EVENT_ORDER = [
  "enrichment_done",
  "node_created",
  "node_updated",
  "node_deleted",
  "enrichment_deferred",
  "enrichment_failed",
  "ingest_failed",
  "ingest_resolved",
];

/** The raw error string an event carries in its detail, if any. */
export function errorText(e: IngestEvent): string | null {
  const err = (e.detail as { error?: unknown } | null)?.error;
  return typeof err === "string" && err.length > 0 ? err : null;
}

export type ErrorClass = {
  /** Stable bucket key for grouping. */
  key: string;
  /** Short human label, e.g. "Inference timeout". */
  label: string;
  /** Whether re-queuing can plausibly succeed (transient) vs needs a fix first (terminal). */
  retryable: boolean;
};

/**
 * Bucket a failure event by its error family and decide whether retrying can help.
 * Transient infrastructure errors (inference timeouts) are retryable; structural ones
 * (duplicate-slug constraint violations) are terminal until the underlying data is fixed —
 * re-queuing them just loops. Unknown errors default to retryable so a new transient class
 * isn't stranded behind a "needs fix" label.
 */
export function classifyError(e: IngestEvent): ErrorClass | null {
  const text = errorText(e);
  if (!text) return null;
  const type = text.split(":", 1)[0].trim();
  if (type === "InferenceError") {
    return {
      key: "inference",
      label: /tim(e|ed)\s*out|timeout/i.test(text) ? "Inference timeout" : "Inference error",
      retryable: true,
    };
  }
  if (type === "UniqueViolation") {
    return { key: "duplicate-slug", label: "Duplicate slug", retryable: false };
  }
  return { key: "other", label: type && type.length <= 32 ? type : "Error", retryable: true };
}

export type FailureGroup = ErrorClass & {
  /** Distinct failed nodes in this family. */
  count: number;
  /** Their node ids — so a scoped retry can target exactly the retryable ones. */
  nodeIds: string[];
};

/** Newest-first copy, so "latest event per node" is well-defined regardless of input order. */
function newestFirst(events: IngestEvent[]): IngestEvent[] {
  return [...events].sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime());
}

/**
 * Roll failure events up by error family, deduplicated by node (newest failure per node wins),
 * collecting each family's node ids. This is what lets the UI say "3 inference timeouts
 * (retryable) · 1 duplicate slug (needs fix)" and retry only the retryable set.
 */
export function groupFailures(events: IngestEvent[]): FailureGroup[] {
  const byKey = new Map<string, FailureGroup>();
  const seen = new Set<string>();
  for (const e of newestFirst(events)) {
    if (e.event !== FAILED_EVENT || seen.has(e.node_id)) continue;
    seen.add(e.node_id);
    const c = classifyError(e);
    if (!c) continue;
    const g = byKey.get(c.key);
    if (g) {
      g.count += 1;
      g.nodeIds.push(e.node_id);
    } else {
      byKey.set(c.key, { ...c, count: 1, nodeIds: [e.node_id] });
    }
  }
  return [...byKey.values()].sort((a, b) => b.count - a.count);
}

export type EventCount = { event: string; label: string; count: number };

/** Count events by type for the "what's in view" rollup, in pipeline order. */
export function countByEvent(events: IngestEvent[]): EventCount[] {
  const counts = new Map<string, number>();
  for (const e of events) counts.set(e.event, (counts.get(e.event) ?? 0) + 1);
  const order = [...EVENT_ORDER, ...[...counts.keys()].filter((e) => !EVENT_ORDER.includes(e))];
  return order
    .filter((e) => counts.has(e))
    .map((e) => ({ event: e, label: EVENT_LABELS[e] ?? e, count: counts.get(e)! }));
}

export type NodeGroup = {
  nodeId: string;
  path: string | null;
  source: string;
  /** This node's events, newest-first — its lifecycle. */
  events: IngestEvent[];
  latest: IngestEvent;
  /** Set when the node's most recent event is a failure (i.e. it's currently broken). */
  failure: ErrorClass | null;
};

/**
 * Collapse the flat event log into one row per node (newest activity first), preserving each
 * node's event history so a row can show its lifecycle (created → enriched → failed → …). A
 * node whose latest event is a failure carries its error class; one that later recovered does not.
 */
export function groupByNode(events: IngestEvent[]): NodeGroup[] {
  const byNode = new Map<string, NodeGroup>();
  for (const e of newestFirst(events)) {
    const g = byNode.get(e.node_id);
    if (g) {
      g.events.push(e);
    } else {
      byNode.set(e.node_id, {
        nodeId: e.node_id,
        path: e.path,
        source: e.source,
        events: [e],
        latest: e,
        failure: e.event === FAILED_EVENT ? classifyError(e) : null,
      });
    }
  }
  return [...byNode.values()].sort(
    (a, b) => new Date(b.latest.at).getTime() - new Date(a.latest.at).getTime(),
  );
}

/** A readable title from a dotted path: the leaf with underscores turned to spaces. */
export function titleOf(path: string | null): string {
  if (!path) return "—";
  const leaf = path.split(".").pop() ?? path;
  return leaf.replace(/_/g, " ");
}

/**
 * Full "provider:model" strings from the node's most recent enrichment_done that
 * recorded them (a NodeGroup's events are already newest-first). Rows predating
 * provider capture — or with malformed detail — contribute nothing.
 */
export function providersOf(g: NodeGroup): string[] {
  for (const e of g.events) {
    if (e.event !== "enrichment_done") continue;
    const p = (e.detail as { providers?: unknown } | null)?.providers;
    if (Array.isArray(p) && p.length > 0 && p.every((x) => typeof x === "string")) {
      return p as string[];
    }
  }
  return [];
}

/** Deduped provider short names ("local:gemma4:26b" -> "local"), input order kept. */
export function shortProviders(providers: string[]): string[] {
  return [...new Set(providers.map((p) => p.split(":", 1)[0] || p))];
}
