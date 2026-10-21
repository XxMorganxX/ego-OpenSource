import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  parseCuration: vi.fn(),
  applyCuration: vi.fn(() => Promise.resolve({ applied: [{}], errors: [] })),
}));
vi.mock("../api/client", () => api);

import { ConceptCommandBar } from "./ConceptCommandBar";

describe("ConceptCommandBar", () => {
  it("parses, previews, applies, and notifies", async () => {
    api.parseCuration.mockResolvedValue({
      ops: [{ op: "cannot_link", left_id: "e1", right_id: "e2" }],
      summary: ["cannot-link: 'Vandelay' and 'Acme' will never share a concept"],
      warnings: ["'old thing' did not match anything"],
    });
    const onApplied = vi.fn();
    render(<ConceptCommandBar onApplied={onApplied} />);
    fireEvent.change(screen.getByLabelText("Curation instruction"),
                     { target: { value: "split vandelay from acme" } });
    fireEvent.click(screen.getByText("Preview"));
    await waitFor(() =>
      expect(screen.getByText(/never share a concept/)).toBeInTheDocument());
    expect(screen.getByText(/did not match anything/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Apply"));
    await waitFor(() => expect(api.applyCuration).toHaveBeenCalledWith(
      [{ op: "cannot_link", left_id: "e1", right_id: "e2" }]));
    expect(onApplied).toHaveBeenCalled();
  });

  it("shows the fail-closed message when parsing 503s and never applies", async () => {
    api.parseCuration.mockRejectedValue(new Error("503: inference unavailable"));
    render(<ConceptCommandBar onApplied={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Curation instruction"),
                     { target: { value: "anything" } });
    fireEvent.click(screen.getByText("Preview"));
    await waitFor(() =>
      expect(screen.getByText(/inference unavailable/)).toBeInTheDocument());
    expect(screen.queryByText("Apply")).toBeNull();
  });

  it("offers Cancel to discard a preview", async () => {
    api.parseCuration.mockResolvedValue({ ops: [], summary: [], warnings: [] });
    render(<ConceptCommandBar onApplied={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Curation instruction"),
                     { target: { value: "noop" } });
    fireEvent.click(screen.getByText("Preview"));
    await waitFor(() => expect(screen.getByText(/No actionable ops/)).toBeInTheDocument());
  });
});
