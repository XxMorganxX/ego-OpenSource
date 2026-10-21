import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ReviewStatus, SkillPage } from "../types";

const fetchSkills = vi.fn();
const fetchAgents = vi.fn();
vi.mock("../api/client", () => ({
  fetchSkills: (...a: unknown[]) => fetchSkills(...a),
  fetchAgents: (...a: unknown[]) => fetchAgents(...a),
}));

import { SkillList } from "./SkillList";

const row = (name: string, over = {}) => ({
  name, description: `${name} does a thing.\nSecond line.`, revision: 2,
  updated_at: "2026-10-17T10:00:00+00:00", archived: false,
  latest_revision: 2, approved_revision: 2, review_status: "approved" as ReviewStatus, pending_revision: null, ...over,
});
const page = (names: string[], next: string | null = null): SkillPage => ({
  skills: names.map((n) => row(n)), next_cursor: next, query: null,
});
const base = { selected: null, onSelect: vi.fn(), perSkill: {}, days: 30 as const, refreshTick: 0 };

beforeEach(() => {
  vi.clearAllMocks();
  fetchSkills.mockResolvedValue(page(["deploy-checklist", "review-infra"]));
});

describe("SkillList", () => {
  it("lists skills with first description line, revision and loads", async () => {
    render(<SkillList {...base}
      perSkill={{ "deploy-checklist": { loads: 3, last_loaded_at: "2026-10-17T10:00:00+00:00" } }} />);
    expect(await screen.findByText("deploy-checklist")).toBeInTheDocument();
    expect(screen.getByText("deploy-checklist does a thing.")).toBeInTheDocument();
    expect(screen.getAllByText("rev 2")).toHaveLength(2);
    // The row's time is the item's last activity, worded apart from a revision's date.
    expect(screen.getAllByText(/^changed .+ ago$/)).toHaveLength(2);
    expect(screen.getByTitle("3 loads in the last 30 days")).toHaveTextContent("3");
    expect(screen.getByTitle("0 loads in the last 30 days")).toHaveTextContent("0");
    expect(screen.getByText("loads · 30d")).toBeInTheDocument();
  });

  it("debounces the search box into one query", async () => {
    render(<SkillList {...base} />);
    await screen.findByText("deploy-checklist");
    await userEvent.type(screen.getByLabelText("Search skills"), "deploy");
    await waitFor(() =>
      expect(fetchSkills).toHaveBeenLastCalledWith({ q: "deploy", archived: false }));
    const queries = fetchSkills.mock.calls.map((c) => (c[0] as { q?: string }).q);
    expect(queries).toEqual([undefined, "deploy"]);
  });

  it("asks for archived skills when toggled and badges them", async () => {
    render(<SkillList {...base} />);
    await screen.findByText("deploy-checklist");
    fetchSkills.mockResolvedValue({ ...page([]), skills: [row("old-skill", { archived: true })] });
    await userEvent.click(screen.getByLabelText("show archived"));
    await waitFor(() =>
      expect(fetchSkills).toHaveBeenLastCalledWith({ q: undefined, archived: true }));
    expect(await screen.findByText("archived")).toBeInTheDocument();
  });

  it("ignores a stale search response", async () => {
    let resolveBrowse!: (p: SkillPage) => void;
    fetchSkills.mockImplementationOnce(() => new Promise((r) => { resolveBrowse = r; }));
    fetchSkills.mockResolvedValueOnce(page(["deploy-checklist"]));
    render(<SkillList {...base} />);
    await userEvent.type(screen.getByLabelText("Search skills"), "deploy");
    expect(await screen.findByText("deploy-checklist")).toBeInTheDocument();
    await act(async () => { resolveBrowse(page(["alpha-skill", "beta-skill"])); });
    expect(screen.queryByText("alpha-skill")).toBeNull();
  });

  it("appends the next page of a search", async () => {
    render(<SkillList {...base} />);
    await screen.findByText("deploy-checklist");
    fetchSkills.mockResolvedValueOnce(page(["alpha-skill"], "NTA="));
    fetchSkills.mockResolvedValueOnce(page(["beta-skill"]));
    await userEvent.type(screen.getByLabelText("Search skills"), "skill");
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByText("beta-skill")).toBeInTheDocument();
    expect(screen.getByText("alpha-skill")).toBeInTheDocument();
    expect(fetchSkills).toHaveBeenLastCalledWith({ q: "skill", archived: false, cursor: "NTA=" });
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("reads every page when browsing, so a later page's pending skill is grouped first", async () => {
    fetchSkills.mockResolvedValueOnce(page(["alpha-skill"], "NTA="));
    fetchSkills.mockResolvedValueOnce({ skills: [
      row("zulu-skill", { latest_revision: 3, approved_revision: 2, review_status: "pending", pending_revision: 3 }),
    ], next_cursor: null, query: null });
    const onBrowsed = vi.fn();
    render(<SkillList {...base} onBrowsed={onBrowsed} />);
    const attention = await screen.findByRole("region", { name: "Needs attention" });
    expect(within(attention).getByText("zulu-skill")).toBeInTheDocument();
    expect(fetchSkills).toHaveBeenLastCalledWith({ q: undefined, archived: false, cursor: "NTA=" });
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    expect(onBrowsed).toHaveBeenCalledWith(2, ["zulu-skill"]);   // two rows, one needs review
  });

  it("explains an empty library and an empty search", async () => {
    fetchSkills.mockResolvedValue(page([]));
    render(<SkillList {...base} />);
    // Archived skills may exist, so an empty browse says "active".
    expect(await screen.findByText("No active skills.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Search skills"), "zzz");
    expect(await screen.findByText("No skills match “zzz”.")).toBeInTheDocument();
  });

  it("reports browse counts, not search counts", async () => {
    const onBrowsed = vi.fn();
    fetchSkills.mockResolvedValueOnce(page([]));
    render(<SkillList {...base} onBrowsed={onBrowsed} />);
    await waitFor(() => expect(onBrowsed).toHaveBeenCalledWith(0, []));
    fetchSkills.mockResolvedValue(page(["deploy-checklist"]));
    await userEvent.type(screen.getByLabelText("Search skills"), "deploy");
    expect(await screen.findByText("deploy-checklist")).toBeInTheDocument();
    expect(onBrowsed).toHaveBeenCalledTimes(1);
  });

  it("reports the active library's size whether or not archived items are shown", async () => {
    const onBrowsed = vi.fn();
    fetchSkills.mockResolvedValueOnce(page(["alpha-skill"]));
    render(<SkillList {...base} onBrowsed={onBrowsed} />);
    await waitFor(() => expect(onBrowsed).toHaveBeenCalledWith(1, []));
    fetchSkills.mockResolvedValueOnce({ skills: [
      row("alpha-skill"),
      row("old-skill", { archived: true, latest_revision: 2, approved_revision: 1, review_status: "pending", pending_revision: 2 }),
    ], next_cursor: null, query: null });
    await userEvent.click(screen.getByLabelText(/archived/i));
    expect(await screen.findByText("old-skill")).toBeInTheDocument();
    expect(onBrowsed).toHaveBeenLastCalledWith(1, []);
  });

  it("selects a skill on click", async () => {
    const onSelect = vi.fn();
    render(<SkillList {...base} onSelect={onSelect} />);
    await userEvent.click(await screen.findByText("review-infra"));
    expect(onSelect).toHaveBeenCalledWith("review-infra");
  });

  it("badges a row whose newest revision waits for review or was rejected", async () => {
    fetchSkills.mockResolvedValue({ skills: [
      row("deploy-checklist", { latest_revision: 3, approved_revision: 2, review_status: "pending", pending_revision: 3 }),
      row("review-infra", { latest_revision: 2, approved_revision: 1, review_status: "rejected", pending_revision: null }),
      row("release-notes", { latest_revision: 2, approved_revision: 2, review_status: "approved", pending_revision: null }),
    ], next_cursor: null, query: null });
    render(<SkillList {...base} />);
    expect(await screen.findByText("pending review")).toBeInTheDocument();
    expect(screen.getByText("rejected")).toBeInTheDocument();
    expect(screen.getAllByText(/pending review|rejected/)).toHaveLength(2);
    // The row says which revision waits and which one agents get, so it never seems to
    // contradict the detail header (which opens on the newest revision).
    expect(screen.getByText("rev 3 · serving rev 2")).toBeInTheDocument();
    expect(screen.getByText("rev 2 · serving rev 1")).toBeInTheDocument();
  });

  it("groups rows that need the owner first, and keeps the server's order while searching", async () => {
    fetchSkills.mockResolvedValue({ skills: [
      row("alpha-skill"),
      row("beta-skill", { latest_revision: 3, approved_revision: 2, review_status: "pending", pending_revision: 3 }),
      row("gamma-skill", { latest_revision: 2, approved_revision: 1, review_status: "rejected" }),
      row("delta-skill", { latest_revision: 1, approved_revision: null, revision: 1, review_status: "pending", pending_revision: 1 }),
    ], next_cursor: null, query: null });
    render(<SkillList {...base} />);
    const attention = await screen.findByRole("region", { name: "Needs attention" });
    expect(within(attention).getAllByRole("button").map((b) => b.querySelector(".skill-row-name")?.textContent))
      .toEqual(["beta-skill", "gamma-skill", "delta-skill"]);
    expect(within(attention).getByText("rev 1 · none served")).toBeInTheDocument();
    const all = screen.getByRole("region", { name: "All skills" });
    expect(within(all).getByText("alpha-skill")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Search skills"), "skill");
    const results = await screen.findByRole("region", { name: "Results" });
    expect(within(results).getAllByRole("button").map((b) => b.querySelector(".skill-row-name")?.textContent))
      .toEqual(["alpha-skill", "beta-skill", "gamma-skill", "delta-skill"]);
    expect(screen.queryByRole("region", { name: "Needs attention" })).toBeNull();
  });

  it("keeps an archived skill's waiting revision in view", async () => {
    render(<SkillList {...base} />);
    await screen.findByText("deploy-checklist");
    fetchSkills.mockResolvedValue({ skills: [
      row("old-skill", { archived: true, latest_revision: 3, approved_revision: 2, review_status: "pending", pending_revision: 3 }),
      row("retired-skill", { archived: true }),
    ], next_cursor: null, query: null });
    await userEvent.click(screen.getByLabelText("show archived"));
    // Approving it brings it back, so it needs the owner like any other.
    const attention = await screen.findByRole("region", { name: "Needs attention" });
    expect(within(attention).getByText("old-skill")).toBeInTheDocument();
    expect(within(attention).getByText("archived")).toBeInTheDocument();
    expect(within(attention).getByText("pending review")).toBeInTheDocument();
    expect(within(attention).getByText("rev 3 · serving rev 2")).toBeInTheDocument();
    const archivedGroup = screen.getByRole("region", { name: "Archived" });
    expect(within(archivedGroup).getByText("retired-skill")).toBeInTheDocument();
    expect(within(archivedGroup).queryByText("old-skill")).toBeNull();
  });

  it("badges the newest revision's state when it was rejected and an older one still waits", async () => {
    fetchSkills.mockResolvedValue({ skills: [
      row("deploy-checklist", { latest_revision: 5, approved_revision: 3, review_status: "rejected", pending_revision: 4 }),
    ], next_cursor: null, query: null });
    render(<SkillList {...base} />);
    expect(await screen.findByText("rejected")).toBeInTheDocument();
    expect(screen.queryByText("pending review")).toBeNull();
    expect(screen.getByText("rev 5 · serving rev 3")).toBeInTheDocument();
  });

  it("groups rows by the query that produced them, not the one still being typed", async () => {
    fetchSkills.mockResolvedValue({ skills: [
      row("alpha-skill"),
      row("beta-skill", { latest_revision: 3, approved_revision: 2, review_status: "pending", pending_revision: 3 }),
    ], next_cursor: null, query: null });
    render(<SkillList {...base} />);
    await screen.findByRole("region", { name: "Needs attention" });
    let resolveSearch!: (p: SkillPage) => void;
    fetchSkills.mockImplementationOnce(() => new Promise((r) => { resolveSearch = r; }));
    await userEvent.type(screen.getByLabelText("Search skills"), "beta");
    await waitFor(() => expect(fetchSkills).toHaveBeenLastCalledWith({ q: "beta", archived: false }));
    // The browse rows are still the whole library, so they keep their browse groups.
    expect(screen.getByRole("region", { name: "Needs attention" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Results" })).toBeNull();
    await act(async () => { resolveSearch(page(["beta-skill"])); });
    expect(await screen.findByRole("region", { name: "Results" })).toBeInTheDocument();
  });

  it("drops an older search's Load more page once a newer search is under way", async () => {
    render(<SkillList {...base} />);
    await screen.findByText("deploy-checklist");
    fetchSkills.mockResolvedValueOnce(page(["alpha-one"], "NTA="));
    await userEvent.type(screen.getByLabelText("Search skills"), "a");
    await screen.findByText("alpha-one");
    let resolveNew!: (p: SkillPage) => void;
    let resolveMore!: (p: SkillPage) => void;
    fetchSkills.mockImplementationOnce(() => new Promise((r) => { resolveNew = r; }));
    await userEvent.type(screen.getByLabelText("Search skills"), "b");
    await waitFor(() => expect(fetchSkills).toHaveBeenLastCalledWith({ q: "ab", archived: false }));
    fetchSkills.mockImplementationOnce(() => new Promise((r) => { resolveMore = r; }));
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(fetchSkills).toHaveBeenLastCalledWith({ q: "a", archived: false, cursor: "NTA=" });
    await act(async () => { resolveNew(page(["ab-skill"])); });
    await screen.findByText("ab-skill");
    await act(async () => { resolveMore(page(["a-page-two"], "MTAw")); });
    const names = Array.from(document.querySelectorAll(".skill-row-name")).map((e) => e.textContent);
    expect(names).toEqual(["ab-skill"]);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("drops a Load more page that lands before the newer search does", async () => {
    render(<SkillList {...base} />);
    await screen.findByText("deploy-checklist");
    fetchSkills.mockResolvedValueOnce(page(["alpha-one"], "NTA="));
    await userEvent.type(screen.getByLabelText("Search skills"), "a");
    await screen.findByText("alpha-one");
    let resolveNew!: (p: SkillPage) => void;
    fetchSkills.mockImplementationOnce(() => new Promise((r) => { resolveNew = r; }));
    await userEvent.type(screen.getByLabelText("Search skills"), "b");
    await waitFor(() => expect(fetchSkills).toHaveBeenLastCalledWith({ q: "ab", archived: false }));
    fetchSkills.mockResolvedValueOnce(page(["a-page-two"]));
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await act(async () => {});
    expect(screen.queryByText("a-page-two")).toBeNull();
    await act(async () => { resolveNew(page(["ab-skill"])); });
    const names = Array.from(document.querySelectorAll(".skill-row-name")).map((e) => e.textContent);
    expect(names).toEqual(["ab-skill"]);
  });

  it("does not show one library's rows under the other while it loads", async () => {
    const onSelect = vi.fn();
    const { rerender } = render(<SkillList {...base} onSelect={onSelect} />);
    await screen.findByText("deploy-checklist");
    let resolveAgents!: (p: SkillPage) => void;
    fetchAgents.mockImplementationOnce(() => new Promise((r) => { resolveAgents = r; }));
    rerender(<SkillList {...base} onSelect={onSelect} kind="agent" />);
    expect(screen.queryByText("deploy-checklist")).toBeNull();
    expect(screen.queryByRole("region", { name: "All agents" })).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    await act(async () => { resolveAgents(page(["code-auditor"])); });
    const all = await screen.findByRole("region", { name: "All agents" });
    expect(within(all).getByText("code-auditor")).toBeInTheDocument();
  });

  it("lists agents from the agents endpoint with agent wording", async () => {
    fetchAgents.mockResolvedValue({ skills: [], next_cursor: null, query: null });
    render(<SkillList {...base} kind="agent" />);
    expect(await screen.findByText("No agents yet. Agents are saved with upsert_agent.")).toBeInTheDocument();
    expect(fetchSkills).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Search agents")).toBeInTheDocument();
  });
});
