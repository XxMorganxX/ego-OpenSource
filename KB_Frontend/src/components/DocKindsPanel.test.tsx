import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { DocKindsPanel } from "./DocKindsPanel";

const fetchDocKinds = vi.fn();
const fetchDocKindMemories = vi.fn();
vi.mock("../api/client", () => ({
  fetchDocKinds: (all: boolean) => fetchDocKinds(all),
  fetchDocKindMemories: (kind: string) => fetchDocKindMemories(kind),
}));
vi.mock("../hooks/useChangeFeed", () => ({ useChangeFeed: () => {} }));

const KINDS = [
  { kind: "meeting notes", count: 5, established: true },
  { kind: "reference", count: 3, established: true },
];

beforeEach(() => {
  fetchDocKinds.mockReset().mockResolvedValue({ kinds: KINDS });
  fetchDocKindMemories.mockReset().mockResolvedValue({
    memories: [
      { id: "m1", path: "notes.standup", parent_id: null, type: "note",
        slug: "standup", title: "Standup sync", tags: [], importance: 1,
        doc_kind: "meeting notes" },
    ],
  });
});

describe("DocKindsPanel", () => {
  it("lists kinds ranked with counts", async () => {
    render(<DocKindsPanel />);
    expect(await screen.findByText("meeting notes")).toBeInTheDocument();
    expect(screen.getByText("5 memories")).toBeInTheDocument();
    expect(fetchDocKinds).toHaveBeenCalledWith(false);
  });

  it("long-tail toggle refetches with all=true", async () => {
    render(<DocKindsPanel />);
    await screen.findByText("meeting notes");
    await userEvent.click(screen.getByRole("button", { name: "All" }));
    await waitFor(() => expect(fetchDocKinds).toHaveBeenCalledWith(true));
  });

  it("clicking a kind loads its memories", async () => {
    render(<DocKindsPanel />);
    await userEvent.click(await screen.findByText("meeting notes"));
    expect(await screen.findByText("Standup sync")).toBeInTheDocument();
    expect(fetchDocKindMemories).toHaveBeenCalledWith("meeting notes");
  });
});
