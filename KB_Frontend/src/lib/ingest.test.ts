import { describe, expect, it } from "vitest";

import type { IngestEvent } from "../types";
import {
  classifyError,
  countByEvent,
  errorText,
  EVENT_LABELS,
  groupByNode,
  groupFailures,
  providersOf,
  shortProviders,
  titleOf,
} from "./ingest";

function ev(over: Partial<IngestEvent> & { id: number }): IngestEvent {
  return {
    at: new Date(2026, 5, 18, 12, 0, 0).toISOString(),
    event: "enrichment_done",
    source: "obsidian",
    external_id: null,
    node_id: `n${over.id}`,
    path: "notes.alpha",
    content_hash: null,
    detail: {},
    ...over,
  };
}

function failure(id: number, error: string, nodeId = `n${id}`): IngestEvent {
  return ev({ id, event: "enrichment_failed", node_id: nodeId, detail: { error } });
}

describe("classifyError", () => {
  it("treats inference timeouts as retryable", () => {
    const c = classifyError(failure(1, "InferenceError: inference call failed: timed out"));
    expect(c).toEqual({ key: "inference", label: "Inference timeout", retryable: true });
  });

  it("treats duplicate-slug constraint violations as terminal", () => {
    const c = classifyError(failure(2, "UniqueViolation: duplicate key value violates ..."));
    expect(c).toMatchObject({ key: "duplicate-slug", retryable: false });
  });

  it("defaults unknown errors to a retryable 'other' bucket", () => {
    expect(classifyError(failure(3, "ValueError: bad input"))).toMatchObject({
      key: "other",
      label: "ValueError",
      retryable: true,
    });
  });

  it("returns null when the event carries no error", () => {
    expect(classifyError(ev({ id: 4 }))).toBeNull();
    expect(errorText(ev({ id: 4 }))).toBeNull();
  });
});

describe("groupFailures", () => {
  it("buckets by family, dedupes by node, and collects retryable node ids", () => {
    const groups = groupFailures([
      failure(1, "InferenceError: timed out", "a"),
      failure(2, "InferenceError: timed out", "b"),
      // a re-failure of node 'a' must not double-count
      failure(3, "InferenceError: timed out", "a"),
      failure(4, "UniqueViolation: dup", "c"),
    ]);
    const inference = groups.find((g) => g.key === "inference")!;
    const dup = groups.find((g) => g.key === "duplicate-slug")!;
    expect(inference.count).toBe(2);
    expect(new Set(inference.nodeIds)).toEqual(new Set(["a", "b"]));
    expect(inference.retryable).toBe(true);
    expect(dup.count).toBe(1);
    expect(dup.retryable).toBe(false);
  });

  it("labels enrichment_deferred and keeps it out of failure buckets", () => {
    const deferred = ev({
      id: 5,
      event: "enrichment_deferred",
      node_id: "n1",
      path: "a.b",
      detail: { provider: "claude-cli", error: "session limit" },
    });
    expect(EVENT_LABELS["enrichment_deferred"]).toBe("deferred");
    expect(groupFailures([deferred])).toEqual([]); // a deferral is not a failure
  });
});

describe("countByEvent", () => {
  it("counts by type with failures ordered last", () => {
    const counts = countByEvent([
      ev({ id: 1, event: "enrichment_done" }),
      ev({ id: 2, event: "enrichment_done" }),
      ev({ id: 3, event: "node_created" }),
      failure(4, "InferenceError: timed out"),
    ]);
    expect(counts.map((c) => [c.label, c.count])).toEqual([
      ["enriched", 2],
      ["created", 1],
      ["failed", 1],
    ]);
  });

  it("orders ingest_failed and ingest_resolved chips after enrichment_failed", () => {
    const counts = countByEvent([
      ev({ id: 1, event: "ingest_resolved" }),
      ev({ id: 2, event: "ingest_failed", detail: { error: "boom" } }),
      failure(3, "InferenceError: timed out"),
      ev({ id: 4, event: "enrichment_done" }),
    ]);
    expect(counts.map((c) => c.event)).toEqual([
      "enrichment_done",
      "enrichment_failed",
      "ingest_failed",
      "ingest_resolved",
    ]);
  });
});

describe("groupByNode", () => {
  it("collapses to one row per node, newest-first, carrying current failure state", () => {
    const groups = groupByNode([
      ev({ id: 1, node_id: "x", event: "node_created", at: new Date(2026, 5, 1).toISOString() }),
      ev({ id: 2, node_id: "x", event: "enrichment_done", at: new Date(2026, 5, 2).toISOString() }),
      failure(3, "InferenceError: timed out", "x"),
      ev({ id: 4, node_id: "y", event: "enrichment_done", at: new Date(2026, 5, 4).toISOString() }),
    ]);
    // failure(3) defaults to 2026-05-18 (newest) so node x sorts first and is "currently failed".
    expect(groups.map((g) => g.nodeId)).toEqual(["x", "y"]);
    const x = groups[0];
    expect(x.events).toHaveLength(3);
    expect(x.latest.event).toBe("enrichment_failed");
    expect(x.failure).toMatchObject({ key: "inference" });
    expect(groups[1].failure).toBeNull(); // y recovered / never failed
  });
});

describe("titleOf", () => {
  it("humanizes the leaf of a dotted path", () => {
    expect(titleOf("Globex.Docs.Guides.Widget_Setup_Guide")).toBe(
      "Widget Setup Guide",
    );
    expect(titleOf("entities.projects.Garden_Plot")).toBe("Garden Plot");
    expect(titleOf(null)).toBe("—");
  });
});

describe("providersOf", () => {
  const at = (h: number) => new Date(2026, 5, 18, h, 0, 0).toISOString();

  it("returns the providers of the newest enrichment_done that recorded them", () => {
    const g = groupByNode([
      ev({ id: 3, node_id: "same", event: "enrichment_done", at: at(12),
        detail: { providers: ["local:gemma4:26b", "claude-cli:claude-sonnet-5"] } }),
      ev({ id: 2, node_id: "same", event: "enrichment_done", at: at(11),
        detail: { providers: ["local:old-model"] } }),
      ev({ id: 1, node_id: "same", event: "node_created", at: at(10) }),
    ])[0];
    expect(providersOf(g)).toEqual(["local:gemma4:26b", "claude-cli:claude-sonnet-5"]);
  });

  it("skips dones without providers and tolerates malformed detail", () => {
    const g = groupByNode([
      ev({ id: 3, node_id: "same", event: "enrichment_done", at: at(12), detail: {} }),
      ev({ id: 2, node_id: "same", event: "enrichment_done", at: at(11),
        detail: { providers: "local" } }),
      ev({ id: 1, node_id: "same", event: "enrichment_done", at: at(10),
        detail: { providers: ["local:gemma4:26b"] } }),
    ])[0];
    expect(providersOf(g)).toEqual(["local:gemma4:26b"]);
  });

  it("returns [] when no enrichment ever recorded providers", () => {
    const g = groupByNode([ev({ id: 1, event: "node_created" })])[0];
    expect(providersOf(g)).toEqual([]);
  });

  it("rejects a providers array containing non-string members and falls through", () => {
    const g = groupByNode([
      ev({ id: 2, node_id: "same", event: "enrichment_done", at: at(11),
        detail: { providers: ["local:gemma4:26b", 123] } }),
      ev({ id: 1, node_id: "same", event: "enrichment_done", at: at(10),
        detail: { providers: ["claude-cli:claude-sonnet-5"] } }),
    ])[0];
    expect(providersOf(g)).toEqual(["claude-cli:claude-sonnet-5"]);
  });
});

describe("shortProviders", () => {
  it("dedupes to the provider segment, keeping order", () => {
    expect(
      shortProviders(["claude-cli:claude-sonnet-5", "local:gemma4:26b",
        "local:qwen3-embedding:0.6b"]),
    ).toEqual(["claude-cli", "local"]);
  });
});

describe("stuck-record events", () => {
  it("labels ingest_failed and ingest_resolved like the other kinds", () => {
    expect(EVENT_LABELS["ingest_failed"]).toBe("ingest failed");
    expect(EVENT_LABELS["ingest_resolved"]).toBe("ingest resolved");
  });

  it("keeps ingest_failed out of the enrichment failure pile", () => {
    // Retry re-queues enrichment; it cannot fix a record that never landed.
    const groups = groupFailures([
      ev({ id: 1, event: "ingest_failed", node_id: "stuck", detail: { error: "ValueError: NUL" } }),
      failure(2, "InferenceError: inference call failed: timed out"),
    ]);
    expect(groups.map((g) => g.nodeIds)).toEqual([["n2"]]);
  });

  it("still surfaces the reason text of an ingest_failed row", () => {
    expect(errorText(ev({ id: 3, event: "ingest_failed", detail: { error: "boom" } }))).toBe("boom");
  });

  it("counts the new kinds in the rollup with their labels", () => {
    const counts = countByEvent([
      ev({ id: 1, event: "ingest_failed", detail: { error: "boom" } }),
      ev({ id: 2, event: "ingest_resolved", detail: { reason: "dismissed" } }),
    ]);
    expect(counts).toEqual([
      { event: "ingest_failed", label: "ingest failed", count: 1 },
      { event: "ingest_resolved", label: "ingest resolved", count: 1 },
    ]);
  });
});
