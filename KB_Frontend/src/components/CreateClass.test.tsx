import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ImpactReport } from "../types";

const previewCuration = vi.fn();
const applyClassCuration = vi.fn();
vi.mock("../api/client", () => ({
  previewCuration: (a: string, args: Record<string, string>) => previewCuration(a, args),
  applyClassCuration: (a: string, args: Record<string, string>) => applyClassCuration(a, args),
}));

import { CreateClass } from "./CreateClass";

const ok: ImpactReport = {
  action: "create", source: null, target: "hobbies", summary: "Create category 'hobbies'",
  affected_memories: 0, affected_instances: 0, queued_reenrichment: 0, warnings: [],
  reversible: true, applied: false,
};

describe("CreateClass", () => {
  beforeEach(() => {
    previewCuration.mockReset().mockResolvedValue(ok);
    applyClassCuration.mockReset().mockResolvedValue({ ...ok, applied: true });
  });

  it("previews then creates a category", async () => {
    const onCreated = vi.fn();
    render(<CreateClass onCreated={onCreated} />);
    await userEvent.type(screen.getByLabelText(/new category/i), "hobbies");
    await userEvent.click(screen.getByRole("button", { name: /create/i }));
    await waitFor(() => expect(previewCuration).toHaveBeenCalledWith("create", { name: "hobbies" }));
    await userEvent.click(screen.getByRole("button", { name: /confirm/i }));
    await waitFor(() => expect(applyClassCuration).toHaveBeenCalledWith("create", { name: "hobbies" }));
    expect(onCreated).toHaveBeenCalled();
  });

  it("surfaces a preview warning", async () => {
    previewCuration.mockResolvedValue({ ...ok, warnings: ["'hobbies' already exists (no-op)"] });
    render(<CreateClass onCreated={vi.fn()} />);
    await userEvent.type(screen.getByLabelText(/new category/i), "hobbies");
    await userEvent.click(screen.getByRole("button", { name: /create/i }));
    await screen.findByText(/already exists/i);
  });
});
