import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Concept } from "../types";
import { ConceptExplorer } from "./ConceptExplorer";

const concept = (overrides: Partial<Concept> & Pick<Concept, "id" | "name">): Concept => ({
  kind: "topic",
  level: 0,
  parent_id: null,
  description: null,
  member_count: 0,
  members: [],
  label_locked: false,
  status: "active",
  ...overrides,
});

const concepts = [
  concept({ id: "z", name: "Zettelkasten", description: "Personal notes", member_count: 4 }),
  concept({ id: "a", name: "AI Systems", kind: "project", description: "Model infrastructure", member_count: 12 }),
  concept({ id: "child", name: "Inference", level: 1, parent_id: "a", description: "Local model serving", member_count: 3 }),
  concept({ id: "child2", name: "Agents", level: 1, parent_id: "a", member_count: 2 }),
  concept({ id: "old", name: "Archived", status: "dismissed" }),
  concept({ id: "old-child", name: "Dismissed child", level: 1, parent_id: "a", status: "dismissed" }),
];

describe("ConceptExplorer", () => {
  it("lists active root concepts alphabetically with kind and member count", () => {
    render(<ConceptExplorer concepts={concepts} selectedConceptId={null} error={null} onSelect={vi.fn()} onRetry={vi.fn()} />);
    const rows = screen.getAllByRole("button", { name: /members/i });
    expect(rows.map((row) => within(row).getByTestId("concept-name").textContent)).toEqual(["AI Systems", "Agents", "Inference", "Zettelkasten"]);
    expect(screen.queryByText("Archived")).toBeNull();
    expect(screen.queryByText("Dismissed child")).toBeNull();
    expect(rows[0]).toHaveAccessibleName(/AI Systems.*Project.*12 members/i);
  });

  it("nests sub-clusters under their root with their description", () => {
    render(<ConceptExplorer concepts={concepts} selectedConceptId={null} error={null} onSelect={vi.fn()} onRetry={vi.fn()} />);
    const child = screen.getByRole("button", { name: /Inference, sub-cluster of AI Systems, Topic, 3 members/i });
    expect(child).toHaveClass("concept-row-child");
    expect(within(child).getByText("Local model serving")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^AI Systems,/i })).not.toHaveClass("concept-row-child");
  });

  it("searches name, kind, and description case-insensitively", async () => {
    render(<ConceptExplorer concepts={concepts} selectedConceptId={null} error={null} onSelect={vi.fn()} onRetry={vi.fn()} />);
    const search = screen.getByRole("searchbox", { name: "Search concepts" });
    await userEvent.type(search, "MODEL");
    expect(screen.getByText("AI Systems")).toBeInTheDocument();
    expect(screen.queryByText("Zettelkasten")).toBeNull();
    await userEvent.clear(search);
    await userEvent.type(search, "topic");
    expect(screen.getByText("Zettelkasten")).toBeInTheDocument();
  });

  it("keeps the root visible when only a sub-cluster matches, showing just matching children", async () => {
    render(<ConceptExplorer concepts={concepts} selectedConceptId={null} error={null} onSelect={vi.fn()} onRetry={vi.fn()} />);
    await userEvent.type(screen.getByRole("searchbox"), "serving");
    expect(screen.getByText("AI Systems")).toBeInTheDocument();
    expect(screen.getByText("Inference")).toBeInTheDocument();
    expect(screen.queryByText("Agents")).toBeNull();
    expect(screen.queryByText("Zettelkasten")).toBeNull();
  });

  it("marks selection and reports row activation for roots and sub-clusters", async () => {
    const onSelect = vi.fn();
    const { rerender } = render(<ConceptExplorer concepts={concepts} selectedConceptId="a" error={null} onSelect={onSelect} onRetry={vi.fn()} />);
    const row = screen.getByRole("button", { name: /^AI Systems/i });
    expect(row).toHaveAttribute("aria-current", "true");
    await userEvent.click(row);
    expect(onSelect).toHaveBeenCalledWith("a");

    rerender(<ConceptExplorer concepts={concepts} selectedConceptId="child" error={null} onSelect={onSelect} onRetry={vi.fn()} />);
    const childRow = screen.getByRole("button", { name: /^Inference/i });
    expect(childRow).toHaveAttribute("aria-current", "true");
    await userEvent.click(childRow);
    expect(onSelect).toHaveBeenCalledWith("child");
  });

  it("renders loading, failure with retry, empty, and no-match states", async () => {
    const { rerender } = render(<ConceptExplorer concepts={null} selectedConceptId={null} error={null} onSelect={vi.fn()} onRetry={vi.fn()} />);
    expect(screen.getByText("Loading concepts…")).toBeInTheDocument();

    const onRetry = vi.fn();
    rerender(<ConceptExplorer concepts={null} selectedConceptId={null} error="503" onSelect={vi.fn()} onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledOnce();

    rerender(<ConceptExplorer concepts={[]} selectedConceptId={null} error={null} onSelect={vi.fn()} onRetry={vi.fn()} />);
    expect(screen.getByText("No top-level concepts yet")).toBeInTheDocument();

    rerender(<ConceptExplorer concepts={concepts} selectedConceptId={null} error={null} onSelect={vi.fn()} onRetry={vi.fn()} />);
    await userEvent.type(screen.getByRole("searchbox"), "unknown");
    expect(screen.getByText("No concepts match “unknown”")).toBeInTheDocument();
  });

  it("shows a concept whose parent did not survive the run", () => {
    // run_once drops a community when its label is incoherent or its previous concept was
    // dismissed; its level-1 children were still persisted pointing at the absent parent.
    // Such a concept matched neither roots (which required level 0) nor childrenOf (which
    // skips null parents), so it rendered nowhere. Such concepts were invisible.
    render(<ConceptExplorer concepts={[...concepts, concept({ id: "orphan", name: "tide-chart", level: 1, parent_id: null, member_count: 7 })]} selectedConceptId={null} error={null} onSelect={vi.fn()} onRetry={vi.fn()} />);
    const row = screen.getByRole("button", { name: /tide-chart/i });
    expect(row).toBeInTheDocument();
    expect(row).not.toHaveClass("concept-row-child");
  });

  it("shows a concept whose parent is no longer active", () => {
    // Same dead zone reached the other way: a non-null parent_id pointing at a dismissed
    // or retired concept is in childrenOf, but no active root ever claims it.
    render(<ConceptExplorer concepts={[...concepts, concept({ id: "stranded", name: "Stranded child", level: 1, parent_id: "old", member_count: 5 })]} selectedConceptId={null} error={null} onSelect={vi.fn()} onRetry={vi.fn()} />);
    expect(screen.getByRole("button", { name: /Stranded child/i })).toBeInTheDocument();
  });

  it("renders every active concept exactly once", () => {
    // The structural guarantee behind both cases above: roots and children are complements.
    render(<ConceptExplorer concepts={[...concepts, concept({ id: "orphan", name: "tide-chart", level: 1, parent_id: null }), concept({ id: "stranded", name: "Stranded child", level: 1, parent_id: "old" })]} selectedConceptId={null} error={null} onSelect={vi.fn()} onRetry={vi.fn()} />);
    const names = screen.getAllByTestId("concept-name").map((n) => n.textContent);
    const activeNames = ["AI Systems", "Agents", "Inference", "Zettelkasten", "tide-chart", "Stranded child"];
    expect(names.slice().sort()).toEqual(activeNames.slice().sort());
    expect(new Set(names).size).toBe(names.length);
  });
});
