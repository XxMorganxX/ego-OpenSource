import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { DbTablePage } from "../types";

const dbAuth = vi.fn();
const fetchDbTables = vi.fn();
const fetchDbTable = vi.fn();
const hasDbPassword = vi.fn();
const storeDbPassword = vi.fn();
const clearDbPassword = vi.fn();

vi.mock("../api/client", () => ({
  dbAuth: (pw: string) => dbAuth(pw),
  fetchDbTables: () => fetchDbTables(),
  fetchDbTable: (s: string, n: string, o: unknown) => fetchDbTable(s, n, o),
  hasDbPassword: () => hasDbPassword(),
  storeDbPassword: (pw: string) => storeDbPassword(pw),
  clearDbPassword: () => clearDbPassword(),
}));

import { DatabasePage } from "./DatabasePage";

const tables = [
  { schema: "public", name: "nodes", columns: 5, est_rows: 42 },
  { schema: "public", name: "schema_migrations", columns: 1, est_rows: 3 },
];

const nodesPage: DbTablePage = {
  schema: "public",
  name: "nodes",
  columns: [
    { name: "id", type: "uuid", nullable: false, default: null },
    { name: "title", type: "text", nullable: true, default: null },
  ],
  primary_key: ["id"],
  rows: [
    { id: "abc-123", title: "Alpha" },
    { id: "def-456", title: null },
  ],
  total: 2,
  limit: 50,
  offset: 0,
};

describe("DatabasePage", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/"); // reset URL-backed table/page between tests
    dbAuth.mockReset();
    fetchDbTables.mockReset().mockResolvedValue({ tables });
    fetchDbTable.mockReset().mockResolvedValue(nodesPage);
    hasDbPassword.mockReset().mockReturnValue(false);
    storeDbPassword.mockReset();
    clearDbPassword.mockReset();
  });

  it("shows the password gate and does not load data while locked", () => {
    render(<DatabasePage />);
    expect(screen.getByLabelText(/database password/i)).toBeInTheDocument();
    expect(fetchDbTables).not.toHaveBeenCalled();
  });

  it("rejects a wrong password with an inline error", async () => {
    dbAuth.mockRejectedValue(new Error("401: invalid db viewer password"));
    render(<DatabasePage />);
    await userEvent.type(screen.getByLabelText(/database password/i), "nope");
    await userEvent.click(screen.getByRole("button", { name: /unlock/i }));
    await screen.findByText(/incorrect password/i);
    expect(storeDbPassword).not.toHaveBeenCalled();
  });

  it("says so when the server has locked out repeated wrong passwords", async () => {
    dbAuth.mockRejectedValue(new Error("429: too many failed db viewer attempts"));
    render(<DatabasePage />);
    await userEvent.type(screen.getByLabelText(/database password/i), "nope");
    await userEvent.click(screen.getByRole("button", { name: /unlock/i }));
    await screen.findByText(/too many incorrect attempts/i);
    expect(storeDbPassword).not.toHaveBeenCalled();
  });

  it("unlocks, lists tables, and browses a table's rows", async () => {
    dbAuth.mockResolvedValue({ ok: true });
    render(<DatabasePage />);
    await userEvent.type(screen.getByLabelText(/database password/i), "s3cret");
    await userEvent.click(screen.getByRole("button", { name: /unlock/i }));

    await waitFor(() => expect(storeDbPassword).toHaveBeenCalledWith("s3cret"));
    await screen.findByRole("button", { name: /^nodes/i });

    await userEvent.click(screen.getByRole("button", { name: /^nodes/i }));
    await waitFor(() =>
      expect(fetchDbTable).toHaveBeenCalledWith("public", "nodes", {
        limit: 50,
        offset: 0,
      }),
    );
    await screen.findByText("Alpha");
    // The pk badge and a NULL cell both render 1:1 with the row data.
    expect(screen.getByText("PK")).toBeInTheDocument();
    expect(screen.getAllByText("NULL").length).toBeGreaterThan(0);
  });
});
