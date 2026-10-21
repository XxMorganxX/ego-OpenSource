import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { forwardRef, useImperativeHandle } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NOT_RETRIEVED } from "../lib/colorScheme";
import { HEAT_R_MAX, ROOT_COLOR, ROOT_R } from "../lib/folderHeat";
import { HEAT_RAMP } from "../lib/heat";
import { DEFAULT_GRAPH_SETTINGS, forceParams } from "../lib/settings";
import type {
  Concept,
  EntityGraphData,
  EntityNodeMeta,
  GraphData,
  GraphMode,
  NodeMeta,
} from "../types";

// Captures the latest graphData handed to ForceGraph2D so tests can assert on the exact
// node objects (identity + simulation state), not just their count.
const captured = vi.hoisted(() => ({
  graphData: null as GraphData | null,
  nodeCanvasObject: null as ((node: any, ctx: CanvasRenderingContext2D, scale: number) => void) | null,
  nodeLabel: null as ((node: any) => string) | null,
  linkWidth: null as ((link: any) => number) | null,
  onRenderFramePost: null as ((ctx: CanvasRenderingContext2D, scale: number) => void) | null,
  forces: {} as Record<string, any>,
}));
vi.mock("react-force-graph-2d", () => ({
  default: forwardRef(
    (props: {
      graphData: GraphData;
      onNodeClick?: (n: any) => void;
      nodeCanvasObject?: (node: any, ctx: CanvasRenderingContext2D, scale: number) => void;
      nodeLabel?: (node: any) => string;
      linkWidth?: (link: any) => number;
      onRenderFramePost?: (ctx: CanvasRenderingContext2D, scale: number) => void;
    }, ref: any) => {
      captured.onRenderFramePost = props.onRenderFramePost ?? null;
      captured.graphData = props.graphData;
      captured.nodeCanvasObject = props.nodeCanvasObject ?? null;
      captured.nodeLabel = props.nodeLabel ?? null;
      captured.linkWidth = props.linkWidth ?? null;
      useImperativeHandle(ref, () => ({
        d3Force: (name: string, force?: any) => {
          if (force !== undefined) captured.forces[name] = force;
          return null;
        },
        d3ReheatSimulation: () => {},
        centerAt: () => {},
        zoom: () => 1,
        zoomToFit: () => {},
      }));
      return (
        <div data-testid="fg">
          {props.graphData.nodes.length} nodes
          {props.graphData.nodes.map((n: any) => (
            <button key={n.id} data-testid={`node-${n.id}`} onClick={() => props.onNodeClick?.(n)}>
              {n.id}
            </button>
          ))}
        </div>
      );
    },
  ),
}));

const fetchSubtree = vi.fn();
const fetchRetrievalHeatmap = vi.fn();
vi.mock("../api/client", () => ({
  fetchSubtree: (p: string) => fetchSubtree(p),
  fetchRetrievalHeatmap: (p: unknown) => fetchRetrievalHeatmap(p),
}));

import {
  conceptRadius,
  escapeHtml,
  FOLDER_R_MAX,
  FOLDER_R_MIN,
  folderRadius,
  GraphView,
  isTopLevelConceptNode,
  LARGE_BRANCH_THRESHOLD,
  makeCollideForce,
  makeDepthRepulsionForce,
  makeRadialForce,
  shouldShowNodeLabel,
} from "./GraphView";

const settings = DEFAULT_GRAPH_SETTINGS;

const baseProps = {
  mode: "memories" as GraphMode,
  onModeChange: vi.fn(),
  folderPath: null as string | null,
  nodes: [] as NodeMeta[],
  entityData: null as EntityGraphData | null,
  entityClass: null as string | null,
  concepts: null as Concept[] | null,
  conceptsError: null as string | null,
  onRetryConcepts: vi.fn(),
  onSelectConcept: vi.fn(),
  showInstances: false,
  onShowInstancesChange: vi.fn(),
  showInactive: false,
  onShowInactiveChange: vi.fn(),
  onSelectClass: vi.fn(),
  settings,
  onSettingsChange: vi.fn(),
  importanceDomain: [0, 1] as [number, number],
  liveVersion: 0,
  onSelectNode: vi.fn(),
};

function renderGraph(overrides: Partial<typeof baseProps> = {}) {
  return render(<GraphView {...baseProps} {...overrides} />);
}

function canvasContext() {
  return {
    arc: vi.fn(),
    beginPath: vi.fn(),
    closePath: vi.fn(),
    drawImage: vi.fn(),
    fill: vi.fn(),
    fillRect: vi.fn(),
    fillText: vi.fn(),
    lineTo: vi.fn(),
    measureText: vi.fn(() => ({ width: 48 })),
    moveTo: vi.fn(),
    restore: vi.fn(),
    save: vi.fn(),
    stroke: vi.fn(),
  } as unknown as CanvasRenderingContext2D;
}

function bigGraph(n: number): GraphData {
  const nodes: NodeMeta[] = Array.from({ length: n }, (_, i) => ({
    id: String(i), path: `p${i}`, parent_id: null, type: "note", slug: `p${i}`,
    title: null, tags: [], importance: 1,
  }));
  return { nodes, links: [] };
}

function ent(over: Partial<EntityNodeMeta> & { id: string; role: EntityNodeMeta["role"] }): EntityNodeMeta {
  return {
    path: over.id, parent_id: null, type: over.role === "root" ? "folder" : "entity",
    slug: over.id, title: null, tags: [], importance: 1, entity_type: null, ...over,
  };
}

// Hub + one class + one instance of that class.
const entityGraph: EntityGraphData = {
  nodes: [
    ent({ id: "root", role: "root" }),
    ent({ id: "people", role: "class", parent_id: "root", entity_type: "class" }),
    ent({ id: "avery", role: "instance", parent_id: "root", entity_type: "instance" }),
  ],
  links: [
    { source: "root", target: "people", kind: "containment" },
    { source: "people", target: "avery", kind: "containment" },
  ],
};

describe("GraphView", () => {
  beforeEach(() => {
    fetchSubtree.mockReset();
    fetchRetrievalHeatmap.mockReset();
    captured.forces = {};
  });

  it("renders the graph for a small subtree", async () => {
    fetchSubtree.mockResolvedValue(bigGraph(3));
    renderGraph({ folderPath: "projects" });
    await waitFor(() => expect(screen.getByTestId("fg")).toHaveTextContent("3 nodes"));
    expect(fetchSubtree).toHaveBeenCalledWith("projects");
  });

  it("guards a large subtree until confirmed", async () => {
    fetchSubtree.mockResolvedValue(bigGraph(LARGE_BRANCH_THRESHOLD + 1));
    renderGraph({ folderPath: "big" });
    const button = await screen.findByRole("button", { name: /render anyway/i });
    expect(screen.queryByTestId("fg")).toBeNull();
    await userEvent.click(button);
    expect(screen.getByTestId("fg")).toBeInTheDocument();
  });

  it("reuses node objects across a live refetch so positions survive", async () => {
    fetchSubtree.mockResolvedValue(bigGraph(3));
    const { rerender } = renderGraph({ folderPath: "projects", liveVersion: 1 });
    await waitFor(() => expect(screen.getByTestId("fg")).toHaveTextContent("3 nodes"));

    // Simulate d3-force having positioned node "1" in place after the first layout.
    const before = captured.graphData!.nodes.find((n: any) => n.id === "1") as any;
    before.x = 42;
    before.y = 7;

    // A live DB change bumps liveVersion; the component refetches the same subtree, but
    // the server payload is a fresh set of objects (new identity, importance changed).
    const refreshed = bigGraph(3);
    (refreshed.nodes.find((n) => n.id === "1") as any).importance = 99;
    fetchSubtree.mockResolvedValue(refreshed);
    rerender(<GraphView {...baseProps} folderPath="projects" liveVersion={2} />);

    await waitFor(() => {
      const after = captured.graphData!.nodes.find((n: any) => n.id === "1") as any;
      expect(after.importance).toBe(99); // data fields refreshed from the payload
      expect(after).toBe(before); // same object reused, not replaced
      expect(after.x).toBe(42); // simulation state preserved through the refetch
      expect(after.y).toBe(7);
    });
  });

  it("renders the root graph from all nodes when no folder is selected", () => {
    renderGraph({ folderPath: null, nodes: bigGraph(4).nodes });
    expect(screen.getByTestId("fg")).toHaveTextContent("4 nodes");
    expect(fetchSubtree).not.toHaveBeenCalled();
  });

  const radialNodes: NodeMeta[] = [
    { id: "a", path: "a", parent_id: null, type: "folder", slug: "a",
      title: null, tags: [], importance: 1 },
    { id: "a1", path: "a.one", parent_id: "a", type: "note", slug: "one",
      title: null, tags: [], importance: 1 },
    { id: "b", path: "b", parent_id: null, type: "folder", slug: "b",
      title: null, tags: [], importance: 1 },
  ];

  it("registers the radial force in the gravity slot in memories mode", () => {
    renderGraph({ folderPath: null, nodes: radialNodes });
    const force = captured.forces["gravity"];
    expect(force).toBeDefined();
    // Behaves radially: a stamped node just off the origin is pushed OUTWARD toward its
    // ring — origin gravity would pull it in.
    const a = captured.graphData!.nodes.find((node: any) => node.id === "a") as any;
    expect(a.__targetR).toBeGreaterThan(0);
    a.x = Math.cos(a.__targetTheta);
    a.y = Math.sin(a.__targetTheta);
    a.vx = 0;
    a.vy = 0;
    force.initialize([a]);
    force(1);
    const outward = a.vx * Math.cos(a.__targetTheta) + a.vy * Math.sin(a.__targetTheta);
    expect(outward).toBeGreaterThan(0);
  });

  it("keeps origin gravity in entities mode", () => {
    renderGraph({ mode: "entities", entityData: entityGraph });
    const force = captured.forces["gravity"];
    expect(force).toBeDefined();
    const n = { x: 10, y: 0, vx: 0, vy: 0 }; // no __targetR — radial would skip it
    force.initialize([n]);
    force(1);
    expect(n.vx).toBeLessThan(0); // pulled toward the origin
  });

  it("reverses origin gravity for a negative setting", () => {
    renderGraph({
      mode: "entities",
      entityData: entityGraph,
      settings: { ...DEFAULT_GRAPH_SETTINGS, gravity: -5 },
    });
    const force = captured.forces["gravity"];
    const n = { x: 10, y: 0, vx: 0, vy: 0 };
    force.initialize([n]);
    force(1);
    expect(n.vx).toBeGreaterThan(0);
  });

  it("reverses radial target correction for a negative setting", () => {
    renderGraph({
      folderPath: null,
      nodes: radialNodes,
      settings: { ...DEFAULT_GRAPH_SETTINGS, gravity: -5 },
    });
    const force = captured.forces["gravity"];
    const a = captured.graphData!.nodes.find((node: any) => node.id === "a") as any;
    a.x = Math.cos(a.__targetTheta);
    a.y = Math.sin(a.__targetTheta);
    a.vx = 0;
    a.vy = 0;
    force.initialize([a]);
    force(1);
    const radialVelocity =
      a.vx * Math.cos(a.__targetTheta) + a.vy * Math.sin(a.__targetTheta);
    expect(radialVelocity).toBeLessThan(0);
  });

  it("reverses tangential wedge correction for a negative setting", () => {
    const targetTheta = 0;
    const nodeTheta = 0.2;
    const radius = 10;
    const n = {
      __targetR: radius,
      __targetTheta: targetTheta,
      x: radius * Math.cos(nodeTheta),
      y: radius * Math.sin(nodeTheta),
      vx: 0,
      vy: 0,
    };
    const force = makeRadialForce(-0.01);
    force.initialize([n]);
    force(1);
    // The node is already on its target ring but counterclockwise from its target wedge.
    // Negative gravity must push it farther counterclockwise, away from that wedge.
    const angularVelocity = Math.cos(nodeTheta) * n.vy - Math.sin(nodeTheta) * n.vx;
    expect(angularVelocity).toBeGreaterThan(0);
  });

  it("seeds new memories nodes on their depth rings", () => {
    renderGraph({ folderPath: null, nodes: radialNodes });
    const { linkDistance } = forceParams(DEFAULT_GRAPH_SETTINGS.spacing);
    for (const id of ["a", "b"]) {
      const node = captured.graphData!.nodes.find((x: any) => x.id === id) as any;
      // ring 1 ± jitter (each axis ≤ 2 ⇒ radial deviation ≤ ~2.9)
      expect(Math.hypot(node.x, node.y)).toBeGreaterThan(linkDistance - 4);
      expect(Math.hypot(node.x, node.y)).toBeLessThan(linkDistance + 4);
    }
    const leaf = captured.graphData!.nodes.find((x: any) => x.id === "a1") as any;
    expect(Math.hypot(leaf.x, leaf.y)).toBeGreaterThan(2 * linkDistance - 4);
  });

  it("releases the entities focus freeze when switching to another mode", () => {
    const { rerender } = renderGraph({ mode: "entities", entityData: entityGraph });
    // Give the shared-space nodes settled positions, as d3 would have.
    const hub = captured.graphData!.nodes.find((n: any) => n.id === "root") as any;
    const cls = captured.graphData!.nodes.find((n: any) => n.id === "people") as any;
    hub.x = 5;
    hub.y = 6;
    cls.x = 30;
    cls.y = 40;
    // Focusing a class freezes the frame (hub + every class) in place.
    rerender(
      <GraphView {...baseProps} mode="entities" entityData={entityGraph} entityClass="people" />,
    );
    expect(cls.fx).toBe(30);
    expect(cls.fy).toBe(40);
    // Switch straight to memories with a graph sharing the class' id: the pin must not
    // survive the mode switch — a pinned node can never be pulled onto its radial ring.
    const shared: NodeMeta[] = [
      { id: "people", path: "people", parent_id: null, type: "folder", slug: "people",
        title: null, tags: [], importance: 1 },
    ];
    rerender(<GraphView {...baseProps} mode="memories" folderPath={null} nodes={shared} />);
    const survivor = captured.graphData!.nodes.find((n: any) => n.id === "people") as any;
    expect(survivor).toBe(cls); // same cached object carried across the mode switch
    expect(survivor.fx).toBeUndefined();
    expect(survivor.fy).toBeUndefined();
  });

  it("clears radial target stamps when leaving memories mode", () => {
    // The entities graph shares the id "a" with the memories fixture, so the cached node
    // object survives the mode switch and must lose its memories-mode stamps — otherwise
    // a stale __targetR would let the radial force act outside memories mode.
    const { rerender } = renderGraph({ folderPath: null, nodes: radialNodes });
    const a = captured.graphData!.nodes.find((n: any) => n.id === "a") as any;
    expect(a.__targetR).toBeGreaterThan(0);
    const sharedEntities: EntityGraphData = {
      nodes: [
        ent({ id: "root", role: "root" }),
        ent({ id: "a", role: "class", entity_type: "class" }),
      ],
      links: [{ source: "root", target: "a", kind: "containment" }],
    };
    rerender(<GraphView {...baseProps} mode="entities" entityData={sharedEntities} />);
    const survivor = captured.graphData!.nodes.find((n: any) => n.id === "a") as any;
    expect(survivor).toBe(a);
    expect(survivor.__targetR).toBeUndefined();
    expect(survivor.__targetTheta).toBeUndefined();
    expect(survivor.__targetDepth).toBeUndefined();
  });

  it("wires depth repulsion in memories mode and removes it elsewhere", () => {
    const { rerender } = renderGraph({ folderPath: null, nodes: radialNodes });
    expect(typeof captured.forces["depthRepel"]).toBe("function");
    // Depths are stamped so the force has something to read: root folders sit on ring 1,
    // their children one ring further out.
    const a = captured.graphData!.nodes.find((n: any) => n.id === "a") as any;
    const a1 = captured.graphData!.nodes.find((n: any) => n.id === "a1") as any;
    expect(a.__targetDepth).toBe(1);
    expect(a1.__targetDepth).toBe(2);
    rerender(<GraphView {...baseProps} mode="entities" entityData={entityGraph} />);
    expect(captured.forces["depthRepel"]).toBeNull();
  });

  it("shows a Removed legend entry for an inherited descendant of an excluded folder", () => {
    // child carries effective exclusion (own-flag unset) — descendants drive the legend too
    const nodes: NodeMeta[] = [
      { id: "1", path: "scratch", parent_id: null, type: "folder", slug: "scratch",
        title: null, tags: [], importance: 1,
        analysis_excluded: true, analysis_excluded_effective: true },
      { id: "2", path: "scratch.note", parent_id: "1", type: "note", slug: "note",
        title: null, tags: [], importance: 1,
        analysis_excluded: false, analysis_excluded_effective: true },
    ];
    renderGraph({ folderPath: null, nodes });
    expect(screen.getByText("Removed")).toBeInTheDocument();
  });

  it("omits the Removed legend entry when nothing is excluded", () => {
    renderGraph({ folderPath: null, nodes: bigGraph(2).nodes });
    expect(screen.queryByText("Removed")).toBeNull();
  });

  it("shows an empty-state guard when there are no nodes at the root", () => {
    renderGraph({ folderPath: null, nodes: [] });
    expect(screen.queryByTestId("fg")).toBeNull();
    expect(screen.getByText(/nothing to graph yet/i)).toBeInTheDocument();
  });

  it("renders the mode toggle and switches modes", async () => {
    renderGraph();
    const entitiesBtn = screen.getByRole("button", { name: "Entities" });
    await userEvent.click(entitiesBtn);
    expect(baseProps.onModeChange).toHaveBeenCalledWith("entities");
  });

  it("switches the node color scheme from the view-filter overlay", async () => {
    const onSettingsChange = vi.fn();
    renderGraph({ folderPath: null, nodes: bigGraph(3).nodes, onSettingsChange });
    await userEvent.click(screen.getByRole("radio", { name: /by depth/i }));
    expect(onSettingsChange).toHaveBeenCalledWith(
      expect.objectContaining({ colorScheme: "depth" }),
    );
  });

  describe("retrieval scheme (folders only, summed heat)", () => {
    const meta = (id: string, path: string, type: string, parent_id: string | null): NodeMeta => ({
      id, path, parent_id, type, slug: path.split(".").pop()!, title: null, tags: [], importance: 1,
    });
    // vault ─┬─ hot (2 memories, 100 retrievals) ── deep (1 memory, 60)
    //        ├─ warm (1 memory, 1 retrieval)
    //        └─ cold (1 memory, never retrieved)
    const tree: NodeMeta[] = [
      meta("v", "vault", "folder", null),
      meta("h", "vault.hot", "folder", "v"),
      meta("d", "vault.hot.deep", "folder", "h"),
      meta("w", "vault.warm", "folder", "v"),
      meta("c", "vault.cold", "folder", "v"),
      meta("m1", "vault.hot.note1", "note", "h"),
      meta("m2", "vault.hot.deep.note2", "note", "d"),
      meta("m3", "vault.warm.note3", "note", "w"),
      meta("m4", "vault.cold.note4", "note", "c"),
    ];
    const row = (id: string, path: string, surfaced: number, opened: number, title: string | null) => ({
      id, path, title, surfaced, opened, cells: [],
    });
    const payload = (rows: ReturnType<typeof row>[]) => ({
      window: { days: null, bucket: "week", tz: "UTC", start: "2026-07-27", end: "2026-10-05" },
      buckets: [], rows, total_memories: rows.length, max_cell: 0,
    });
    const rows = [
      row("m1", "vault.hot.note1", 36, 4, "First note"),
      row("m2", "vault.hot.deep.note2", 55, 5, null),
      row("m3", "vault.warm.note3", 1, 0, null),
    ];
    const node = (id: string) => (captured.graphData!.nodes as any[]).find((n) => n.id === id);
    const retrievalSettings = { ...settings, colorScheme: "retrieval" as const };

    it("fetches all-history counts with a cap that covers every retrieved memory", () => {
      fetchRetrievalHeatmap.mockResolvedValue(payload(rows));
      renderGraph({ folderPath: null, nodes: tree, settings: retrievalSettings });
      expect(fetchRetrievalHeatmap).toHaveBeenCalledWith(
        expect.objectContaining({ days: "all", path: null, limit: 1000 }),
      );
    });

    const ids = () => captured.graphData!.nodes.map((n) => n.id).sort();
    // Settle every drawn node far apart (one per 300 px row) so placement has room.
    const spread = () =>
      (captured.graphData!.nodes as any[]).forEach((n, i) => Object.assign(n, { x: 0, y: i * 300 }));
    // Run the frame's label pass; returns the drawn texts (in draw order) and their backgrounds.
    const frameLabels = (scale = 1) => {
      const ctx = canvasContext();
      captured.onRenderFramePost!(ctx, scale);
      const texts = (ctx.fillText as any).mock.calls.map((c: any[]) => c[0] as string);
      const boxes = (ctx.fillRect as any).mock.calls.map(([x, y, w, h]: number[]) => ({ x, y, w, h }));
      return { texts, boxes };
    };

    it("draws only folders with retrievals beneath them, plus the root", async () => {
      fetchRetrievalHeatmap.mockResolvedValue(payload(rows));
      renderGraph({ folderPath: null, nodes: tree, settings: retrievalSettings });
      // No memory dots, and the never-retrieved "cold" folder is gone.
      await waitFor(() => expect(ids()).toEqual(["d", "h", "v", "w"]));
      expect(captured.graphData!.links).toHaveLength(3); // v→h, h→d, v→w
    });

    it("shows only the root while counts load, so cold folders never flash in", () => {
      fetchRetrievalHeatmap.mockReturnValue(new Promise(() => {}));
      renderGraph({ folderPath: null, nodes: tree, settings: retrievalSettings });
      expect(ids()).toEqual(["v"]);
      expect(screen.getByText("Loading retrievals…")).toBeInTheDocument();
    });

    it("marks the root as the root: centered, neutral, fixed size, no number", async () => {
      fetchRetrievalHeatmap.mockResolvedValue(payload(rows));
      renderGraph({ folderPath: null, nodes: tree, settings: retrievalSettings });
      await waitFor(() => expect(ids()).toContain("h"));
      expect(node("v").__color).toBe(ROOT_COLOR);
      expect(node("v").__r).toBe(ROOT_R);
      expect([node("v").fx, node("v").fy]).toEqual([0, 0]);
      spread();
      expect(frameLabels().texts).toContain("vault · root");
      // The node painter itself no longer draws labels in this view.
      const ctx = canvasContext();
      captured.nodeCanvasObject!({ ...node("v"), x: 0, y: 0 }, ctx, 1);
      expect(ctx.fillText).not.toHaveBeenCalled();
    });

    it("puts the root's total in the legend, not on the node", async () => {
      fetchRetrievalHeatmap.mockResolvedValue(payload(rows));
      renderGraph({ folderPath: null, nodes: tree, settings: retrievalSettings });
      expect(
        await screen.findByText("Root: vault · 101 retrievals across 3 memories"),
      ).toBeInTheDocument();
      expect(screen.queryByText(/Root · \d+ nodes/)).toBeNull();
    });

    it("scales color and size over the subfolders, not the root that holds them all", async () => {
      fetchRetrievalHeatmap.mockResolvedValue(payload(rows));
      renderGraph({ folderPath: null, nodes: tree, settings: retrievalSettings });
      await waitFor(() => expect(node("h").__color).toBe(HEAT_RAMP[HEAT_RAMP.length - 1]));
      expect(node("w").__color).toBe(HEAT_RAMP[0]);
      expect(node("h").__r).toBe(HEAT_R_MAX); // hot (100) tops the scale, not vault (101)
      expect(node("h").__r).toBeGreaterThan(node("d").__r);
      expect(node("d").__r).toBeGreaterThan(node("w").__r);
      expect(screen.getByText("1 → 100 retrievals per folder")).toBeInTheDocument();
    });

    it("hovering a folder shows its total and its busiest memories", async () => {
      fetchRetrievalHeatmap.mockResolvedValue(payload(rows));
      renderGraph({ folderPath: null, nodes: tree, settings: retrievalSettings });
      await waitFor(() => expect(ids()).toContain("h"));
      expect(captured.nodeLabel!(node("h"))).toBe(
        "vault.hot · 100 retrievals across 2 memories — top: note2 (60), First note (40)",
      );
    });

    it("labels the hottest subfolders at normal zoom, with their totals", async () => {
      fetchRetrievalHeatmap.mockResolvedValue(payload(rows));
      renderGraph({ folderPath: null, nodes: tree, settings: retrievalSettings });
      await waitFor(() => expect(ids()).toContain("h"));
      spread();
      // Root first, then hottest first.
      expect(frameLabels().texts).toEqual(["vault · root", "hot · 100", "deep · 60", "warm · 1"]);
    });

    it("never draws two labels on top of each other when folders crowd together", async () => {
      fetchRetrievalHeatmap.mockResolvedValue(payload(rows));
      renderGraph({ folderPath: null, nodes: tree, settings: retrievalSettings });
      await waitFor(() => expect(ids()).toContain("h"));
      // The live failure: hot folders settling within a few px of each other.
      for (const [id, x, y] of [["v", 0, 0], ["h", 6, 3], ["d", 10, -2], ["w", 4, 8]] as const)
        Object.assign(node(id), { x, y });
      const { texts, boxes } = frameLabels();
      expect(texts[0]).toBe("vault · root"); // the root always keeps its name
      for (let i = 0; i < boxes.length; i++)
        for (let j = i + 1; j < boxes.length; j++) {
          const [a, b] = [boxes[i], boxes[j]];
          const hit = a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
          expect(hit, `${texts[i]} × ${texts[j]}`).toBe(false);
        }
    });

    it("labels the root plus at most the ten hottest subfolders at normal zoom", async () => {
      const many = [meta("root", "r", "folder", null)];
      const hits = [];
      for (let i = 0; i < 12; i++) {
        many.push(meta(`f${i}`, `r.f${i}`, "folder", "root"));
        hits.push(row(`n${i}`, `r.f${i}.note`, i + 1, 0, null));
      }
      fetchRetrievalHeatmap.mockResolvedValue(payload(hits));
      renderGraph({ folderPath: null, nodes: many, settings: retrievalSettings });
      await waitFor(() => expect(ids()).toContain("f11"));
      spread();
      const labels = frameLabels().texts;
      expect(labels).toHaveLength(11);
      expect(labels).toContain("r · root");
      expect(labels).not.toContain("f0 · 1"); // 11th and 12th hottest stay unlabeled
      expect(labels).not.toContain("f1 · 2");
    });

    it("treats the selected folder as the root when drilled in", async () => {
      fetchSubtree.mockResolvedValue({
        nodes: tree.filter((n) => n.path.startsWith("vault.hot")),
        links: [
          { source: "h", target: "d", kind: "containment" },
          { source: "h", target: "m1", kind: "containment" },
          { source: "d", target: "m2", kind: "containment" },
        ],
      });
      fetchRetrievalHeatmap.mockResolvedValue(payload(rows));
      renderGraph({ folderPath: "vault.hot", settings: retrievalSettings });
      expect(
        await screen.findByText("Root: vault.hot · 100 retrievals across 2 memories"),
      ).toBeInTheDocument();
      expect(ids()).toEqual(["d", "h"]);
      expect(node("h").__color).toBe(ROOT_COLOR);
    });

    it("emphasizes links into hotter folders", async () => {
      fetchRetrievalHeatmap.mockResolvedValue(payload(rows));
      renderGraph({ folderPath: null, nodes: tree, settings: retrievalSettings });
      await waitFor(() => expect(ids()).toContain("h"));
      const link = (target: string) => ({ source: node("v"), target: node(target), kind: "containment" });
      expect(captured.linkWidth!(link("h"))).toBeGreaterThan(captured.linkWidth!(link("w")));
    });

    it("unpins the root when leaving the retrieval view", async () => {
      fetchRetrievalHeatmap.mockResolvedValue(payload(rows));
      const { rerender } = renderGraph({ folderPath: null, nodes: tree, settings: retrievalSettings });
      await waitFor(() => expect(node("v").fx).toBe(0));
      rerender(<GraphView {...baseProps} folderPath={null} nodes={tree} settings={settings} />);
      expect(node("v").fx).toBeUndefined();
      expect(node("v").fy).toBeUndefined();
    });
  });

  it("does not fetch retrieval counts under any other scheme", () => {
    renderGraph({ folderPath: null, nodes: bigGraph(3).nodes });
    expect(fetchRetrievalHeatmap).not.toHaveBeenCalled();
  });

  it("hides the view-filter overlay in entities mode (entities own their palette)", () => {
    renderGraph({ mode: "entities", entityData: entityGraph });
    expect(screen.queryByRole("radiogroup", { name: /color/i })).toBeNull();
  });

  it("shows classes (and the hub) but hides instances by default in entities mode", () => {
    renderGraph({ mode: "entities", entityData: entityGraph });
    // hub + people class, instance excluded -> 2 nodes
    expect(screen.getByTestId("fg")).toHaveTextContent("2 nodes");
    expect(screen.getByText("Class")).toBeInTheDocument();
    expect(screen.queryByText("Instance")).toBeNull();
  });

  it("reveals instances in entities mode when showInstances is set", () => {
    renderGraph({ mode: "entities", entityData: entityGraph, showInstances: true });
    expect(screen.getByTestId("fg")).toHaveTextContent("3 nodes");
    expect(screen.getByText("Instance")).toBeInTheDocument();
  });

  it("offers a Show instances toggle in entities mode", () => {
    renderGraph({ mode: "entities", entityData: entityGraph });
    expect(screen.getByText("Show instances")).toBeInTheDocument();
  });

  it("shows an empty-state guard when there are no entities", () => {
    renderGraph({ mode: "entities", entityData: { nodes: [], links: [] } });
    expect(screen.queryByTestId("fg")).toBeNull();
    expect(screen.getByText(/no entities yet/i)).toBeInTheDocument();
  });

  it("renders the Show inactive toggle in the all-entities view and reports changes", async () => {
    const onShowInactiveChange = vi.fn();
    renderGraph({ mode: "entities", entityData: entityGraph, onShowInactiveChange });
    await userEvent.click(screen.getByLabelText("Show inactive"));
    expect(onShowInactiveChange).toHaveBeenCalledWith(true);
  });

  it("single click on a class opens its panel AND drills in", () => {
    vi.useFakeTimers();
    try {
      const onSelectClass = vi.fn();
      const onSelectNode = vi.fn();
      renderGraph({ mode: "entities", entityData: entityGraph, onSelectClass, onSelectNode });
      fireEvent.click(screen.getByTestId("node-people"));
      // The panel opens immediately; the drill lands after the camera flight.
      expect(onSelectNode).toHaveBeenCalledWith("people");
      act(() => vi.advanceTimersByTime(250));
      expect(onSelectClass).toHaveBeenCalledWith("people");
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not focus a class when clicking the hub", async () => {
    const onSelectClass = vi.fn();
    const onSelectNode = vi.fn();
    renderGraph({ mode: "entities", entityData: entityGraph, onSelectClass, onSelectNode });
    await userEvent.click(screen.getByTestId("node-root"));
    expect(onSelectClass).not.toHaveBeenCalled();
    expect(onSelectNode).toHaveBeenCalledWith("root");
  });

  it("does not re-focus when already drilled into a class", async () => {
    const onSelectClass = vi.fn();
    const onSelectNode = vi.fn();
    renderGraph({
      mode: "entities",
      entityData: entityGraph,
      entityClass: "people",
      onSelectClass,
      onSelectNode,
    });
    await userEvent.click(screen.getByTestId("node-people"));
    expect(onSelectClass).not.toHaveBeenCalled();
    expect(onSelectNode).toHaveBeenCalledWith("people");
  });
});

describe("concepts mode", () => {
  const concepts: Concept[] = [
    {
      id: "c1", name: "KB project", kind: "project", level: 0, parent_id: null,
      description: "engine work", member_count: 2, label_locked: false, status: "active",
      members: [
        { node_id: "m1", member_kind: "memory", score: 1.0, label: "Note One" },
        { node_id: "e1", member_kind: "entity", score: 0.9, label: "Ollama" },
      ],
    },
  ];

  const hierarchicalConcepts: Concept[] = [
    {
      id: "root-concept", name: "Garden Planner", kind: "project", level: 0, parent_id: null,
      description: "Side project work", member_count: 2, label_locked: false,
      status: "active",
      members: [
        { node_id: "m1", member_kind: "memory", score: 1, label: "Architecture" },
        { node_id: "m2", member_kind: "entity", score: 0.9, label: "Ollama" },
      ],
    },
    {
      id: "child-concept", name: "Inference", kind: "topic", level: 1,
      parent_id: "root-concept", description: null, member_count: 1, label_locked: false,
      status: "active",
      members: [{ node_id: "m2", member_kind: "entity", score: 0.9, label: "Ollama" }],
    },
  ];

  it("always labels concept hubs but keeps member labels zoom-gated", () => {
    expect(shouldShowNodeLabel({ role: "concept" }, 0.7, true)).toBe(true);
    expect(shouldShowNodeLabel({ role: "member" }, 0.7, true)).toBe(false);
    expect(shouldShowNodeLabel({ role: "member" }, 1.31, true)).toBe(true);
  });

  it("labels only grouping-level nodes on mobile, at any zoom", () => {
    // Grouping nodes keep their titles even zoomed out.
    expect(shouldShowNodeLabel({ role: "class" }, 0.7, false, true)).toBe(true);
    expect(shouldShowNodeLabel({ role: "root" }, 0.7, false, true)).toBe(true);
    expect(shouldShowNodeLabel({ role: "concept" }, 0.7, true, true)).toBe(true);
    // Individual nodes never label on mobile, even deep-zoomed.
    expect(shouldShowNodeLabel({ role: "instance" }, 3, false, true)).toBe(false);
    expect(shouldShowNodeLabel({ role: "member" }, 3, true, true)).toBe(false);
    expect(shouldShowNodeLabel({}, 3, false, true)).toBe(false);
  });

  it("identifies only root concept hubs for hierarchy rings", () => {
    expect(isTopLevelConceptNode({ role: "concept", parent_id: null })).toBe(true);
    expect(isTopLevelConceptNode({ role: "concept", parent_id: "parent" })).toBe(false);
    expect(isTopLevelConceptNode({ role: "member", parent_id: null })).toBe(false);
  });

  it("paints overview hub labels, zoom-gated member labels, and a root-only ring", () => {
    renderGraph({ mode: "concepts", concepts: hierarchicalConcepts });
    const painter = captured.nodeCanvasObject;
    expect(painter).not.toBeNull();
    const root = captured.graphData!.nodes.find((node) => node.id === "root-concept") as any;
    const child = captured.graphData!.nodes.find((node) => node.id === "child-concept") as any;
    const member = captured.graphData!.nodes.find((node) => node.id === "m1") as any;
    root.__sprite = child.__sprite = member.__sprite = null;
    root.__spriteDensity = child.__spriteDensity = member.__spriteDensity = 1;

    const rootContext = canvasContext();
    painter!(root, rootContext, 0.7);
    expect(rootContext.fillText).toHaveBeenCalledWith("Garden Planner", root.x, expect.any(Number));
    expect(rootContext.arc).toHaveBeenCalledTimes(2);
    expect(rootContext.arc).toHaveBeenLastCalledWith(
      root.x,
      root.y,
      root.__r + 3 / 0.7,
      0,
      2 * Math.PI,
    );

    const childContext = canvasContext();
    painter!(child, childContext, 0.7);
    expect(childContext.fillText).toHaveBeenCalledWith("Inference", child.x, expect.any(Number));
    expect(childContext.arc).toHaveBeenCalledTimes(1);

    const overviewMemberContext = canvasContext();
    painter!(member, overviewMemberContext, 0.7);
    expect(overviewMemberContext.fillText).not.toHaveBeenCalled();

    const zoomedMemberContext = canvasContext();
    member.__spriteDensity = 2;
    painter!(member, zoomedMemberContext, 1.31);
    expect(zoomedMemberContext.fillText).toHaveBeenCalledWith(
      "Architecture",
      member.x,
      expect.any(Number),
    );
  });

  it("keeps origin gravity in concepts mode", () => {
    renderGraph({ mode: "concepts", concepts });
    const force = captured.forces["gravity"];
    expect(force).toBeDefined();
    const n = { x: 10, y: 0, vx: 0, vy: 0 }; // no __targetR — radial would skip it
    force.initialize([n]);
    force(1);
    expect(n.vx).toBeLessThan(0); // pulled toward the origin
  });

  it("reports separate concept and member totals in the legend", () => {
    renderGraph({ mode: "concepts", concepts: hierarchicalConcepts });
    expect(screen.getByText("Concepts · 2 concepts · 2 members")).toBeInTheDocument();
  });

  it("shows a retryable guard when concepts loading fails", async () => {
    const onRetryConcepts = vi.fn();
    renderGraph({ mode: "concepts", concepts: null, conceptsError: "503", onRetryConcepts });
    expect(screen.getByText("Concepts could not be loaded")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetryConcepts).toHaveBeenCalledOnce();
  });

  it("renders concept hubs plus member nodes and routes clicks by role", () => {
    const onSelectConcept = vi.fn();
    const onSelectNode = vi.fn();
    render(
      <GraphView
        {...baseProps}
        mode="concepts"
        concepts={concepts}
        onSelectConcept={onSelectConcept}
        onSelectNode={onSelectNode}
      />,
    );
    expect(captured.graphData?.nodes).toHaveLength(3); // 1 hub + 2 members
    fireEvent.click(screen.getByTestId("node-c1"));
    expect(onSelectConcept).toHaveBeenCalledWith("c1"); // hub -> concept card
    expect(onSelectNode).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("node-m1"));
    expect(onSelectNode).toHaveBeenCalledWith("m1"); // member -> node detail
  });

  it("shows the empty guard when no concepts exist", () => {
    render(<GraphView {...baseProps} mode="concepts" concepts={[]} />);
    expect(screen.getByText("No concepts yet")).toBeInTheDocument();
  });

  it("offers all three mode buttons and marks Concepts active", () => {
    render(<GraphView {...baseProps} mode="concepts" concepts={[]} />);
    const btn = screen.getByRole("button", { name: "Concepts" });
    expect(btn.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Memories" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Entities" })).toBeInTheDocument();
  });
});

it("conceptRadius grows with member count and caps", () => {
  expect(conceptRadius(0)).toBeCloseTo(5.5);
  expect(conceptRadius(9)).toBeCloseTo(5.5 + 3 * 0.9);
  expect(conceptRadius(1000)).toBe(13);
});

it("escapeHtml neutralizes markup in tooltip labels", () => {
  expect(escapeHtml(`<img src=x onerror=alert(1)>"&'`)).toBe(
    "&lt;img src=x onerror=alert(1)&gt;&quot;&amp;&#39;",
  );
  expect(escapeHtml("projects.kb_mcp.notes")).toBe("projects.kb_mcp.notes");
});

describe("folderRadius", () => {
  it("stays within the configured bounds", () => {
    for (const depth of [1, 2, 5, 20]) {
      for (const sig of [0, 0.5, 1]) {
        const r = folderRadius(depth, sig);
        expect(r).toBeGreaterThanOrEqual(FOLDER_R_MIN);
        expect(r).toBeLessThanOrEqual(FOLDER_R_MAX);
      }
    }
  });

  it("shrinks with depth at equal significance (shallower reads bigger)", () => {
    expect(folderRadius(1, 0.5)).toBeGreaterThan(folderRadius(2, 0.5));
    expect(folderRadius(2, 0.5)).toBeGreaterThan(folderRadius(4, 0.5));
  });

  it("grows with significance at equal depth", () => {
    expect(folderRadius(2, 1)).toBeGreaterThan(folderRadius(2, 0));
  });

  it("mixes both signals — a deep, significant folder can outsize a shallow, trivial one", () => {
    // depth alone would make the shallow folder bigger; significance tips it the other way.
    expect(folderRadius(3, 1)).toBeGreaterThan(folderRadius(1, 0));
  });

  it("hits the top of the range only when shallow AND maximally significant", () => {
    expect(folderRadius(1, 1)).toBeCloseTo(FOLDER_R_MAX);
  });

  it("clamps out-of-range significance", () => {
    expect(folderRadius(1, 2)).toBe(folderRadius(1, 1));
    expect(folderRadius(1, -1)).toBe(folderRadius(1, 0));
  });
});

describe("makeCollideForce", () => {
  const node = (x: number, y: number, r: number) => ({ x, y, vx: 0, vy: 0, r });
  const run = (nodes: any[], radius = 10) => {
    const force = makeCollideForce(() => radius, 0, 1);
    force.initialize(nodes);
    force();
    return nodes;
  };

  it("pushes overlapping nodes apart along their connecting axis", () => {
    const a = node(0, 0, 10);
    const b = node(5, 0, 10); // centers 5 apart, radii sum 20 -> overlapping
    run([a, b]);
    expect(a.vx).toBeLessThan(0); // a shoved left
    expect(b.vx).toBeGreaterThan(0); // b shoved right
    expect(a.vy).toBeCloseTo(0); // purely horizontal separation
    expect(b.vy).toBeCloseTo(0);
  });

  it("leaves nodes that don't overlap untouched", () => {
    const a = node(0, 0, 5);
    const b = node(100, 0, 5);
    run([a, b], 5);
    expect(a.vx).toBe(0);
    expect(b.vx).toBe(0);
  });

  it("separates coincident nodes deterministically instead of dividing by zero", () => {
    const a = node(0, 0, 10);
    const b = node(0, 0, 10);
    run([a, b]);
    expect(Number.isFinite(a.vx)).toBe(true);
    expect(Number.isFinite(b.vx)).toBe(true);
    expect(a.vx).not.toBe(b.vx); // pushed off the shared point
  });

  it("still separates an overlapping pair that straddles a grid-cell boundary", () => {
    // radius 10, pad 0 -> cellSize 20. a at x=19 (cell 0), b at x=21 (cell 1): different cells
    // but only 2 apart, so they overlap. The adjacent-cell scan must still catch them.
    const a = node(19, 0, 10);
    const b = node(21, 0, 10);
    run([a, b]);
    expect(a.vx).toBeLessThan(0);
    expect(b.vx).toBeGreaterThan(0);
  });

  it("displaces a smaller node more than a larger one", () => {
    const small = { x: 0, y: 0, vx: 0, vy: 0 };
    const big = { x: 8, y: 0, vx: 0, vy: 0 };
    const radiusOf = (n: any) => (n === big ? 14 : 6);
    const force = makeCollideForce(radiusOf, 0, 1);
    force.initialize([small, big]);
    force();
    expect(Math.abs(small.vx)).toBeGreaterThan(Math.abs(big.vx));
  });
});

describe("makeDepthRepulsionForce", () => {
  const folder = (depth: number, x = 0, y = 0) =>
    ({ x, y, vx: 0, vy: 0, type: "folder", __targetDepth: depth }) as any;
  const note = (depth: number, x: number, y = 0) =>
    ({ x, y, vx: 0, vy: 0, type: "note", __targetDepth: depth }) as any;
  const run = (nodes: any[], strength = 90, distanceMax = 150) => {
    const force = makeDepthRepulsionForce(strength, distanceMax);
    force.initialize(nodes);
    force(1); // alpha = 1
    return nodes;
  };

  it("pushes a deeper node directly away from a folder", () => {
    const f = folder(1);
    const n = note(2, 50);
    run([f, n]);
    expect(n.vx).toBeGreaterThan(0);
    expect(n.vy).toBeCloseTo(0);
  });

  it("moves only the deeper node — the folder holds its ring", () => {
    const f = folder(1);
    const n = note(2, 50);
    run([f, n]);
    expect(f.vx).toBe(0);
    expect(f.vy).toBe(0);
  });

  it("leaves same-depth peers and shallower nodes alone", () => {
    const f = folder(2);
    const peer = note(2, 50);
    const shallower = note(1, -50);
    run([f, peer, shallower]);
    expect(peer.vx).toBe(0);
    expect(shallower.vx).toBe(0);
  });

  it("pushes harder the more nested the folder is", () => {
    const [, fromShallow] = run([folder(1), note(5, 50)]);
    const [, fromDeep] = run([folder(4), note(5, 50)]);
    expect(fromShallow.vx).toBeGreaterThan(0);
    expect(fromDeep.vx).toBeGreaterThan(fromShallow.vx);
  });

  it("only folders repel — a note never pushes deeper nodes", () => {
    const shallow = note(1, 0);
    const deeper = note(2, 50);
    run([shallow, deeper]);
    expect(deeper.vx).toBe(0);
  });

  it("does not let the depth-0 drilled-in root repel", () => {
    const root = folder(0);
    const n = note(1, 50);
    run([root, n]);
    expect(n.vx).toBe(0);
  });

  it("ignores pairs beyond the range cap", () => {
    const f = folder(3);
    const n = note(4, 200);
    run([f, n], 90, 150);
    expect(n.vx).toBe(0);
  });

  it("still reaches a pair that straddles a grid-cell boundary", () => {
    // cellSize = distanceMax = 150: folder at x=149 (cell 0), node at x=151 (cell 1) —
    // different cells but only 2 apart, so the adjacent-cell scan must catch them.
    const f = folder(2, 149);
    const n = note(3, 151);
    run([f, n]);
    expect(n.vx).toBeGreaterThan(0);
  });

  it("skips coincident pairs instead of dividing by zero", () => {
    const f = folder(1);
    const n = note(2, 0); // exactly on the folder — collide owns the separation
    run([f, n]);
    expect(n.vx).toBe(0);
    expect(Number.isFinite(n.vx)).toBe(true);
  });

  it("skips nodes without a stamped depth", () => {
    const f = folder(1);
    const alien = { x: 30, y: 0, vx: 0, vy: 0, type: "note" } as any;
    run([f, alien]);
    expect(alien.vx).toBe(0);
  });

  it("is inert at zero strength", () => {
    const f = folder(1);
    const n = note(2, 50);
    run([f, n], 0);
    expect(n.vx).toBe(0);
  });
});

describe("makeRadialForce", () => {
  const run = (node: any, strength = 1) => {
    const force = makeRadialForce(strength);
    force.initialize([node]);
    force(1); // alpha = 1
    return node;
  };

  it("pushes a node inside its ring outward along the radial direction", () => {
    const n = { x: 10, y: 0, vx: 0, vy: 0, __targetR: 50, __targetTheta: 0 };
    run(n);
    expect(n.vx).toBeGreaterThan(0);
    expect(n.vy).toBeCloseTo(0);
  });

  it("pulls a node outside its ring back inward", () => {
    const n = { x: 100, y: 0, vx: 0, vy: 0, __targetR: 50, __targetTheta: 0 };
    run(n);
    expect(n.vx).toBeLessThan(0);
    expect(n.vy).toBeCloseTo(0);
  });

  it("pulls the depth-0 root straight toward the origin", () => {
    const n = { x: 30, y: -40, vx: 0, vy: 0, __targetR: 0, __targetTheta: 0 };
    run(n);
    expect(n.vx).toBeLessThan(0);
    expect(n.vy).toBeGreaterThan(0);
  });

  it("swings a node toward its wedge midpoint the short way around the wrap", () => {
    // Node at angle 0, target just below the 2π wrap: the short way is clockwise
    // (negative y), not a full counter-clockwise lap.
    const n = { x: 50, y: 0, vx: 0, vy: 0, __targetR: 50, __targetTheta: 2 * Math.PI - 0.2 };
    run(n);
    expect(n.vy).toBeLessThan(0);
  });

  it("exits a node parked exactly on the origin along its wedge midpoint", () => {
    const n = { x: 0, y: 0, vx: 0, vy: 0, __targetR: 50, __targetTheta: Math.PI / 2 };
    run(n);
    expect(n.vy).toBeGreaterThan(0);
    expect(n.vx).toBeCloseTo(0);
  });

  it("skips nodes without a stamped target", () => {
    const n = { x: 10, y: 0, vx: 0, vy: 0 };
    run(n);
    expect(n.vx).toBe(0);
    expect(n.vy).toBe(0);
  });

  it("is inert at zero strength", () => {
    const n = { x: 10, y: 0, vx: 0, vy: 0, __targetR: 50, __targetTheta: 0 };
    run(n, 0);
    expect(n.vx).toBe(0);
    expect(n.vy).toBe(0);
  });
});
