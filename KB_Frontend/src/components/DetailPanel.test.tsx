import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { NodeDetail } from "../types";

const fetchNode = vi.fn();
const fetchNodeMemories = vi.fn();
const updateNode = vi.fn();
const deleteNode = vi.fn();
const reanalyzeNode = vi.fn();
const setAnalysisExcluded = vi.fn();
const clearAnalysisExcluded = vi.fn();
const setSignificance = vi.fn();
vi.mock("../api/client", () => ({
  fetchNode: (id: string) => fetchNode(id),
  fetchNodeMemories: (id: string, opts?: unknown) => fetchNodeMemories(id, opts),
  updateNode: (id: string, patch: unknown) => updateNode(id, patch),
  deleteNode: (id: string, recursive: boolean) => deleteNode(id, recursive),
  reanalyzeNode: (id: string) => reanalyzeNode(id),
  setAnalysisExcluded: (id: string, opts: unknown) => setAnalysisExcluded(id, opts),
  clearAnalysisExcluded: (id: string) => clearAnalysisExcluded(id),
  setSignificance: (id: string, value: number) => setSignificance(id, value),
}));

// Isolate DetailPanel from ClassCuration's own data fetching; render the footer slot so
// the Found-in wiring for entity classes stays testable.
vi.mock("./ClassCuration", () => ({
  ClassCuration: ({ footer, readOnly }: { footer?: unknown; readOnly?: boolean }) => (
    <div data-testid="class-curation" data-readonly={String(!!readOnly)}>
      {footer as never}
    </div>
  ),
}));

import { DetailPanel } from "./DetailPanel";

const node: NodeDetail = {
  id: "n1", path: "notes.doc", parent_id: "p", type: "note", slug: "doc",
  title: "Doc", tags: [], importance: 2.5, content: "body text", summary: null,
  uri: null, source: "obsidian", external_id: "notes/doc.md",
};

const entity: NodeDetail = {
  id: "e1", path: "entities.people.avery", parent_id: "c", type: "entity", slug: "avery",
  title: "Avery", tags: [], importance: 1, content: "a person", summary: null,
  uri: null, source: "entity", external_id: "person:avery",
};

const folder: NodeDetail = {
  id: "f1", path: "scratch", parent_id: null, type: "folder", slug: "scratch",
  title: null, tags: [], importance: 0, content: "", summary: null,
  uri: null, source: "tree", external_id: "scratch",
  analysis_excluded: false, analysis_excluded_effective: false,
  excluded_by_path: null, descendant_doc_count: 14,
};

const scored: NodeDetail = {
  ...node,
  importance: 2.75,
  importance_modifiers: { manual: 0.5, access: 0.25, inherited: 1.0 },
  factors: [
    { name: "manual", value: 0.5, coefficient: 1.0, contribution: 0.5 },
    { name: "access", value: 0.5, coefficient: 0.5, contribution: 0.25 },
    { name: "inherited", value: 1.0, coefficient: 1.0, contribution: 1.0 },
  ],
  inherited_from: [{ path: "projects.kb", value: 1.0 }],
  effective_importance: 2.5,
  age_days: 4,
  descendant_count: 0,
};

const dormant: NodeDetail = {
  ...scored,
  importance: 1.5,
  importance_modifiers: { manual: 0, access: 0, inherited: 1.0, project_dormancy: 0.5 },
  factors: [
    { name: "manual", value: 0, coefficient: 1.0, contribution: 0 },
    { name: "access", value: 0, coefficient: 0.5, contribution: 0 },
    { name: "inherited", value: 1.0, coefficient: 1.0, contribution: 1.0 },
    { name: "project_dormancy", value: 0.5, coefficient: -1.0, contribution: -0.5 },
  ],
  project_dormant_days: 183,
};

/** One LinkedMemory page-row for the paged "Found in" tests. */
const lm = (i: number, over: Record<string, unknown> = {}) => ({
  id: `m${i}`, path: `notes.m${i}`, parent_id: "p", type: "note", slug: `m${i}`,
  title: `Mem ${i}`, tags: [], importance: 1,
  link_type: "mentions", weight: 0.5, event_at: "2025-03-03T00:00:00+00:00",
  updated_at: "2025-03-10T00:00:00+00:00", ...over,
});

describe("DetailPanel", () => {
  beforeEach(() => {
    fetchNode.mockReset().mockResolvedValue(node);
    fetchNodeMemories.mockReset().mockResolvedValue({ memories: [], total: 0 });
    updateNode.mockReset().mockResolvedValue({ ...node, title: "Edited" });
    deleteNode.mockReset().mockResolvedValue({ deleted: 1 });
    reanalyzeNode.mockReset().mockResolvedValue({ requested: true, node_id: "n1" });
    setAnalysisExcluded.mockReset().mockResolvedValue({ ...node, analysis_excluded: true,
      analysis_excluded_effective: true, excluded_by_path: null, descendant_doc_count: 1 });
    clearAnalysisExcluded.mockReset().mockResolvedValue({ ...node, analysis_excluded: false,
      analysis_excluded_effective: false, excluded_by_path: null, descendant_doc_count: 1 });
    setSignificance.mockReset().mockResolvedValue({ ...node });
  });

  it("loads the node and saves an edited title", async () => {
    const onChanged = vi.fn();
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={onChanged} />);
    await waitFor(() => expect(screen.getByDisplayValue("Doc")).toBeInTheDocument());
    await userEvent.clear(screen.getByLabelText(/title/i));
    await userEvent.type(screen.getByLabelText(/title/i), "Edited");
    await userEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(updateNode).toHaveBeenCalledWith("n1", expect.objectContaining({ title: "Edited" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("shows the original file name so a renamed note still reveals its origin", async () => {
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("Original file")).toBeInTheDocument());
    expect(screen.getByText("notes/doc.md")).toBeInTheDocument();
  });

  it("omits the original-file row when the external id isn't a file (e.g. a UUID)", async () => {
    fetchNode.mockResolvedValue({
      ...node,
      source: "agent",
      external_id: "baa1bb47-49c1-4610-87d2-cb32419cbde2",
    });
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByDisplayValue("Doc")).toBeInTheDocument());
    expect(screen.queryByText("Original file")).toBeNull();
  });

  it("omits the original-file row for entities (no source file)", async () => {
    fetchNode.mockResolvedValue(entity);
    render(<DetailPanel isOwner nodeId="e1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/found in/i)).toBeInTheDocument());
    expect(screen.queryByText("Original file")).toBeNull();
  });

  it("shows the node's significance score", async () => {
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("Significance")).toBeInTheDocument());
    expect(screen.getByText("2.50")).toBeInTheDocument();
  });

  it("lists the memories an entity was found in and opens one on click", async () => {
    fetchNode.mockResolvedValue(entity);
    fetchNodeMemories.mockResolvedValue({ memories: [lm(1, { title: "Doc" })], total: 1 });
    const onSelectNode = vi.fn();
    render(<DetailPanel isOwner nodeId="e1" onClose={vi.fn()} onChanged={vi.fn()} onSelectNode={onSelectNode} />);
    await waitFor(() => expect(screen.getByText(/found in/i)).toBeInTheDocument());
    expect(fetchNodeMemories).toHaveBeenCalledWith("e1", { limit: 25 });
    await userEvent.click(await screen.findByText("Doc"));
    expect(onSelectNode).toHaveBeenCalledWith("m1");
  });

  it("renders the first page with total, dates and link-type tags", async () => {
    fetchNode.mockResolvedValue(entity);
    fetchNodeMemories.mockResolvedValue({
      memories: [lm(1), lm(2, { link_type: "belongs_to", event_at: null })],
      total: 7,
    });
    render(<DetailPanel isOwner nodeId="e1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("Found in · 7")).toBeInTheDocument());
    expect(fetchNodeMemories).toHaveBeenCalledWith("e1", { limit: 25 });
    expect(screen.getByText("Mem 1")).toBeInTheDocument();
    expect(screen.getByText("mention")).toBeInTheDocument();
    expect(screen.getByText("member")).toBeInTheDocument(); // belongs_to tag
    expect(screen.getByRole("button", { name: /show more \(5 remaining\)/i }))
      .toBeInTheDocument();
  });

  it("appends the next page on Show more and hides the button when done", async () => {
    fetchNode.mockResolvedValue(entity);
    fetchNodeMemories
      .mockResolvedValueOnce({ memories: [lm(1)], total: 2 })
      .mockResolvedValueOnce({ memories: [lm(2)], total: 2 });
    render(<DetailPanel isOwner nodeId="e1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("Mem 1")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /show more/i }));
    await waitFor(() => expect(screen.getByText("Mem 2")).toBeInTheDocument());
    expect(fetchNodeMemories).toHaveBeenLastCalledWith("e1", { limit: 25, offset: 1 });
    expect(screen.getByText("Mem 1")).toBeInTheDocument(); // page 1 kept
    expect(screen.queryByRole("button", { name: /show more/i })).toBeNull();
  });

  it("shows the Found-in list under class curation for entity classes", async () => {
    fetchNode.mockResolvedValue({ ...entity, id: "c1", title: "people" });
    fetchNodeMemories.mockResolvedValue({
      memories: [lm(1, { link_type: "belongs_to" })],
      total: 3,
    });
    render(
      <DetailPanel isOwner nodeId="c1" categoryName="people" onClose={vi.fn()} onChanged={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getByText("Found in · 3")).toBeInTheDocument());
    expect(screen.getByTestId("class-curation")).toBeInTheDocument();
    expect(screen.getByText("member")).toBeInTheDocument();
  });

  it("drops class-curation controls for a tenant and keeps them for the owner", async () => {
    fetchNode.mockResolvedValue({ ...entity, id: "c1", title: "people" });
    fetchNodeMemories.mockResolvedValue({ memories: [], total: 0 });
    const { unmount } = render(
      <DetailPanel isOwner nodeId="c1" categoryName="people" onClose={vi.fn()} onChanged={vi.fn()} />,
    );
    expect(await screen.findByTestId("class-curation")).toHaveAttribute("data-readonly", "false");
    unmount();
    render(
      <DetailPanel
        nodeId="c1"
        categoryName="people"
        isOwner={false}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );
    expect(await screen.findByTestId("class-curation")).toHaveAttribute("data-readonly", "true");
  });

  it("shows a retry on failure and recovers", async () => {
    fetchNode.mockResolvedValue(entity);
    fetchNodeMemories
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValueOnce({ memories: [lm(1)], total: 1 });
    render(<DetailPanel isOwner nodeId="e1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByText(/couldn't load memories/i)).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(screen.getByText("Mem 1")).toBeInTheDocument());
  });

  it("does not query linked memories for non-entity nodes", async () => {
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByDisplayValue("Doc")).toBeInTheDocument());
    expect(fetchNodeMemories).not.toHaveBeenCalled();
    expect(screen.queryByText(/found in/i)).toBeNull();
  });

  it("re-crawls the selected memory on demand", async () => {
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByDisplayValue("Doc")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /re-crawl/i }));
    expect(reanalyzeNode).toHaveBeenCalledWith("n1");
    await waitFor(() =>
      expect(screen.getByText(/re-analyzing in the background/i)).toBeInTheDocument(),
    );
  });

  it("offers no re-crawl for entities (only memories are crawlable)", async () => {
    fetchNode.mockResolvedValue(entity);
    render(<DetailPanel isOwner nodeId="e1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/found in/i)).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /re-crawl/i })).toBeNull();
  });

  it("offers no re-crawl for retired (dead) memories", async () => {
    fetchNode.mockResolvedValue({ ...node, lifecycle: "dead" });
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByDisplayValue("Doc")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /re-crawl/i })).toBeNull();
  });

  it("offers going-forward and retract when excluding a document", async () => {
    const onChanged = vi.fn();
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={onChanged} />);
    await waitFor(() => expect(screen.getByDisplayValue("Doc")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /exclude from entity analysis/i }));
    await userEvent.click(screen.getByRole("button", { name: /exclude going forward/i }));
    expect(setAnalysisExcluded).toHaveBeenCalledWith("n1", { retract: false });
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("passes retract: true when 'retract past entities' is chosen", async () => {
    const onChanged = vi.fn();
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={onChanged} />);
    await waitFor(() => expect(screen.getByDisplayValue("Doc")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /exclude from entity analysis/i }));
    await userEvent.click(screen.getByRole("button", { name: /retract past entities/i }));
    expect(setAnalysisExcluded).toHaveBeenCalledWith("n1", { retract: true });
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("shows Include and clears the flag for an already-excluded node", async () => {
    fetchNode.mockResolvedValue({ ...node, analysis_excluded: true,
      analysis_excluded_effective: true, excluded_by_path: null, descendant_doc_count: 1 });
    const onChanged = vi.fn();
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={onChanged} />);
    await waitFor(() => expect(screen.getByDisplayValue("Doc")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /include in entity analysis/i }));
    expect(clearAnalysisExcluded).toHaveBeenCalledWith("n1");
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("shows an inherited exclusion read-only with the folder path", async () => {
    fetchNode.mockResolvedValue({ ...node, analysis_excluded: false,
      analysis_excluded_effective: true, excluded_by_path: "scratch", descendant_doc_count: 1 });
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/excluded via folder/i)).toBeInTheDocument());
    expect(screen.getByText("scratch")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /exclude from entity analysis/i })).toBeNull();
  });

  it("does not show the exclude control for entities", async () => {
    fetchNode.mockResolvedValue(entity);
    render(<DetailPanel isOwner nodeId="e1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/found in/i)).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /exclude from entity analysis/i })).toBeNull();
  });

  it("shows the affected doc count when excluding a folder", async () => {
    fetchNode.mockResolvedValue(folder);
    render(<DetailPanel isOwner nodeId="f1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /exclude from entity analysis/i }))
        .toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: /exclude from entity analysis/i }));
    expect(screen.getByText(/applies to 14 docs/i)).toBeInTheDocument();
  });

  it("shows the significance factor breakdown with inherited sources", async () => {
    fetchNode.mockResolvedValue(scored);
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("inherited")).toBeInTheDocument());
    expect(screen.getByText("+1.00")).toBeInTheDocument();         // inherited contribution
    expect(screen.getByText(/projects\.kb/)).toBeInTheDocument();  // its source
  });

  it("explains the project_dormancy factor with how long the project has been quiet", async () => {
    fetchNode.mockResolvedValue(dormant);
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("project_dormancy")).toBeInTheDocument());
    expect(screen.getByText("-0.50")).toBeInTheDocument();          // the removed slice
    expect(screen.getByText(/project quiet 183d/)).toBeInTheDocument();
  });

  it("applies an entered significance value", async () => {
    fetchNode.mockResolvedValue(scored);
    const onChanged = vi.fn();
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={onChanged} />);
    await waitFor(() => expect(screen.getByRole("spinbutton")).toBeInTheDocument());
    await userEvent.clear(screen.getByRole("spinbutton"));
    await userEvent.type(screen.getByRole("spinbutton"), "2");
    await userEvent.click(screen.getByRole("button", { name: /apply/i }));
    expect(setSignificance).toHaveBeenCalledWith("n1", 2);
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("labels the folder significance editor with the descendant count", async () => {
    fetchNode.mockResolvedValue({ ...folder, descendant_count: 14 });
    render(<DetailPanel isOwner nodeId="f1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/everything inside it/i)).toBeInTheDocument());
    expect(screen.getByText(/14 items/i)).toBeInTheDocument();
  });

  it("shows a failure note and does not call onChanged when setSignificance rejects", async () => {
    fetchNode.mockResolvedValue(scored);
    setSignificance.mockReset().mockRejectedValue(new Error("boom"));
    const onChanged = vi.fn();
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={onChanged} />);
    await waitFor(() => expect(screen.getByRole("spinbutton")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /apply/i }));
    await waitFor(() =>
      expect(screen.getByText(/failed to apply significance/i)).toBeInTheDocument(),
    );
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("shows the doc_kind badge next to the type badge", async () => {
    fetchNode.mockResolvedValue({ ...node, doc_kind: "meeting notes" });
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={vi.fn()} />);
    expect(await screen.findByText("meeting notes")).toBeInTheDocument();
  });

  it("renders no doc_kind badge when the node has none", async () => {
    fetchNode.mockResolvedValue({ ...node, doc_kind: null });
    render(<DetailPanel isOwner nodeId="n1" onClose={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByDisplayValue("Doc")).toBeInTheDocument());
    expect(document.querySelector(".badge.dockind")).toBeNull();
  });
});
