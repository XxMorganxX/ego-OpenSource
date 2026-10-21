import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fetchExcludedMemories, reviveMemory } from "../api/client";
import { UNKNOWN_ME } from "../lib/identity";
import { DEFAULT_GRAPH_SETTINGS } from "../lib/settings";
import type { ExcludedMemory } from "../types";
import { SettingsModal } from "./SettingsModal";

vi.mock("../api/client", () => ({
  fetchExcludedMemories: vi.fn(),
  reviveMemory: vi.fn(),
}));

const excluded = (over: Partial<ExcludedMemory>): ExcludedMemory => ({
  id: "x",
  path: "notes.x",
  parent_id: null,
  type: "note",
  slug: "x",
  title: "X",
  tags: [],
  importance: 0,
  summary: null,
  lifecycle: "noise",
  reason: null,
  dead_at: null,
  ...over,
});

describe("SettingsModal", () => {
  it("names the signed-in identity and tenant; nothing on a pre-tenancy backend", () => {
    const { unmount } = render(
      <SettingsModal
        me={{ email: "b@example.com", tenant: "bob", is_owner: false }}
        settings={DEFAULT_GRAPH_SETTINGS}
        onChange={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("· b@example.com · bob")).toBeInTheDocument();
    unmount();
    render(
      <SettingsModal me={null} settings={DEFAULT_GRAPH_SETTINGS} onChange={vi.fn()} onClose={vi.fn()} />,
    );
    expect(screen.queryByText(/b@x\.com/)).not.toBeInTheDocument();
  });

  it("says the identity is unknown when /api/me could not be read", () => {
    render(
      <SettingsModal
        me={UNKNOWN_ME}
        settings={DEFAULT_GRAPH_SETTINGS}
        onChange={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("· identity unknown")).toBeInTheDocument();
  });

  it("emits a new spacing value when the slider moves", () => {
    const onChange = vi.fn();
    render(
      <SettingsModal settings={DEFAULT_GRAPH_SETTINGS} onChange={onChange} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText("Graph dispersal"), { target: { value: "80" } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ spacing: 80 }));
  });

  it("offers signed cluster gravity and emits a negative value", () => {
    const onChange = vi.fn();
    render(
      <SettingsModal settings={DEFAULT_GRAPH_SETTINGS} onChange={onChange} onClose={vi.fn()} />,
    );
    const slider = screen.getByLabelText("Cluster gravity");
    expect(slider).toHaveAttribute("min", "-5");
    expect(slider).toHaveAttribute("max", "150");
    expect(slider).toHaveAttribute("step", "1");
    fireEvent.change(slider, { target: { value: "-3" } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ gravity: -3 }));
    expect(screen.getByText("Repel")).toBeInTheDocument();
    expect(screen.getByText("Attract")).toBeInTheDocument();
  });

  it("no longer hosts the color-by-importance toggle (it moved to the graph view filter)", () => {
    render(
      <SettingsModal settings={DEFAULT_GRAPH_SETTINGS} onChange={vi.fn()} onClose={vi.fn()} />,
    );
    expect(screen.queryByLabelText("Color by importance")).toBeNull();
  });

  it("offers a log out link pointing at the Cloudflare Access logout", () => {
    render(
      <SettingsModal settings={DEFAULT_GRAPH_SETTINGS} onChange={vi.fn()} onClose={vi.fn()} />,
    );
    const logout = screen.getByRole("link", { name: /log out/i });
    expect(logout).toHaveAttribute("href", "/cdn-cgi/access/logout");
  });

  it("closes on Escape and on backdrop click", async () => {
    const onClose = vi.fn();
    render(
      <SettingsModal settings={DEFAULT_GRAPH_SETTINGS} onChange={vi.fn()} onClose={onClose} />,
    );
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("SettingsModal — Memories tab", () => {
  beforeEach(() => {
    vi.mocked(fetchExcludedMemories).mockReset().mockResolvedValue({ memories: [] });
    vi.mocked(reviveMemory).mockReset().mockResolvedValue({} as never);
  });

  const openMemories = async () => {
    render(
      <SettingsModal settings={DEFAULT_GRAPH_SETTINGS} onChange={vi.fn()} onClose={vi.fn()} />,
    );
    await userEvent.click(screen.getByRole("tab", { name: "Memories" }));
  };

  it("lists shut-off memories labeled by lifecycle", async () => {
    vi.mocked(fetchExcludedMemories).mockResolvedValue({
      memories: [
        excluded({ id: "d1", title: "Old event", lifecycle: "dead", reason: "event has passed",
                   dead_at: "2024-01-01T00:00:00Z" }),
        excluded({ id: "n1", title: "Junk", lifecycle: "noise", reason: "random characters" }),
      ],
    });
    await openMemories();
    expect(await screen.findByText("Old event")).toBeInTheDocument();
    expect(screen.getByText("Junk")).toBeInTheDocument();
    expect(document.querySelector(".badge.dead")).toBeInTheDocument();
    expect(document.querySelector(".badge.noise")).toBeInTheDocument();
    expect(screen.getByText("event has passed")).toBeInTheDocument();
  });

  it("revives a dead memory and drops it from the list", async () => {
    vi.mocked(fetchExcludedMemories).mockResolvedValue({
      memories: [excluded({ id: "d1", title: "Old event", lifecycle: "dead" })],
    });
    await openMemories();
    await screen.findByText("Old event");
    await userEvent.click(screen.getByRole("button", { name: "Revive" }));
    expect(reviveMemory).toHaveBeenCalledWith("d1");
    await waitFor(() => expect(screen.queryByText("Old event")).not.toBeInTheDocument());
  });

  it("refetches with the chosen kind when the filter changes", async () => {
    await openMemories();
    await userEvent.click(screen.getByRole("tab", { name: "Dead" }));
    expect(fetchExcludedMemories).toHaveBeenLastCalledWith("dead");
    await userEvent.click(screen.getByRole("tab", { name: "All" }));
    expect(fetchExcludedMemories).toHaveBeenLastCalledWith(undefined);
  });
});
