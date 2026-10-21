import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ClassDetail, ImpactReport } from "../types";

const fetchClass = vi.fn();
const fetchClasses = vi.fn();
const previewCuration = vi.fn();
const applyClassCuration = vi.fn();
vi.mock("../api/client", () => ({
  fetchClass: (name: string) => fetchClass(name),
  fetchClasses: () => fetchClasses(),
  previewCuration: (a: string, args: Record<string, string>) => previewCuration(a, args),
  applyClassCuration: (a: string, args: Record<string, string>) => applyClassCuration(a, args),
}));

import { ClassCuration } from "./ClassCuration";

const cls: ClassDetail = {
  name: "frameworks", status: "active", description: "web frameworks",
  description_locked: false, pinned: false, members: 4, instances: 2, voters: 5,
};
const report: ImpactReport = {
  action: "prune", source: "frameworks", target: null, summary: "Remove category 'frameworks'",
  affected_memories: 4, affected_instances: 2, queued_reenrichment: 5, warnings: [],
  reversible: false, applied: false,
};

describe("ClassCuration", () => {
  beforeEach(() => {
    fetchClass.mockReset().mockResolvedValue(cls);
    fetchClasses.mockReset().mockResolvedValue({
      classes: [cls, { ...cls, name: "tools", members: 9 }],
    });
    previewCuration.mockReset().mockResolvedValue(report);
    applyClassCuration.mockReset().mockResolvedValue({ ...report, applied: true });
  });

  it("shows status with the pinned badge when pinned", async () => {
    fetchClass.mockResolvedValue({ ...cls, pinned: true });
    render(<ClassCuration name="frameworks" onChanged={vi.fn()} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/pinned/i)).toBeInTheDocument());
  });

  it("shows a tenant the class without any curation control; the owner keeps them", async () => {
    const controls = [/merge/i, /rename/i, /save/i, /pin/i, /remove/i];
    const { unmount } = render(
      <ClassCuration name="frameworks" onChanged={vi.fn()} onClose={vi.fn()} />,
    );
    await screen.findByDisplayValue("web frameworks");
    for (const name of controls) expect(screen.getByRole("button", { name })).toBeInTheDocument();
    expect(fetchClasses).toHaveBeenCalledTimes(1);
    unmount();
    fetchClasses.mockClear();

    render(
      <ClassCuration
        name="frameworks"
        readOnly
        onChanged={vi.fn()}
        onClose={vi.fn()}
        footer={<div>found-in footer</div>}
      />,
    );
    expect(await screen.findByText("web frameworks")).toBeInTheDocument();
    expect(screen.getByText("found-in footer")).toBeInTheDocument();
    expect(screen.getByText("Members")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    // The class list only feeds the hidden merge picker: read-only never asks for it.
    expect(fetchClasses).not.toHaveBeenCalled();
  });

  it("previews a prune, then applies on confirm", async () => {
    const onChanged = vi.fn();
    const onClose = vi.fn();
    render(<ClassCuration name="frameworks" onChanged={onChanged} onClose={onClose} />);
    await screen.findByDisplayValue("web frameworks");
    await userEvent.click(screen.getByRole("button", { name: /remove/i }));
    await waitFor(() => expect(previewCuration).toHaveBeenCalledWith("prune", { name: "frameworks" }));
    await screen.findByText(/cannot be undone/i);
    await userEvent.click(screen.getByRole("button", { name: /confirm/i }));
    await waitFor(() => expect(applyClassCuration).toHaveBeenCalledWith("prune", { name: "frameworks" }));
    expect(onChanged).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("merges into a chosen target", async () => {
    previewCuration.mockResolvedValue({ ...report, action: "merge", target: "tools" });
    render(<ClassCuration name="frameworks" onChanged={vi.fn()} onClose={vi.fn()} />);
    await screen.findByDisplayValue("web frameworks");
    await userEvent.selectOptions(screen.getByLabelText(/merge target/i), "tools");
    await userEvent.click(screen.getByRole("button", { name: /^merge$/i }));
    await waitFor(() =>
      expect(previewCuration).toHaveBeenCalledWith("merge", { source: "frameworks", target: "tools" }));
  });

  it("saves the description through the curation api", async () => {
    render(<ClassCuration name="frameworks" onChanged={vi.fn()} onClose={vi.fn()} />);
    const box = await screen.findByDisplayValue("web frameworks");
    await userEvent.clear(box);
    await userEvent.type(box, "frontend frameworks");
    await userEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() =>
      expect(applyClassCuration).toHaveBeenCalledWith("set_description", {
        name: "frameworks", description: "frontend frameworks",
      }));
  });
});
