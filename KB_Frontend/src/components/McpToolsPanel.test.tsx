import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { McpToolState } from "../types";

const fetchMcpTools = vi.fn();
const setMcpToolEnabled = vi.fn();
const dbAuth = vi.fn();
const hasDbPassword = vi.fn();
const storeDbPassword = vi.fn();
const clearDbPassword = vi.fn();
vi.mock("../api/client", () => ({
  fetchMcpTools: () => fetchMcpTools(),
  setMcpToolEnabled: (t: string, e: boolean) => setMcpToolEnabled(t, e),
  dbAuth: (p: string) => dbAuth(p),
  hasDbPassword: () => hasDbPassword(),
  storeDbPassword: (p: string) => storeDbPassword(p),
  clearDbPassword: () => clearDbPassword(),
}));

let feedCb: ((tables: Set<string>) => void) | undefined;
vi.mock("../hooks/useChangeFeed", () => ({
  useChangeFeed: (cb: (tables: Set<string>) => void) => {
    feedCb = cb;
  },
}));

import { McpToolsPanel } from "./McpToolsPanel";

const tools: McpToolState[] = [
  { tool: "search_memories", kind: "read", enabled: true,
    updated_at: "2026-07-29T10:00:00+00:00", updated_by: null },
  { tool: "get_memory", kind: "read", enabled: true,
    updated_at: "2026-07-29T10:00:00+00:00", updated_by: null },
  { tool: "add_memory", kind: "write", enabled: false,
    updated_at: "2026-07-29T11:00:00+00:00", updated_by: "owner" },
];

describe("McpToolsPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchMcpTools.mockResolvedValue({ tools });
    hasDbPassword.mockReturnValue(true);
  });

  it("renders both groups with counts", async () => {
    render(<McpToolsPanel />);
    await screen.findByText("add_memory");
    expect(screen.getByText(/1 write/)).toBeInTheDocument();
    expect(screen.getByText(/2 read/)).toBeInTheDocument();
  });

  it("shows who last changed a tool", async () => {
    render(<McpToolsPanel />);
    await screen.findByText("add_memory");
    expect(screen.getByText(/owner/)).toBeInTheDocument();
  });

  it("toggles a tool and reflects the server response", async () => {
    setMcpToolEnabled.mockResolvedValue({ ...tools[2], enabled: true });
    render(<McpToolsPanel />);
    const row = (await screen.findByText("add_memory")).closest("li")!;
    await userEvent.click(within(row).getByRole("switch"));
    await waitFor(() =>
      expect(setMcpToolEnabled).toHaveBeenCalledWith("add_memory", true),
    );
    await waitFor(() =>
      expect(within(row).getByRole("switch")).toHaveAttribute("aria-checked", "true"),
    );
  });

  it("shows an inline error and leaves the switch alone on failure", async () => {
    // A non-auth failure (404/500): inline error only, panel stays unlocked.
    setMcpToolEnabled.mockRejectedValue(new Error("500: broken"));
    render(<McpToolsPanel />);
    const row = (await screen.findByText("add_memory")).closest("li")!;
    await userEvent.click(within(row).getByRole("switch"));
    expect(await screen.findByRole("alert")).toHaveTextContent("500");
    // No optimistic update: still disabled.
    expect(within(row).getByRole("switch")).toHaveAttribute("aria-checked", "false");
    expect(clearDbPassword).not.toHaveBeenCalled();
  });

  it("re-locks the panel on a 401 instead of just showing an error", async () => {
    setMcpToolEnabled.mockRejectedValue(new Error("401: invalid db viewer password"));
    render(<McpToolsPanel />);
    const row = (await screen.findByText("add_memory")).closest("li")!;
    await userEvent.click(within(row).getByRole("switch"));
    await waitFor(() => expect(clearDbPassword).toHaveBeenCalled());
    // Back to locked: password form reachable again, no switches left to click.
    expect(await screen.findByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  });

  it("prompts for the password when none is stored", async () => {
    hasDbPassword.mockReturnValue(false);
    render(<McpToolsPanel />);
    await screen.findByText("add_memory");
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  });

  it("unlocks after a successful auth", async () => {
    hasDbPassword.mockReturnValue(false);
    dbAuth.mockResolvedValue({ ok: true });
    render(<McpToolsPanel />);
    await userEvent.type(await screen.findByLabelText(/password/i), "pw");
    await userEvent.click(screen.getByRole("button", { name: /unlock/i }));
    await waitFor(() => expect(storeDbPassword).toHaveBeenCalledWith("pw"));
    await waitFor(() => expect(screen.getAllByRole("switch").length).toBe(3));
  });

  it("renders an empty state when nothing has been published", async () => {
    fetchMcpTools.mockResolvedValue({ tools: [] });
    render(<McpToolsPanel />);
    expect(await screen.findByText(/has not published/i)).toBeInTheDocument();
  });

  it("refetches when mcp_tool_state changes", async () => {
    render(<McpToolsPanel />);
    await screen.findByText("add_memory");
    expect(fetchMcpTools).toHaveBeenCalledTimes(1);
    feedCb?.(new Set(["mcp_tool_state"]));
    await waitFor(() => expect(fetchMcpTools).toHaveBeenCalledTimes(2));
  });

  it("ignores unrelated change-feed tables", async () => {
    render(<McpToolsPanel />);
    await screen.findByText("add_memory");
    feedCb?.(new Set(["nodes"]));
    expect(fetchMcpTools).toHaveBeenCalledTimes(1);
  });
});
