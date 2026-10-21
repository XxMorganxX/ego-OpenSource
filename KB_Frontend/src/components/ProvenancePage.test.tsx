import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ProvenanceEntity } from "../types";

const fetchProvenance = vi.fn();
vi.mock("../api/client", () => ({ fetchProvenance: () => fetchProvenance() }));

// Capture the change-feed callback so a live "nodes changed" event can be fired by hand
// (the real socket no-ops under jsdom).
let feedCb: ((tables: Set<string>) => void) | undefined;
vi.mock("../hooks/useChangeFeed", () => ({
  useChangeFeed: (cb: (tables: Set<string>) => void) => {
    feedCb = cb;
  },
}));

import { ProvenancePage } from "./ProvenancePage";

function entity(over: Partial<ProvenanceEntity> & { id: string; name: string }): ProvenanceEntity {
  return {
    path: `entities.${over.name}`, parent_id: null, type: "entity", slug: over.name,
    title: over.name, tags: [], importance: 1, entity_type: "instance", status: "active",
    summary: null, memories: [], ...over,
  };
}

const avery = entity({
  id: "e-avery",
  name: "Avery",
  entity_type: "instance",
  memories: [
    {
      id: "m1", path: "notes.m1", parent_id: null, type: "note", slug: "m1",
      title: "Kickoff call", tags: [], importance: 2, summary: null, link_type: "mentions",
    },
    {
      id: "m2", path: "notes.m2", parent_id: null, type: "note", slug: "m2",
      title: "Standup", tags: [], importance: 1, summary: null, link_type: "mentions",
    },
  ],
});

const people = entity({
  id: "e-people",
  name: "people",
  entity_type: "class",
  memories: [
    {
      id: "m1", path: "notes.m1", parent_id: null, type: "note", slug: "m1",
      title: "Kickoff call", tags: [], importance: 2, summary: null, link_type: "belongs_to",
    },
  ],
});

describe("ProvenancePage", () => {
  beforeEach(() => {
    feedCb = undefined;
    fetchProvenance.mockReset().mockResolvedValue({ entities: [avery, people] });
  });

  it("groups each active entity with the memories it came from", async () => {
    render(<ProvenancePage />);
    await screen.findByText("Avery");
    expect(screen.getByText("people")).toBeInTheDocument();
    // Avery's two source memories appear; the shared one (Kickoff call) appears under both.
    expect(screen.getAllByText("Kickoff call").length).toBe(2);
    expect(screen.getByText("Standup")).toBeInTheDocument();
    // The link_type is surfaced as a human label (mentions vs classified).
    expect(screen.getAllByText(/mentions/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/classified/i)).toBeInTheDocument();
  });

  it("filters to classes only", async () => {
    render(<ProvenancePage />);
    await screen.findByText("Avery");
    await userEvent.click(screen.getByRole("button", { name: "Classes" }));
    expect(screen.getByText("people")).toBeInTheDocument();
    expect(screen.queryByText("Avery")).not.toBeInTheDocument();
  });

  it("filters by a search query over entity and memory text", async () => {
    render(<ProvenancePage />);
    await screen.findByText("Avery");
    await userEvent.type(screen.getByLabelText(/filter provenance/i), "Standup");
    // Only Avery has a "Standup" memory.
    expect(screen.getByText("Avery")).toBeInTheDocument();
    expect(screen.queryByText("people")).not.toBeInTheDocument();
  });

  it("shows an empty state when there are no active entities", async () => {
    fetchProvenance.mockResolvedValue({ entities: [] });
    render(<ProvenancePage />);
    await screen.findByText(/No active entities yet/i);
  });

  it("refetches when the live feed reports a nodes change", async () => {
    render(<ProvenancePage />);
    await screen.findByText("Avery");
    expect(fetchProvenance).toHaveBeenCalledTimes(1);
    await act(async () => {
      feedCb!(new Set(["nodes"]));
    });
    await waitFor(() => expect(fetchProvenance).toHaveBeenCalledTimes(2));
  });
});
