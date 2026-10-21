import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { forwardRef, useImperativeHandle } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { UNKNOWN_ME } from "./lib/identity";
import type { Concept, EntityGraphData, GraphData, NodeMeta } from "./types";

const fetchTree = vi.fn();
const fetchSubtree = vi.fn();
const fetchEntities = vi.fn();
const fetchConcepts = vi.fn();
const capturedGraph = vi.hoisted(() => ({ graphData: null as GraphData | null }));
vi.mock("react-force-graph-2d", () => ({
  default: forwardRef((p: { graphData: GraphData }, ref: any) => {
    capturedGraph.graphData = p.graphData;
    useImperativeHandle(ref, () => ({ d3Force: () => null, d3ReheatSimulation: () => {} }));
    return <div data-testid="fg">{p.graphData.nodes.length}</div>;
  }),
}));
const fetchNode = vi.fn();
const primeEntities = vi.fn();
const primeConcepts = vi.fn();
const fetchMe = vi.fn();
const fetchClaudeAuth = vi.fn();
const AUTH_OK = { level: "ok", logged_in: true, days_left: 20, expires_at: null, email: null };
const AUTH_OUT = { level: "out", logged_in: false, days_left: 0, expires_at: null, email: null };
vi.mock("./api/client", () => ({
  // null = a pre-tenancy backend (no /api/me), which renders as the owner.
  fetchMe: () => fetchMe(),
  fetchTree: () => fetchTree(),
  fetchSubtree: (p: string) => fetchSubtree(p),
  fetchEntities: () => fetchEntities(),
  fetchConcepts: () => fetchConcepts(),
  primeEntities: () => primeEntities(),
  primeConcepts: () => primeConcepts(),
  fetchNode: (id: string) => fetchNode(id),
  // ReenrichBanner (mounted globally) polls ingest status on mount; idle by default here.
  fetchIngestStatus: () => Promise.resolve({ enrichment: { pending: 0 }, sources: [] }),
  // ClaudeAuthBanner (mounted globally) polls auth status on mount; healthy by default
  // here — its own behavior is covered by ClaudeAuthBanner.test.tsx.
  fetchClaudeAuth: () => fetchClaudeAuth(),
  postClaudeRelogin: () => Promise.resolve({ launched: true }),
  // ConceptRebuild (mounted in the concepts pane) polls consolidation status on mount;
  // idle by default here — its own behavior is covered by ConceptRebuild.test.tsx.
  fetchConsolidationStatus: () =>
    Promise.resolve({ state: "idle", request: null, running: null, last: null, backlog: 0 }),
  requestConsolidation: () =>
    Promise.resolve({ state: "idle", request: null, running: null, last: null, backlog: 0 }),
  // AgeDecayToggle (mounted in the concepts pane) loads on mount; its own behavior
  // is covered by AgeDecayToggle.test.tsx.
  fetchAgeDecay: vi.fn().mockResolvedValue({ enabled: true }),
  setAgeDecay: vi.fn().mockResolvedValue({ enabled: false }),
  fetchPolicy: () =>
    Promise.resolve({ source: null, is_default: true, overlays: [], policy: { decay: [] } }),
}));

vi.mock("./components/SkillsPage", () => ({
  SkillsPage: () => <div>skills page stub</div>,
}));

import App from "./App";

beforeAll(() => import("./components/GraphView"));

const tree: NodeMeta[] = [
  { id: "1", path: "projects", parent_id: null, type: "folder", slug: "projects", title: null, tags: [], importance: 1 },
];

const entities: EntityGraphData = {
  nodes: [
    { id: "root", path: "entities", parent_id: null, type: "folder", slug: "entities", title: null, tags: [], importance: 1, role: "root", entity_type: null },
    { id: "people", path: "entities.people", parent_id: "root", type: "entity", slug: "people", title: null, tags: [], importance: 1, role: "class", entity_type: "class" },
  ],
  links: [{ source: "root", target: "people", kind: "containment" }],
};

const concepts: Concept[] = [{
  id: "c1", name: "KB project", kind: "project", level: 0, parent_id: null,
  description: "engine work", member_count: 2, members: [
    { node_id: "m1", member_kind: "memory", score: 1, label: "Architecture" },
    { node_id: "e1", member_kind: "entity", score: 0.9, label: "Ollama" },
  ], label_locked: false,
  status: "active",
}];

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function capturedGraphIds() {
  const endpointId = (endpoint: string | { id: string }) =>
    typeof endpoint === "string" ? endpoint : endpoint.id;
  return {
    nodes: capturedGraph.graphData?.nodes.map((node) => node.id) ?? [],
    links: capturedGraph.graphData?.links.map((link) =>
      `${endpointId(link.source)}>${endpointId(link.target)}:${link.kind}`,
    ) ?? [],
  };
}

describe("App", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/"); // reset URL-backed view/mode between tests
    fetchTree.mockReset().mockResolvedValue(tree);
    fetchSubtree.mockReset().mockResolvedValue({ nodes: [], links: [] });
    fetchEntities.mockReset().mockResolvedValue(entities);
    fetchConcepts.mockReset().mockResolvedValue({ concepts: [] });
    primeEntities.mockReset();
    primeConcepts.mockReset();
    capturedGraph.graphData = null;
    fetchMe.mockReset().mockResolvedValue(null);
    fetchClaudeAuth.mockReset().mockResolvedValue(AUTH_OK);
    fetchNode.mockReset().mockResolvedValue({
      id: "1", path: "projects", parent_id: null, type: "folder", slug: "projects",
      title: null, tags: [], importance: 1, content: "", summary: null,
      uri: null, source: "tree", external_id: "projects",
      analysis_excluded: false,
      analysis_excluded_effective: false,
      excluded_by_path: null,
      descendant_doc_count: 0,
    });
  });

  it("loads the tree and drives the graph when a folder is clicked", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText("projects")).toBeInTheDocument());
    await userEvent.click(screen.getByText("projects"));
    expect(fetchSubtree).toHaveBeenCalledWith("projects");
    expect(fetchNode).toHaveBeenCalledWith("1");
  });

  it("warms the entity and concept layers once the tree has loaded", async () => {
    const pending = deferred<NodeMeta[]>();
    fetchTree.mockReturnValueOnce(pending.promise);
    render(<App />);
    expect(primeEntities).not.toHaveBeenCalled(); // never competes with the tree itself
    await act(async () => pending.resolve(tree));
    await waitFor(() => expect(primeEntities).toHaveBeenCalledTimes(1));
    expect(primeConcepts).toHaveBeenCalledTimes(1);
  });

  it("does not warm the layer the page opened on, which fetches itself", async () => {
    window.history.replaceState(null, "", "/?mode=entities");
    render(<App />);
    await waitFor(() => expect(primeConcepts).toHaveBeenCalledTimes(1));
    expect(primeEntities).not.toHaveBeenCalled();
  });

  it("switches to the isolated entity graph via the Entities toggle", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText("projects")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Entities" }));
    await waitFor(() => expect(fetchEntities).toHaveBeenCalled());
    // the left pane becomes the entity navigator: an "All entities" item + each class
    expect(await screen.findByText("All entities")).toBeInTheDocument();
    expect(screen.getByText("people")).toBeInTheDocument();
  });

  it("lists top-level concepts and opens the existing concept card", async () => {
    fetchConcepts.mockResolvedValue({ concepts });
    render(<App />);
    await userEvent.click(screen.getByRole("button", { name: "Concepts" }));
    const row = await screen.findByRole("button", { name: /KB project.*2 members/i });
    const graphBeforeSelection = capturedGraphIds();
    await userEvent.click(row);
    expect(screen.getByRole("heading", { name: "KB project" })).toBeInTheDocument();
    expect(row).toHaveAttribute("aria-current", "true");
    expect(screen.getByLabelText("Curation instruction")).toBeInTheDocument();
    expect(capturedGraphIds()).toEqual(graphBeforeSelection);
  });

  it("reports a concepts load failure and retries", async () => {
    fetchConcepts
      .mockRejectedValueOnce(new Error("503: unavailable"))
      .mockResolvedValueOnce({ concepts });
    render(<App />);
    await userEvent.click(screen.getByRole("button", { name: "Concepts" }));
    expect((await screen.findAllByText("Concepts could not be loaded")).length).toBeGreaterThan(0);
    await userEvent.click(screen.getAllByRole("button", { name: "Retry" })[0]);
    expect(await screen.findByText("KB project")).toBeInTheDocument();
    expect(fetchConcepts).toHaveBeenCalledTimes(2);
  });

  it("ignores a stale concepts failure after a newer request succeeds", async () => {
    const older = deferred<{ concepts: Concept[] }>();
    const newer = deferred<{ concepts: Concept[] }>();
    fetchConcepts
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);
    render(<App />);

    await userEvent.click(screen.getByRole("button", { name: "Concepts" }));
    await waitFor(() => expect(fetchConcepts).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByRole("button", { name: "Memories" }));
    await userEvent.click(screen.getByRole("button", { name: "Concepts" }));
    await waitFor(() => expect(fetchConcepts).toHaveBeenCalledTimes(2));

    await act(async () => newer.resolve({ concepts }));
    expect(await screen.findByText("KB project")).toBeInTheDocument();
    await act(async () => older.reject(new Error("stale 503")));

    expect(screen.getByText("KB project")).toBeInTheDocument();
    expect(screen.queryByText("Concepts could not be loaded")).toBeNull();
  });

  it("ignores a stale concepts success after a newer request succeeds", async () => {
    const older = deferred<{ concepts: Concept[] }>();
    const newer = deferred<{ concepts: Concept[] }>();
    const staleConcepts = [{ ...concepts[0], id: "stale", name: "Stale project" }];
    fetchConcepts
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);
    render(<App />);

    await userEvent.click(screen.getByRole("button", { name: "Concepts" }));
    await waitFor(() => expect(fetchConcepts).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByRole("button", { name: "Memories" }));
    await userEvent.click(screen.getByRole("button", { name: "Concepts" }));
    await waitFor(() => expect(fetchConcepts).toHaveBeenCalledTimes(2));

    await act(async () => newer.resolve({ concepts }));
    expect(await screen.findByText("KB project")).toBeInTheDocument();
    await act(async () => older.resolve({ concepts: staleConcepts }));

    expect(screen.getByText("KB project")).toBeInTheDocument();
    expect(screen.queryByText("Stale project")).toBeNull();
  });

  describe("identity-aware chrome", () => {
    it("shows owner-only chrome when the backend has no /api/me", async () => {
      render(<App />);
      expect(await screen.findByRole("button", { name: "Database" })).toBeInTheDocument();
      await userEvent.click(screen.getByRole("button", { name: "Concepts" }));
      expect(await screen.findByLabelText("Curation instruction")).toBeInTheDocument();
    });

    it("hides owner-only chrome from a tenant", async () => {
      fetchMe.mockResolvedValue({ email: "b@x", tenant: "b", is_owner: false });
      render(<App />);
      // Not even for the first paint, before /api/me has answered.
      expect(screen.queryByRole("button", { name: "Database" })).not.toBeInTheDocument();
      await waitFor(() => expect(fetchMe).toHaveBeenCalled());
      await screen.findByText("projects");
      expect(screen.queryByRole("button", { name: "Database" })).not.toBeInTheDocument();
      await userEvent.click(screen.getByRole("button", { name: "Concepts" }));
      await waitFor(() => expect(fetchConcepts).toHaveBeenCalled());
      expect(screen.queryByLabelText("Curation instruction")).not.toBeInTheDocument();
      expect(screen.queryByRole("region", { name: "Curate concepts" })).not.toBeInTheDocument();
    });

    it("shows the owner the Claude auth banner but never a tenant", async () => {
      fetchClaudeAuth.mockResolvedValue(AUTH_OUT);
      const { unmount } = render(<App />);
      expect(await screen.findByText("claude auth login")).toBeInTheDocument();
      unmount();
      fetchClaudeAuth.mockClear();

      fetchMe.mockResolvedValue({ email: "b@x", tenant: "b", is_owner: false });
      render(<App />);
      await screen.findByText("projects");
      await waitFor(() => expect(fetchMe).toHaveBeenCalled());
      expect(screen.queryByText("claude auth login")).not.toBeInTheDocument();
      expect(fetchClaudeAuth).not.toHaveBeenCalled();
    });

    it("fails closed when /api/me cannot be read: no owner-only chrome", async () => {
      fetchMe.mockResolvedValue(UNKNOWN_ME);
      fetchClaudeAuth.mockResolvedValue(AUTH_OUT);
      render(<App />);
      await screen.findByText("projects");
      await waitFor(() => expect(fetchMe).toHaveBeenCalled());
      expect(screen.queryByRole("button", { name: "Database" })).not.toBeInTheDocument();
      expect(fetchClaudeAuth).not.toHaveBeenCalled();
      await userEvent.click(screen.getByRole("button", { name: "Concepts" }));
      await waitFor(() => expect(fetchConcepts).toHaveBeenCalled());
      expect(screen.queryByLabelText("Curation instruction")).not.toBeInTheDocument();
    });

    it("sends a tenant deep-linked to the Database view to the graph", async () => {
      fetchMe.mockResolvedValue({ email: "b@x", tenant: "b", is_owner: false });
      window.history.replaceState(null, "", "/?view=database");
      render(<App />);
      expect(await screen.findByText("projects")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Database" })).not.toBeInTheDocument();
    });

    it("shows the Skills tab to a tenant and opens the page", async () => {
      window.history.replaceState(null, "", "/");
      fetchMe.mockResolvedValue({ email: "b@x", tenant: "b", is_owner: false });
      render(<App />);
      await screen.findByText("projects");
      await userEvent.click(await screen.findByRole("button", { name: "Skills" }));
      expect(await screen.findByText("skills page stub")).toBeInTheDocument();
      expect(new URLSearchParams(window.location.search).get("view")).toBe("skills");
    });
  });

  // jsdom has no matchMedia; installing one that matches puts the app in the
  // mobile (graph-only) layout. All other tests run desktop by its absence.
  describe("mobile layout", () => {
    beforeEach(() => {
      Object.defineProperty(window, "matchMedia", {
        value: (query: string) => ({
          matches: true,
          media: query,
          onchange: null,
          addEventListener: () => {},
          removeEventListener: () => {},
          addListener: () => {},
          removeListener: () => {},
          dispatchEvent: () => false,
        }),
        writable: true,
        configurable: true,
      });
    });
    afterEach(() => {
      delete (window as { matchMedia?: unknown }).matchMedia;
    });

    it("hides the view tabs and locks the view to the graph", async () => {
      window.history.replaceState(null, "", "/?view=database");
      render(<App />);
      expect(screen.queryByRole("navigation", { name: "Views" })).toBeNull();
      // The graph surface renders despite the deep link to the database page.
      expect(await screen.findByRole("group", { name: "Graph mode" })).toBeInTheDocument();
      // The stat + settings fold behind the toolbar disclosure.
      expect(screen.queryByLabelText("Graph settings")).toBeNull();
      await userEvent.click(screen.getByRole("button", { name: "Toggle toolbar" }));
      expect(screen.getByLabelText("Graph settings")).toBeInTheDocument();
    });

    it("opens the explorer drawer and closes it again on selection", async () => {
      const { container } = render(<App />);
      await waitFor(() => expect(screen.getByText("projects")).toBeInTheDocument());
      const explorer = container.querySelector(".explorer")!;
      expect(explorer.classList.contains("closed")).toBe(true);
      await userEvent.click(screen.getByRole("button", { name: "Toggle explorer" }));
      expect(explorer.classList.contains("closed")).toBe(false);
      await userEvent.click(screen.getByText("projects"));
      expect(fetchSubtree).toHaveBeenCalledWith("projects");
      expect(explorer.classList.contains("closed")).toBe(true);
    });
  });
});
