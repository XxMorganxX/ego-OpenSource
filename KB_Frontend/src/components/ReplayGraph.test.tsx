import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { buildLayout } from "../lib/replayLayout";
import { buildStages } from "../lib/replayStages";
import { CRAWL, LABELS } from "../test/replayFixtures";
import { ReplayGraph } from "./ReplayGraph";

const layout = buildLayout(CRAWL);
const stages = buildStages(CRAWL, {});
const stage = (id: string) => stages.find((s) => s.id === id)!;

function renderAt(id: string, onSelect = vi.fn(), pinned: string | null = null) {
  const s = stage(id);
  const prev = stages[stages.indexOf(s) - 1] ?? null;
  const utils = render(
    <ReplayGraph layout={layout} stage={s} prev={prev} labels={LABELS}
                 hubLabels={{ "hub:k1": "Postgres (label-cosine)" }}
                 page={new Set(["b", "x"])} limit={2} motion={false}
                 onHover={vi.fn()} onSelect={onSelect} pinned={pinned} />);
  const node = (nid: string) => utils.container.querySelector(`[data-node="${nid}"]`)!;
  const edge = (eid: string) => utils.container.querySelector(`[data-edge="${eid}"]`)!;
  return { ...utils, node, edge };
}

describe("ReplayGraph", () => {
  it("shows only what the stage reveals and marks newly drawn edges", () => {
    const { node, edge } = renderAt("keyword");
    expect(node("a").getAttribute("class")).toContain("on");
    expect(node("c").getAttribute("class")).not.toContain("on");
    expect(edge("fts:a").getAttribute("class")).toContain("on");
    expect(edge("fts:a").getAttribute("class")).toContain("new");
  });

  it("dims rejected crawl rows and labels hubs", () => {
    const { node, getByText } = renderAt("hop");
    expect(node("y").getAttribute("class")).toContain("dim");
    expect(getByText("Postgres (label-cosine)")).toBeInTheDocument();
  });

  it("names the stage's top rows in the callout column, in rank order", () => {
    const callouts = (id: string) => [...renderAt(id).container.querySelectorAll("text.replay-callout")]
      .map((t) => t.textContent);
    expect(callouts("query")).toEqual([]);
    expect(callouts("keyword")).toEqual(["1 Alpha", "2 Beta"]);
    expect(callouts("cut")).toEqual(["1 Beta", "2 Crawled"]);   // limit 2: Alpha and Gamma cut
  });

  it("ties each callout to its node with a leader line", () => {
    const { container } = renderAt("cut");
    const leader = container.querySelector('[data-leader="b"]')!;
    const b = layout.nodes.find((n) => n.id === "b")!;
    expect(leader.getAttribute("points")!.startsWith(`${b.x},${b.y}`)).toBe(true);
  });

  it("no longer labels memories inside the graph", () => {
    const { container } = renderAt("cut");
    const inGraph = [...container.querySelectorAll("text.replay-label")].map((t) => t.textContent);
    expect(inGraph).not.toContain("1 Beta");
  });

  it("selects a memory from its node or its callout, never the query or a hub", () => {
    const onSelect = vi.fn();
    const { node, container } = renderAt("cut", onSelect);
    fireEvent.click(node("b"));
    fireEvent.click(node("y"));            // deleted memories can still be traced
    fireEvent.click(node("query"));
    fireEvent.click(node("hub:k1"));
    fireEvent.click(container.querySelector('[data-callout="x"]')!);
    expect(onSelect.mock.calls).toEqual([["b"], ["y"], ["x"]]);
  });

  it("keeps the pinned memory's node, edges and callout marked", () => {
    const { node, edge, container } = renderAt("cut", vi.fn(), "b");
    expect(node("b").getAttribute("class")).toContain("pinned");
    expect(edge("fts:b").getAttribute("class")).toContain("pinned");
    expect(edge("knn:b").getAttribute("class")).toContain("pinned");
    expect(edge("fts:a").getAttribute("class")).not.toContain("pinned");
    expect(container.querySelector('[data-callout="b"]')!.getAttribute("class")).toContain("pinned");
  });

  it("keeps labels off hub circles", () => {
    const L = {
      nodes: [{ id: "query", kind: "query" as const, x: 500, y: 330, sector: null, sim: null },
        { id: "hub:h1", kind: "hub" as const, x: 400, y: 500, sector: "crawl" as const, sim: null },
        { id: "hub:h2", kind: "hub" as const, x: 450, y: 500, sector: "crawl" as const, sim: null }],
      edges: [],
    };
    const s = { ...stage("cut"), nodes: new Set(["query", "hub:h1", "hub:h2"]),
                highlight: new Set<string>(), dimmed: new Set<string>() };
    const { container } = render(
      <ReplayGraph layout={L} stage={s} prev={null} labels={{}} motion={false}
                   hubLabels={{ "hub:h1": "A long first hub label", "hub:h2": "Second" }}
                   page={new Set()} limit={10} onHover={vi.fn()} onSelect={vi.fn()} />);
    const first = [...container.querySelectorAll("text.replay-label")]
      .find((t) => t.textContent === "A long first hub label")!;
    // Drawn to the right it would run across hub h2 at x=450 -- so it must go elsewhere.
    const x = Number(first.getAttribute("x"));
    const anchor = first.getAttribute("text-anchor");
    const spansH2 = anchor === "start" && x < 450 + 10 && x + 22 * 6.8 > 450 - 10
      && Math.abs(Number(first.getAttribute("y")) - 500) < 14;
    expect(spansH2).toBe(false);
  });

  it("rings the focused node -- the memory hovered elsewhere, e.g. in the fusion columns", () => {
    const s = stage("cut");
    const { container } = render(
      <ReplayGraph layout={layout} stage={s} prev={null} labels={LABELS} hubLabels={{}}
                   page={new Set()} limit={2} motion={false} focus="b" onHover={vi.fn()} onSelect={vi.fn()} />);
    expect(container.querySelector('[data-node="b"]')!.getAttribute("class")).toContain("focus");
    expect(container.querySelector('[data-node="a"]')!.getAttribute("class")).not.toContain("focus");
    expect(container.querySelector('[data-callout="b"]')!.getAttribute("class")).toContain("focus");
  });
});

