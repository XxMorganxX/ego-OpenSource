import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  updateConceptLabel: vi.fn(() => Promise.resolve({})),
  dismissConcept: vi.fn(() => Promise.resolve({})),
  removeConceptMember: vi.fn(() => Promise.resolve({})),
}));
vi.mock("../api/client", () => api);

import type { Concept } from "../types";
import { ConceptCard } from "./ConceptCard";

const concept: Concept = {
  id: "c1",
  name: "KB project",
  kind: "project",
  level: 1,
  parent_id: "p1",
  description: "engine work",
  member_count: 2,
  members: [
    { node_id: "e1", member_kind: "entity", score: 0.9, label: "Ollama" },
    { node_id: "m1", member_kind: "memory", score: 1.0, label: "Note One" },
  ],
  label_locked: false,
  status: "active",
};

describe("ConceptCard", () => {
  it("shows name, kind, description, parent, and grouped members", () => {
    render(
      <ConceptCard
        concept={concept}
        parentName="Infra work"
        onClose={vi.fn()}
        onSelectNode={vi.fn()}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.getByText("KB project")).toBeInTheDocument();
    expect(screen.getByText("project")).toBeInTheDocument();
    expect(screen.getByText("engine work")).toBeInTheDocument();
    expect(screen.getByText(/part of Infra work/)).toBeInTheDocument();
    expect(screen.getByText("Ollama")).toBeInTheDocument();
    expect(screen.getByText("Note One")).toBeInTheDocument();
    expect(screen.getByText(/derived by consolidation/)).toBeInTheDocument();
  });

  it("opens a member's node detail on click and closes via the button", () => {
    const onSelectNode = vi.fn();
    const onClose = vi.fn();
    render(
      <ConceptCard
        concept={concept}
        parentName={null}
        onClose={onClose}
        onSelectNode={onSelectNode}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("Note One"));
    expect(onSelectNode).toHaveBeenCalledWith("m1");
    fireEvent.click(screen.getByLabelText("Close"));
    expect(onClose).toHaveBeenCalled();
  });

  it("renders nothing without a concept", () => {
    const { container } = render(
      <ConceptCard
        concept={null}
        parentName={null}
        onClose={vi.fn()}
        onSelectNode={vi.fn()}
        onChanged={vi.fn()}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renames with lock, dismisses, and removes members via the API", async () => {
    const onChanged = vi.fn();
    render(
      <ConceptCard
        concept={concept}
        parentName={null}
        onClose={vi.fn()}
        onSelectNode={vi.fn()}
        onChanged={onChanged}
      />,
    );
    fireEvent.click(screen.getByLabelText("Rename concept"));
    fireEvent.change(screen.getByLabelText("Concept name"), {
      target: { value: "Vandelay" },
    });
    fireEvent.click(screen.getByLabelText("Save name"));
    await waitFor(() =>
      expect(api.updateConceptLabel).toHaveBeenCalledWith("c1", { name: "Vandelay" }),
    );
    fireEvent.click(screen.getByLabelText(`Remove Ollama from this concept`));
    await waitFor(() => expect(api.removeConceptMember).toHaveBeenCalledWith("c1", "e1"));
    fireEvent.click(screen.getByLabelText("Dismiss concept"));
    fireEvent.click(screen.getByText("Confirm dismiss"));
    await waitFor(() => expect(api.dismissConcept).toHaveBeenCalledWith("c1"));
    expect(onChanged).toHaveBeenCalled();
  });

  it("shows a lock badge when the label is locked", () => {
    render(
      <ConceptCard
        concept={{ ...concept, label_locked: true }}
        parentName={null}
        onClose={vi.fn()}
        onSelectNode={vi.fn()}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.getByTitle("Label locked by you")).toBeInTheDocument();
  });

  it("shows a doc_kind chip on memory member rows", () => {
    render(
      <ConceptCard
        concept={{
          ...concept,
          members: [
            { node_id: "m1", member_kind: "memory", score: 0.9,
              label: "Standup sync", doc_kind: "meeting notes" },
            { node_id: "m2", member_kind: "memory", score: 0.8,
              label: "Plain note", doc_kind: null },
          ],
        }}
        parentName={null}
        onClose={vi.fn()}
        onSelectNode={vi.fn()}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.getByText("meeting notes")).toBeInTheDocument();
    expect(
      screen.getByText("Plain note").closest("li")!.querySelector(".concept-kind"),
    ).toBeNull();
  });
});
