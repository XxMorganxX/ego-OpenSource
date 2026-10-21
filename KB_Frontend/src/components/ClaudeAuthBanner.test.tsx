import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ClaudeAuthStatus } from "../types";

const fetchClaudeAuth = vi.fn();
const postClaudeRelogin = vi.fn();
const hasDbPassword = vi.fn();
vi.mock("../api/client", () => ({
  fetchClaudeAuth: () => fetchClaudeAuth(),
  postClaudeRelogin: () => postClaudeRelogin(),
  hasDbPassword: () => hasDbPassword(),
}));

import { ClaudeAuthBanner } from "./ClaudeAuthBanner";

const status = (over: Partial<ClaudeAuthStatus> = {}): ClaudeAuthStatus => ({
  level: "warn", logged_in: true, days_left: 6.4,
  expires_at: "2026-09-07T19:32:44+00:00", email: "m@example.com",
  relogin_enabled: true, ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  postClaudeRelogin.mockResolvedValue({ launched: true });
  hasDbPassword.mockReturnValue(true);
});

describe("ClaudeAuthBanner", () => {
  it("renders nothing while auth is healthy", async () => {
    fetchClaudeAuth.mockResolvedValue(status({ level: "ok", days_left: 20 }));
    const { container } = render(<ClaudeAuthBanner />);
    await waitFor(() => expect(fetchClaudeAuth).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("warns with the day count and always shows the copyable command", async () => {
    fetchClaudeAuth.mockResolvedValue(status());
    render(<ClaudeAuthBanner />);
    await waitFor(() => expect(screen.getByRole("status")).toBeInTheDocument());
    // Own modifier class, not the generic `.warn` used elsewhere in the app —
    // that class collides in specificity and must not be reused here.
    expect(screen.getByRole("status").className).toContain("claude-auth-warn");
    expect(screen.getByText(/expires in 6 days/)).toBeInTheDocument();
    // The command is present in every non-ok state, not revealed on failure — a
    // click from a phone opens a Terminal nobody sees, and this is the way out.
    expect(screen.getByText("claude auth login")).toBeInTheDocument();
  });

  it("shows the signed-out state as critical", async () => {
    fetchClaudeAuth.mockResolvedValue(
      status({ level: "out", logged_in: false, days_left: null }));
    render(<ClaudeAuthBanner />);
    await waitFor(() => expect(screen.getByText(/signed out/)).toBeInTheDocument());
    expect(screen.getByRole("status").className).toContain("claude-auth-critical");
  });

  // Sub-day and negative days_left render badly with a naive Math.floor: "0 days"
  // for the entire final day (peak urgency), "-1 days" once the CLI lags the
  // actual expiry, and "1 days" (plural) at 1.3 days left.
  it.each([
    [6.4, /expires in 6 days/],
    [1.3, /expires in 1 day\b/],
    [0.4, /expires in 9 hours/],
    [-1, /expires in less than an hour/],
  ])("renders days_left=%s as %s", async (daysLeft, expected) => {
    fetchClaudeAuth.mockResolvedValue(status({ days_left: daysLeft }));
    render(<ClaudeAuthBanner />);
    await waitFor(() => expect(screen.getByRole("status")).toBeInTheDocument());
    expect(screen.getByText(expected)).toBeInTheDocument();
  });

  // The one-click re-auth starts a process on the host, so the server only offers it
  // when explicitly opted in. Off (or an older server that does not report it), the
  // banner still warns and shows the command, but has no button to click.
  it.each([
    ["disabled", { relogin_enabled: false }],
    ["unreported", { relogin_enabled: undefined }],
  ])("hides the re-authenticate button when relogin is %s", async (_label, over) => {
    fetchClaudeAuth.mockResolvedValue(status(over));
    render(<ClaudeAuthBanner />);
    await waitFor(() => expect(screen.getByRole("status")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /re-authenticate/i })).toBeNull();
    expect(screen.getByText("claude auth login")).toBeInTheDocument();
  });

  it("confirms when the Terminal opened", async () => {
    fetchClaudeAuth.mockResolvedValue(status());
    render(<ClaudeAuthBanner />);
    await waitFor(() => expect(screen.getByRole("status")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /re-authenticate/i }));
    await waitFor(() =>
      expect(screen.getByText(/finish the login there/i)).toBeInTheDocument());
  });

  // dbHeaders() reads a per-tab sessionStorage key only ever set by the Database
  // page / MCP tools unlock form. A fresh tab has none, so posting anyway 401s
  // and would otherwise blame the user for "the wrong password" when they never
  // entered one — the fix must short-circuit before the request, not after.
  it("guides the user to unlock instead of POSTing when no db password is set", async () => {
    fetchClaudeAuth.mockResolvedValue(status());
    hasDbPassword.mockReturnValue(false);
    render(<ClaudeAuthBanner />);
    await waitFor(() => expect(screen.getByRole("status")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /re-authenticate/i }));
    await waitFor(() =>
      expect(screen.getByText(/unlock on the database page first/i)).toBeInTheDocument());
    expect(postClaudeRelogin).not.toHaveBeenCalled();
    // The command must stay visible in this branch too.
    expect(screen.getByText("claude auth login")).toBeInTheDocument();
  });

  // asJson throws `${status}: ${body}`, so the gate's two failure modes are
  // distinguishable and must not collapse into one vague message.
  it.each([
    ["401: invalid db viewer password", /wrong database password/i],
    ["503: db viewer not configured", /not configured/i],
    ["429: too many failed db viewer attempts", /too many wrong passwords/i],
    ["404: Not Found", /re-auth is turned off/i],
    ["500: boom", /could not open a terminal/i],
  ])("reports %s distinctly", async (thrown, expected) => {
    fetchClaudeAuth.mockResolvedValue(status());
    postClaudeRelogin.mockRejectedValue(new Error(thrown));
    render(<ClaudeAuthBanner />);
    await waitFor(() => expect(screen.getByRole("status")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /re-authenticate/i }));
    await waitFor(() => expect(screen.getByText(expected)).toBeInTheDocument());
  });
});
