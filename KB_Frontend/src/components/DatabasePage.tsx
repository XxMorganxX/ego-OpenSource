import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import {
  clearDbPassword,
  dbAuth,
  fetchDbTable,
  fetchDbTables,
  hasDbPassword,
  storeDbPassword,
} from "../api/client";
import { useUrlParam } from "../hooks/useUrlParam";
import type { DbTableMeta, DbTablePage } from "../types";
import { DatabaseIcon } from "./icons";

const PAGE_SIZES = [25, 50, 100, 250, 500];
const DEFAULT_PAGE_SIZE = 50;

/** True when a request failed the password gate (stored password missing or wrong). */
function isAuthError(err: unknown): boolean {
  return err instanceof Error && err.message.startsWith("401");
}

// One reused Intl formatter rather than per-call toLocaleString (which rebuilds it each time).
const COUNT_FMT = new Intl.NumberFormat();
function formatCount(n: number | null): string {
  return n == null ? "—" : COUNT_FMT.format(n);
}

/** One cell rendered the way Postgres holds it: NULL is flagged; objects/arrays as JSON. */
function cellText(value: unknown): { text: string; isNull: boolean } {
  if (value === null || value === undefined) return { text: "NULL", isNull: true };
  if (typeof value === "object") return { text: JSON.stringify(value), isNull: false };
  return { text: String(value), isNull: false };
}

/** The password prompt shown until the shared DB password is verified for this session. */
function Gate({ onUnlock }: { onUnlock: () => void }) {
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!pw || busy) return;
    setBusy(true);
    setError(null);
    try {
      await dbAuth(pw);
      storeDbPassword(pw);
      onUnlock();
    } catch (err) {
      setError(
        isAuthError(err)
          ? "Incorrect password."
          : err instanceof Error && err.message.startsWith("429")
            ? "Too many incorrect attempts — try again later."
            : "Unable to reach the database service.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="db-gate">
      <form className="db-gate-card" onSubmit={submit}>
        <span className="db-gate-mark">
          <DatabaseIcon size={22} />
        </span>
        <h2>Database</h2>
        <p>
          This page mirrors the live Postgres tables one-to-one. Enter the access
          password to continue.
        </p>
        <input
          type="password"
          name="db-password"
          autoComplete="current-password"
          autoFocus
          aria-label="Database password"
          placeholder="Password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
        />
        {error && (
          <div className="db-gate-error" role="alert">
            {error}
          </div>
        )}
        <button type="submit" className="db-gate-btn" disabled={!pw || busy}>
          {busy ? "Checking…" : "Unlock"}
        </button>
      </form>
    </div>
  );
}

export function DatabasePage() {
  const [unlocked, setUnlocked] = useState(hasDbPassword);
  const [tables, setTables] = useState<DbTableMeta[] | null>(null);
  // Selected table, page size and offset live in the URL so a browsed table+page is shareable.
  const [tableParam, setTableParam] = useUrlParam<string>("table", "");
  const selected = useMemo(() => {
    if (!tableParam) return null;
    const dot = tableParam.indexOf(".");
    return dot === -1
      ? { schema: "public", name: tableParam }
      : { schema: tableParam.slice(0, dot), name: tableParam.slice(dot + 1) };
  }, [tableParam]);
  const [page, setPage] = useState<DbTablePage | null>(null);
  const [rowsParam, setRowsParam] = useUrlParam<string>("rows", String(DEFAULT_PAGE_SIZE));
  const pageSize = PAGE_SIZES.includes(Number(rowsParam)) ? Number(rowsParam) : DEFAULT_PAGE_SIZE;
  const [offsetParam, setOffsetParam] = useUrlParam<string>("offset", "0");
  const offset = Math.max(0, Number(offsetParam) || 0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A stale/invalid session password — wipe it and fall back to the gate.
  const relock = useCallback(() => {
    clearDbPassword();
    setUnlocked(false);
    setTables(null);
    setTableParam("");
    setPage(null);
  }, [setTableParam]);

  // Load the table list once unlocked.
  useEffect(() => {
    if (!unlocked) return;
    let alive = true;
    fetchDbTables()
      .then((r) => alive && setTables(r.tables))
      .catch((err) => {
        if (!alive) return;
        if (isAuthError(err)) relock();
        else setError(String(err));
      });
    return () => {
      alive = false;
    };
  }, [unlocked, relock]);

  // Load the selected table's current page whenever the table, page size, or offset changes.
  useEffect(() => {
    if (!selected) return;
    let alive = true;
    setLoading(true);
    setError(null);
    fetchDbTable(selected.schema, selected.name, { limit: pageSize, offset })
      .then((p) => alive && setPage(p))
      .catch((err) => {
        if (!alive) return;
        if (isAuthError(err)) relock();
        else setError(String(err));
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [selected, pageSize, offset, relock]);

  const openTable = (t: DbTableMeta) => {
    setTableParam(`${t.schema}.${t.name}`);
    setOffsetParam("0");
    setPage(null);
  };

  if (!unlocked) return <Gate onUnlock={() => setUnlocked(true)} />;

  const total = page?.total ?? 0;
  const shownFrom = total === 0 ? 0 : offset + 1;
  const shownTo = Math.min(offset + (page?.rows.length ?? 0), total);
  const isSelected = (t: DbTableMeta) =>
    selected?.schema === t.schema && selected?.name === t.name;

  return (
    <div className="db-page">
      <aside className="db-tables">
        <div className="db-tables-head">
          Tables{tables ? <span className="db-count">{tables.length}</span> : null}
        </div>
        <div className="db-tables-list">
          {tables === null ? (
            <div className="db-hint">Loading tables…</div>
          ) : tables.length === 0 ? (
            <div className="db-hint">No tables.</div>
          ) : (
            tables.map((t) => (
              <button
                key={`${t.schema}.${t.name}`}
                className={`db-table-item${isSelected(t) ? " active" : ""}`}
                aria-current={isSelected(t) ? "true" : undefined}
                onClick={() => openTable(t)}
              >
                <span className="db-table-name">
                  {t.schema !== "public" && (
                    <span className="db-schema">{t.schema}.</span>
                  )}
                  {t.name}
                </span>
                <span className="db-table-meta">
                  ~{formatCount(t.est_rows)} · {t.columns} cols
                </span>
              </button>
            ))
          )}
        </div>
      </aside>

      <section className="db-main">
        {!selected ? (
          <div className="db-empty">
            <DatabaseIcon size={28} />
            <p>Select a table to browse its rows.</p>
          </div>
        ) : (
          <>
            <header className="db-main-head">
              <div className="db-title">
                {selected.schema !== "public" && (
                  <span className="db-schema">{selected.schema}.</span>
                )}
                {selected.name}
                <span className="db-total">{formatCount(total)} rows</span>
              </div>
              <div className="db-toolbar">
                <label className="db-pagesize">
                  Rows
                  <select
                    name="page-size"
                    value={pageSize}
                    onChange={(e) => {
                      setRowsParam(e.target.value);
                      setOffsetParam("0");
                    }}
                  >
                    {PAGE_SIZES.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                </label>
                <span className="db-range">
                  {shownFrom}–{shownTo} of {formatCount(total)}
                </span>
                <button
                  className="db-pager"
                  disabled={offset === 0 || loading}
                  onClick={() => setOffsetParam(String(Math.max(0, offset - pageSize)))}
                >
                  Prev
                </button>
                <button
                  className="db-pager"
                  disabled={offset + pageSize >= total || loading}
                  onClick={() => setOffsetParam(String(offset + pageSize))}
                >
                  Next
                </button>
              </div>
            </header>

            {error ? (
              <div className="db-error" role="alert">
                {error}
              </div>
            ) : null}

            <div className="db-grid">
              {page === null ? (
                <div className="db-hint">Loading…</div>
              ) : (
                <table className="db-data">
                  <thead>
                    <tr>
                      {page.columns.map((c) => (
                        <th
                          key={c.name}
                          scope="col"
                          title={`${c.type}${c.nullable ? "" : " · not null"}`}
                        >
                          <span className="db-col-name">
                            {c.name}
                            {page.primary_key.includes(c.name) && (
                              <span className="db-pk" title="primary key">
                                PK
                              </span>
                            )}
                          </span>
                          <span className="db-col-type">{c.type}</span>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {page.rows.length === 0 ? (
                      <tr>
                        <td className="db-hint" colSpan={page.columns.length || 1}>
                          No rows.
                        </td>
                      </tr>
                    ) : (
                      page.rows.map((row, i) => (
                        <tr key={i}>
                          {page.columns.map((c) => {
                            const { text, isNull } = cellText(row[c.name]);
                            return (
                              <td
                                key={c.name}
                                className={isNull ? "db-null" : undefined}
                                title={text}
                              >
                                {text}
                              </td>
                            );
                          })}
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )}
      </section>
    </div>
  );
}
