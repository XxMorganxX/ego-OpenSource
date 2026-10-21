import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const fetchSkills = vi.fn();
const fetchSkill = vi.fn();
const fetchAgents = vi.fn();
const fetchAgent = vi.fn();
const fetchSkillFile = vi.fn();
const fetchSkillUsage = vi.fn();
const fetchSkillLibraryUsage = vi.fn();
vi.mock("../api/client", () => ({
  fetchSkills: (...a: unknown[]) => fetchSkills(...a),
  fetchSkill: (...a: unknown[]) => fetchSkill(...a),
  fetchAgents: (...a: unknown[]) => fetchAgents(...a),
  fetchAgent: (...a: unknown[]) => fetchAgent(...a),
  fetchSkillFile: (...a: unknown[]) => fetchSkillFile(...a),
  fetchSkillUsage: (...a: unknown[]) => fetchSkillUsage(...a),
  fetchSkillLibraryUsage: (...a: unknown[]) => fetchSkillLibraryUsage(...a),
}));

let feedCb: ((tables: Set<string>) => void) | undefined;
vi.mock("../hooks/useChangeFeed", () => ({
  useChangeFeed: (cb: (tables: Set<string>) => void) => {
    feedCb = cb;
  },
}));

import { SkillsPage } from "./SkillsPage";

const summary = (name: string, archived = false) => ({
  name, description: "Example.", revision: 2, updated_at: "2026-10-17T10:00:00+00:00", archived,
  latest_revision: 2, approved_revision: 2, review_status: "approved", pending_revision: null,
});
const detail = (name: string, over = {}) => ({
  name, description: "Example.", revision: 2, current_revision: 2, archived: false,
  archived_at: null, archived_by: null, package_hash: "h", change_summary: "c", actor: null,
  agent_label: null, created_at: "2026-10-17T10:00:00+00:00",
  skill_md: `---\nname: ${name}\n---\n`, files: [{ path: "SKILL.md", bytes: 1, content_hash: "a" }],
  review_status: "approved", approved_revision: 2, latest_revision: 2, pending_revision: null,
  reviewer_label: null, review_notes: null, history: [], ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/?view=skills");
  fetchSkills.mockResolvedValue({ skills: [summary("deploy-checklist"), summary("review-infra")],
                                  next_cursor: null, query: null });
  fetchAgents.mockResolvedValue({ skills: [], next_cursor: null, query: null });
  fetchSkill.mockImplementation((name: string) => Promise.resolve(detail(name)));
  fetchSkillUsage.mockImplementation((name: string) => Promise.resolve({
    name, days: 30, tz: "UTC", since: "2026-09-19T00:00:00+00:00", loads: 0, file_reads: 0,
    surfaced: 0, last_loaded_at: null, surfaced_since: null, recent_loads: [] }));
  fetchSkillLibraryUsage.mockResolvedValue({
    days: 30, tz: "UTC", since: "2026-09-19T00:00:00+00:00", searches_per_day: [],
    missed_searches: [], missing_requests: [],
    per_skill: { "review-infra": { loads: 4, last_loaded_at: "2026-10-17T10:00:00+00:00" } },
    never_loaded: [], surfaced_since: null });
});

describe("SkillsPage", () => {
  it("prompts for a selection and feeds the library's load counts to the list", async () => {
    render(<SkillsPage />);
    expect(screen.getByText("Select a skill to read it.")).toBeInTheDocument();
    expect(await screen.findByTitle("4 loads in the last 30 days")).toHaveTextContent("4");
    expect(fetchSkill).not.toHaveBeenCalled();
  });

  it("opens a deep-linked skill at the linked revision, archived or not", async () => {
    window.history.replaceState(null, "", "/?view=skills&skill=old-skill&rev=1");
    fetchSkill.mockResolvedValue(detail("old-skill", { revision: 1, archived: true }));
    render(<SkillsPage />);
    expect(await screen.findByRole("heading", { name: "old-skill" })).toBeInTheDocument();
    expect(fetchSkill).toHaveBeenCalledWith("old-skill", 1);
    expect(screen.getAllByText("archived").length).toBeGreaterThan(0);
  });

  it("reads a malformed ?rev= as current", async () => {
    window.history.replaceState(null, "", "/?view=skills&skill=deploy-checklist&rev=abc");
    render(<SkillsPage />);
    await waitFor(() => expect(fetchSkill).toHaveBeenCalledWith("deploy-checklist", null));
  });

  it("selecting a skill sets ?skill= and drops ?rev=", async () => {
    window.history.replaceState(null, "", "/?view=skills&skill=deploy-checklist&rev=1");
    render(<SkillsPage />);
    await userEvent.click(await screen.findByText("review-infra"));
    const params = new URLSearchParams(window.location.search);
    expect(params.get("skill")).toBe("review-infra");
    expect(params.get("rev")).toBeNull();
    expect(await screen.findByRole("heading", { name: "review-infra" })).toBeInTheDocument();
  });

  it("refreshes skills on a skills change and usage on a trace change", async () => {
    window.history.replaceState(null, "", "/?view=skills&skill=deploy-checklist");
    render(<SkillsPage />);
    await screen.findByRole("heading", { name: "deploy-checklist" });
    await waitFor(() => expect(fetchSkillUsage).toHaveBeenCalledTimes(1));
    const [skills0, skill0, lib0] = [fetchSkills, fetchSkill, fetchSkillLibraryUsage]
      .map((f) => f.mock.calls.length);
    await act(async () => feedCb!(new Set(["mcp_trace"])));
    await waitFor(() => expect(fetchSkillUsage).toHaveBeenCalledTimes(2));
    expect(fetchSkillLibraryUsage.mock.calls.length).toBe(lib0 + 1);
    expect(fetchSkill.mock.calls.length).toBe(skill0);
    await act(async () => feedCb!(new Set(["skills"])));
    await waitFor(() => expect(fetchSkill.mock.calls.length).toBe(skill0 + 1));
    expect(fetchSkills.mock.calls.length).toBe(skills0 + 1);
  });

  it("shows library usage in place of a skill, and the overview row returns to it", async () => {
    window.history.replaceState(null, "", "/?view=skills&skill=deploy-checklist&rev=1");
    render(<SkillsPage />);
    expect(await screen.findByRole("heading", { name: "deploy-checklist" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Library usage")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Library overview/ }));
    const params = new URLSearchParams(window.location.search);
    expect(params.get("skill")).toBeNull();
    expect(params.get("rev")).toBeNull();
    expect(await screen.findByLabelText("Library usage")).toBeInTheDocument();
    expect(screen.getByText("Select a skill to read it.")).toBeInTheDocument();
  });

  it("switches to agents, hides the usage panels, and keeps the choice in the URL", async () => {
    render(<SkillsPage />);
    expect(await screen.findByLabelText("Library usage")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Agents" }));
    expect(window.location.search).toContain("kind=agent");
    expect(screen.queryByLabelText("Library usage")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Agents", selected: true })).toBeInTheDocument();
    expect(await screen.findByText("Select an agent to read it.")).toBeInTheDocument();
  });

  it("lists the agents that need review on the agent library page, each one click away", async () => {
    window.history.replaceState(null, "", "/?view=skills&kind=agent");
    fetchAgents.mockResolvedValue({ skills: [
      summary("code-helper"),
      { ...summary("docs-helper"), latest_revision: 3, review_status: "pending", pending_revision: 3 },
    ], next_cursor: null, query: null });
    fetchAgent.mockImplementation((name: string) => Promise.resolve(detail(name)));
    render(<SkillsPage />);
    expect(await screen.findByText("2 agents · 1 needs review")).toBeInTheDocument();
    const section = screen.getByRole("heading", { name: "Needs review" }).parentElement!;
    await userEvent.click(within(section).getByRole("button", { name: "docs-helper" }));
    expect(new URLSearchParams(window.location.search).get("skill")).toBe("docs-helper");
  });
});
