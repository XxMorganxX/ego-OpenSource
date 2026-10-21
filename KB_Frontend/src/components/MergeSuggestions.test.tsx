import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MergeSuggestion } from "../types";

const fetchMergeSuggestions = vi.fn();
const approveMerge = vi.fn();
const rejectMerge = vi.fn();
vi.mock("../api/client", () => ({
  fetchMergeSuggestions: () => fetchMergeSuggestions(),
  approveMerge: (id: number, targetId?: string) => approveMerge(id, targetId),
  rejectMerge: (id: number, reason?: string) => rejectMerge(id, reason),
}));

import { MergeSuggestions } from "./MergeSuggestions";

const suggestion: MergeSuggestion = {
  id: 42,
  kind: "semantic",
  distance: 0.12,
  judge_reason: "Very similar names and co-occurrence",
  suggested_target_id: "entity-left-id",
  left: { id: "entity-left-id", name: "React", aliases: ["ReactJS"], mention_count: 10 },
  right: { id: "entity-right-id", name: "React.js", aliases: [], mention_count: 5 },
};

describe("MergeSuggestions", () => {
  beforeEach(() => {
    fetchMergeSuggestions.mockReset().mockResolvedValue([suggestion]);
    approveMerge.mockReset().mockResolvedValue({ merged: true, target_id: "entity-left-id" });
    rejectMerge.mockReset().mockResolvedValue({ rejected: true });
  });

  it("shows both entity names and the judge reason", async () => {
    render(<MergeSuggestions />);
    await screen.findByText("React");
    expect(screen.getByText("React.js")).toBeInTheDocument();
    expect(screen.getByText(/Very similar names/i)).toBeInTheDocument();
  });

  it("shows mention counts and aliases", async () => {
    render(<MergeSuggestions />);
    await screen.findByText("React");
    expect(screen.getByText(/ReactJS/i)).toBeInTheDocument();
    expect(screen.getByText(/10/)).toBeInTheDocument();
    expect(screen.getByText(/5/)).toBeInTheDocument();
  });

  it("shows empty state when no suggestions", async () => {
    fetchMergeSuggestions.mockResolvedValue([]);
    render(<MergeSuggestions />);
    await screen.findByText(/No pending merges/i);
  });

  it("clicking Reject calls rejectMerge and removes the row", async () => {
    render(<MergeSuggestions />);
    await screen.findByText("React");
    await userEvent.click(screen.getByRole("button", { name: /reject/i }));
    await waitFor(() => expect(rejectMerge).toHaveBeenCalledWith(42, undefined));
    await waitFor(() => expect(screen.queryByText("React")).not.toBeInTheDocument());
  });

  it("clicking Approve calls approveMerge with chosen target and removes the row", async () => {
    render(<MergeSuggestions />);
    await screen.findByText("React");
    // The default target is suggested_target_id = "entity-left-id" (left)
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() =>
      expect(approveMerge).toHaveBeenCalledWith(42, "entity-left-id"),
    );
    await waitFor(() => expect(screen.queryByText("React")).not.toBeInTheDocument());
  });

  it("switching target radio changes the approve target", async () => {
    render(<MergeSuggestions />);
    await screen.findByText("React");
    // Select the right entity as target
    await userEvent.click(screen.getByRole("radio", { name: /React\.js/i }));
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() =>
      expect(approveMerge).toHaveBeenCalledWith(42, "entity-right-id"),
    );
  });
});
