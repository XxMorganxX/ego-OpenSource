import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SkillDetailData } from "../types";

const fetchSkill = vi.fn();
const fetchSkillFile = vi.fn();
const fetchAgent = vi.fn();
vi.mock("../api/client", () => ({
  fetchAgent: (...a: unknown[]) => fetchAgent(...a),
  fetchSkill: (...a: unknown[]) => fetchSkill(...a),
  fetchSkillFile: (...a: unknown[]) => fetchSkillFile(...a),
}));

import { SkillDetail } from "./SkillDetail";

const rv = { review_status: "approved" as const, reviewed_at: null, reviewer_label: null, review_notes: null };
const detail = (over: Partial<SkillDetailData> = {}): SkillDetailData => ({
  name: "deploy-checklist", description: "Steps before a deploy.", revision: 2,
  current_revision: 2, archived: false, archived_at: null, archived_by: null,
  package_hash: "h2", change_summary: "add notes", actor: "m@example.com",
  agent_label: "claude-code", created_at: "2026-10-17T10:00:00+00:00",
  review_status: "approved", approved_revision: 2, latest_revision: 2, pending_revision: null,
  reviewer_label: null, review_notes: null,
  skill_md: "---\nname: deploy-checklist\n---\n\nStep one.\n",
  files: [{ path: "SKILL.md", bytes: 40, content_hash: "a" },
          { path: "references/notes.md", bytes: 6, content_hash: "b" }],
  history: [
    { revision: 2, actor: "m@example.com", agent_label: "claude-code",
      change_summary: "add notes", package_hash: "h2", created_at: "2026-10-17T10:00:00+00:00", ...rv },
    { revision: 1, actor: null, agent_label: null, change_summary: "create",
      package_hash: "h1", created_at: "2026-10-16T10:00:00+00:00", ...rv },
  ],
  ...over,
});

const props = { refreshTick: 0, onSelectRevision: vi.fn() };

beforeEach(() => {
  vi.clearAllMocks();
  fetchSkill.mockResolvedValue(detail());
  fetchSkillFile.mockResolvedValue({ name: "deploy-checklist", revision: 2,
    path: "references/notes.md", content: "notes body\n", content_hash: "b" });
});

describe("SkillDetail", () => {
  it("shows SKILL.md raw with header and history", async () => {
    render(<SkillDetail name="deploy-checklist" revision={null} {...props} />);
    expect(await screen.findByRole("heading", { name: "deploy-checklist" })).toBeInTheDocument();
    expect(screen.getByLabelText("SKILL.md content").textContent).toContain("Step one.");
    expect(screen.getByLabelText("SKILL.md content").textContent).toContain("name: deploy-checklist");
    expect(screen.getByText("rev 2 of 2")).toBeInTheDocument();
    expect(screen.getByText("Steps before a deploy.")).toBeInTheDocument();
    expect(screen.getByText("no verified author")).toBeInTheDocument();
    expect(screen.getByText(/claude-code/)).toBeInTheDocument();
    expect(fetchSkill).toHaveBeenCalledWith("deploy-checklist", null);
  });

  it("loads a supporting file at the viewed revision, not current", async () => {
    fetchSkill.mockResolvedValue(detail({ revision: 1, current_revision: 2 }));
    render(<SkillDetail name="deploy-checklist" revision={1} {...props} />);
    await userEvent.click(await screen.findByRole("tab", { name: /references\/notes\.md/ }));
    await waitFor(() =>
      expect(fetchSkillFile).toHaveBeenCalledWith("deploy-checklist", "references/notes.md", 1));
    expect(await screen.findByText("notes body")).toBeInTheDocument();
  });

  it("selects revisions from history and offers a way back to current", async () => {
    const onSelectRevision = vi.fn();
    fetchSkill.mockResolvedValue(detail({ revision: 1, current_revision: 2 }));
    render(<SkillDetail name="deploy-checklist" revision={1} refreshTick={0}
                        onSelectRevision={onSelectRevision} />);
    expect(await screen.findByText(/Viewing revision 1 — newest is 2/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show newest" }));
    expect(onSelectRevision).toHaveBeenLastCalledWith(null);
    await userEvent.click(screen.getByRole("button", { name: "rev 1" }));
    expect(onSelectRevision).toHaveBeenLastCalledWith(1);
    await userEvent.click(screen.getByRole("button", { name: "rev 2" }));
    expect(onSelectRevision).toHaveBeenLastCalledWith(null);   // current = no ?rev=
  });

  it("names a missing skill and a missing revision", async () => {
    fetchSkill.mockRejectedValueOnce(new Error('404: {"detail":"not_found: x"}'));
    const { rerender } = render(<SkillDetail name="release-notes" revision={null} {...props} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Skill not found.");
    fetchSkill.mockRejectedValueOnce(new Error('404: {"detail":"revision_not_found: x"}'));
    rerender(<SkillDetail name="deploy-checklist" revision={9} {...props} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("No revision 9 of deploy-checklist.");
  });

  it("ignores a response for a skill the viewer already left", async () => {
    let resolveAlpha!: (d: SkillDetailData) => void;
    fetchSkill.mockImplementationOnce(() => new Promise((r) => { resolveAlpha = r; }));
    fetchSkill.mockResolvedValueOnce(detail({ name: "beta-skill" }));
    const { rerender } = render(<SkillDetail name="alpha-skill" revision={null} {...props} />);
    rerender(<SkillDetail name="beta-skill" revision={null} {...props} />);
    expect(await screen.findByRole("heading", { name: "beta-skill" })).toBeInTheDocument();
    await act(async () => { resolveAlpha(detail({ name: "alpha-skill" })); });
    expect(screen.getByRole("heading", { name: "beta-skill" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "alpha-skill" })).toBeNull();
  });

  it("keeps a pinned revision across a live refresh", async () => {
    fetchSkill.mockResolvedValueOnce(detail({ revision: 1, current_revision: 2 }));
    const { rerender } = render(<SkillDetail name="deploy-checklist" revision={1} {...props} />);
    expect(await screen.findByText(/newest is 2/)).toBeInTheDocument();
    fetchSkill.mockResolvedValueOnce(detail({ revision: 1, current_revision: 3 }));
    rerender(<SkillDetail name="deploy-checklist" revision={1} refreshTick={1}
                          onSelectRevision={props.onSelectRevision} />);
    expect(await screen.findByText(/newest is 3/)).toBeInTheDocument();
    expect(fetchSkill).toHaveBeenLastCalledWith("deploy-checklist", 1);
    expect(screen.getByText("rev 1 of 3")).toBeInTheDocument();
  });

  it("marks an archived skill", async () => {
    fetchSkill.mockResolvedValue(detail({ archived: true, archived_at: "2026-10-17T12:00:00+00:00" }));
    render(<SkillDetail name="deploy-checklist" revision={null} {...props} />);
    expect(await screen.findByText("archived")).toBeInTheDocument();
  });

  it("shows an agent's AGENT.md and the review state of each revision", async () => {
    fetchAgent.mockResolvedValue({ ...detail({ name: "code-auditor" }), kind: "agent", skill_md: undefined,
      revision: 1, current_revision: 2,
      agent_md: "---\nname: code-auditor\n---\nYou audit.\n",
      files: [{ path: "AGENT.md", bytes: 30, content_hash: "h" }],
      review_status: "approved", approved_revision: 1, latest_revision: 2, pending_revision: 2,
      history: [
        { revision: 2, actor: null, agent_label: "claude-code", change_summary: "edit", package_hash: "b",
          created_at: "2026-10-19T10:00:00+00:00", review_status: "pending", reviewer_label: null, review_notes: null, reviewed_at: null },
        { revision: 1, actor: null, agent_label: "claude-code", change_summary: "create", package_hash: "a",
          created_at: "2026-10-18T10:00:00+00:00", review_status: "approved", reviewer_label: "claude-code",
          review_notes: "read in full", reviewed_at: "2026-10-18T10:05:00+00:00" },
      ] });
    render(<SkillDetail name="code-auditor" revision={null} refreshTick={0} onSelectRevision={vi.fn()} kind="agent" />);
    expect(await screen.findByText(/You audit\./)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /AGENT\.md/ })).toBeInTheDocument();
    expect(screen.getByText("pending review")).toBeInTheDocument();
    expect(screen.getByText("approved by claude-code · read in full")).toBeInTheDocument();
    expect(screen.getByText(/Revision 2 is waiting for review/)).toBeInTheDocument();
  });

  it("badges the viewed revision's own review status next to its number", async () => {
    fetchSkill.mockResolvedValue(detail({ revision: 3, current_revision: 3, review_status: "pending",
      approved_revision: 2, latest_revision: 3, pending_revision: 3 }));
    render(<SkillDetail name="deploy-checklist" revision={null} {...props} />);
    const rev = await screen.findByText("rev 3 of 3");
    const title = rev.parentElement as HTMLElement;
    expect(within(title).getByText("pending review")).toHaveClass("skill-badge-pending");
  });

  it("badges an approved revision as approved", async () => {
    render(<SkillDetail name="deploy-checklist" revision={null} {...props} />);
    const title = (await screen.findByText("rev 2 of 2")).parentElement as HTMLElement;
    expect(within(title).getByText("approved")).toHaveClass("skill-badge-approved");
  });

  it("says where the file is read when the newest revision was rejected", async () => {
    fetchSkill.mockResolvedValue(detail({ revision: 1, current_revision: 2, approved_revision: 1,
      latest_revision: 2, history: [
        { revision: 2, actor: null, agent_label: null, change_summary: "edit", package_hash: "h2",
          created_at: "2026-10-19T10:00:00+00:00", review_status: "rejected", reviewer_label: null,
          review_notes: null, reviewed_at: "2026-10-19T10:05:00+00:00" },
        { revision: 1, actor: null, agent_label: null, change_summary: "create", package_hash: "h1",
          created_at: "2026-10-16T10:00:00+00:00", ...rv },
      ] }));
    render(<SkillDetail name="deploy-checklist" revision={null} {...props} />);
    expect(await screen.findByText(/Revision 2 was rejected on review; agents and devices\s+keep revision 1\./))
      .toBeInTheDocument();
  });

  it("shows a rejected revision's notes once, in its notice", async () => {
    const rejected = { review_status: "rejected" as const, reviewer_label: "owner",
      review_notes: "takes a lock", reviewed_at: "2026-10-17T11:00:00+00:00" };
    fetchSkill.mockResolvedValue(detail({ revision: 2, current_revision: 2, approved_revision: 1,
      latest_revision: 2, ...rejected, history: [
        { revision: 2, actor: null, agent_label: null, change_summary: "edit", package_hash: "h2",
          created_at: "2026-10-17T10:00:00+00:00", ...rejected },
        { revision: 1, actor: null, agent_label: null, change_summary: "create", package_hash: "h1",
          created_at: "2026-10-16T10:00:00+00:00", ...rv },
      ] }));
    render(<SkillDetail name="deploy-checklist" revision={null} {...props} />);
    expect(await screen.findByText("Rejected by owner: “takes a lock”")).toBeInTheDocument();
    expect(screen.getAllByText(/takes a lock/)).toHaveLength(1);
    const review = screen.getByRole("region", { name: "Review" });
    expect(within(review).getByText(/^owner · /)).toBeInTheDocument();
    // The history entry keeps its state; its notes move to the hover title.
    const entry = within(screen.getByRole("region", { name: "History" })).getByText("rejected by owner");
    expect(entry).toHaveAttribute("title", expect.stringContaining("takes a lock"));
  });

  it("quotes a rejected newest revision's notes once while an older one is read", async () => {
    fetchSkill.mockResolvedValue(detail({ revision: 1, current_revision: 2, approved_revision: 1,
      latest_revision: 2, history: [
        { revision: 2, actor: null, agent_label: null, change_summary: "edit", package_hash: "h2",
          created_at: "2026-10-19T10:00:00+00:00", review_status: "rejected", reviewer_label: "owner",
          review_notes: "takes a lock", reviewed_at: "2026-10-19T10:05:00+00:00" },
        { revision: 1, actor: null, agent_label: null, change_summary: "create", package_hash: "h1",
          created_at: "2026-10-16T10:00:00+00:00", ...rv },
      ] }));
    render(<SkillDetail name="deploy-checklist" revision={1} {...props} />);
    expect(await screen.findByText("Rejected by owner: “takes a lock”")).toBeInTheDocument();
    expect(screen.getAllByText(/takes a lock/)).toHaveLength(1);
  });

  it("gives file tabs distinct ids even when their paths differ only in punctuation", async () => {
    fetchSkill.mockResolvedValue(detail({ files: [
      { path: "SKILL.md", bytes: 40, content_hash: "a" },
      { path: "references/a.md", bytes: 6, content_hash: "b" },
      { path: "references/a_md", bytes: 6, content_hash: "c" },
    ] }));
    render(<SkillDetail name="deploy-checklist" revision={null} {...props} />);
    const tabs = await screen.findAllByRole("tab");
    const ids = tabs.map((t) => t.id);
    expect(new Set(ids).size).toBe(3);
    const panel = screen.getByRole("tabpanel");
    expect(panel.getAttribute("aria-labelledby")).toBe(tabs[0].id);
    tabs[0].focus();
    await userEvent.keyboard("{ArrowRight}");
    await userEvent.keyboard("{ArrowRight}");
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("tab", { name: /a_md/ })));
    expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby"))
      .toBe(screen.getByRole("tab", { name: /a_md/ }).id);
  });

  it("dates the header by the revision on screen", async () => {
    fetchSkill.mockResolvedValue(detail({ revision: 1, current_revision: 2, latest_revision: 2,
      created_at: "2026-10-16T10:00:00+00:00" }));
    render(<SkillDetail name="deploy-checklist" revision={1} {...props} />);
    expect(await screen.findByTitle(/^revision 1 written /)).toHaveTextContent(/^written /);
  });

  it("keeps the rail's aside mounted while another revision loads", async () => {
    let resolveNext!: (d: SkillDetailData) => void;
    const { rerender } = render(<SkillDetail name="deploy-checklist" revision={null} {...props}
                                             aside={<p>usage panel</p>} />);
    await screen.findByRole("heading", { name: "deploy-checklist" });
    const panel = screen.getByText("usage panel");
    fetchSkill.mockImplementationOnce(() => new Promise((r) => { resolveNext = r; }));
    rerender(<SkillDetail name="deploy-checklist" revision={1} {...props} aside={<p>usage panel</p>} />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.getByText("usage panel")).toBe(panel);
    await act(async () => { resolveNext(detail({ revision: 1 })); });
    expect(screen.getByText("usage panel")).toBe(panel);
  });

  it("shows the newest revisions and the viewed one, and expands to all of them", async () => {
    const long = [6, 5, 4, 3, 2, 1].map((r) => ({ revision: r, actor: null, agent_label: null,
      change_summary: `change ${r}`, package_hash: `h${r}`, created_at: "2026-10-16T10:00:00+00:00", ...rv }));
    fetchSkill.mockResolvedValue(detail({ revision: 1, current_revision: 6, latest_revision: 6,
      approved_revision: 6, history: long }));
    render(<SkillDetail name="deploy-checklist" revision={1} {...props} />);
    expect(await screen.findByRole("button", { name: "rev 6" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "rev 1" })).toBeInTheDocument();   // the viewed one
    expect(screen.queryByRole("button", { name: "rev 2" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show all 6 revisions" }));
    expect(screen.getByRole("button", { name: "rev 2" })).toBeInTheDocument();
  });

  it("names the reviewer of each history entry", async () => {
    fetchSkill.mockResolvedValue(detail({ history: [
      { revision: 2, actor: null, agent_label: "claude-code", change_summary: "edit", package_hash: "h2",
        created_at: "2026-10-19T10:00:00+00:00", review_status: "rejected", reviewer_label: "review-session",
        review_notes: "widens tools", reviewed_at: "2026-10-19T10:05:00+00:00" },
      { revision: 1, actor: null, agent_label: null, change_summary: "create", package_hash: "h1",
        created_at: "2026-10-16T10:00:00+00:00", ...rv },
    ] }));
    render(<SkillDetail name="deploy-checklist" revision={null} {...props} />);
    expect(await screen.findByText("rejected by review-session · widens tools")).toBeInTheDocument();
  });
});
