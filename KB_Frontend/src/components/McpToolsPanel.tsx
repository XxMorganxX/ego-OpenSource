import { useCallback, useEffect, useRef, useState } from "react";

import {
  clearDbPassword,
  dbAuth,
  fetchMcpTools,
  hasDbPassword,
  setMcpToolEnabled,
  storeDbPassword,
} from "../api/client";
import { useChangeFeed } from "../hooks/useChangeFeed";
import type { McpToolState } from "../types";

/** True when a request failed the password gate (stored password missing or wrong) --
 *  same test DatabasePage.tsx's relock() uses. */
function isAuthError(err: unknown): boolean {
  return err instanceof Error && err.message.startsWith("401");
}

/**
 * Server-side enable/disable for KB_MCP's tools. The flags live in mcp_tool_state and
 * are enforced by the MCP server itself, so they hold for every client — NOT just this
 * dashboard, and NOT just Claude Code. A tool shown as active here can still be blocked
 * client-side by a deny rule in ~/.claude/settings.json; this panel reports server state
 * only and makes no claim about any particular client.
 */
function Row({
  tool,
  busy,
  locked,
  onToggle,
}: {
  tool: McpToolState;
  busy: boolean;
  /** True while the session has not authenticated with the DB password yet — the
   *  switch is withheld entirely (rather than shown-disabled) so nothing here
   *  looks interactive until it actually is. */
  locked: boolean;
  onToggle: (t: McpToolState) => void;
}) {
  return (
    <li className="mcp-tool-row">
      <span className="mcp-tool-name">{tool.tool}</span>
      {tool.updated_by && (
        <span className="mcp-tool-meta">
          {tool.updated_by} · {new Date(tool.updated_at).toLocaleDateString()}
        </span>
      )}
      {!locked && (
        <button
          role="switch"
          aria-checked={tool.enabled}
          aria-label={`${tool.enabled ? "Disable" : "Enable"} ${tool.tool}`}
          className={`mcp-tool-switch${tool.enabled ? " on" : ""}`}
          disabled={busy}
          onClick={() => onToggle(tool)}
        />
      )}
    </li>
  );
}

export function McpToolsPanel() {
  const [tools, setTools] = useState<McpToolState[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [unlocked, setUnlocked] = useState(hasDbPassword());
  const [password, setPassword] = useState("");

  const liveRef = useRef(true);
  useEffect(() => {
    liveRef.current = true;
    return () => {
      liveRef.current = false;
    };
  }, []);

  const load = useCallback(() => {
    fetchMcpTools().then((d) => {
      if (liveRef.current) setTools(d.tools);
    });
  }, []);

  useEffect(load, [load]);

  useChangeFeed((tables) => {
    if (tables.has("mcp_tool_state")) load();
  });

  const toggle = useCallback((t: McpToolState) => {
    setBusy(t.tool);
    setError(null);
    // No optimistic update: the row shows what the server confirmed, so a rejected
    // write can never leave the UI claiming a tool is off when it is still live.
    setMcpToolEnabled(t.tool, !t.enabled)
      .then((updated) => {
        if (!liveRef.current) return;
        setTools((prev) =>
          (prev ?? []).map((x) => (x.tool === updated.tool ? updated : x)),
        );
      })
      .catch((e: Error) => {
        if (!liveRef.current) return;
        if (isAuthError(e)) {
          // The stored db password is stale (e.g. rotated by an ordinary ops
          // action) -- drop back to locked so the password form is reachable
          // again. Without this a 401 leaves the panel stuck "unlocked" with
          // every toggle refusing and no in-panel way to re-enter a password
          // (mirrors DatabasePage.tsx's relock()).
          clearDbPassword();
          setUnlocked(false);
          return;
        }
        setError(e.message);
      })
      .finally(() => {
        if (liveRef.current) setBusy(null);
      });
  }, []);

  const unlock = useCallback(() => {
    setError(null);
    dbAuth(password)
      .then(() => {
        storeDbPassword(password);
        setPassword("");
        setUnlocked(true);
      })
      .catch((e: Error) => setError(e.message));
  }, [password]);

  if (tools === null) return <div className="linked-empty">Loading…</div>;
  if (tools.length === 0) {
    return (
      <div className="linked-empty">
        KB_MCP has not published its tool inventory yet — start the MCP server against
        this database.
      </div>
    );
  }

  const writes = tools.filter((t) => t.kind === "write");
  const reads = tools.filter((t) => t.kind === "read");
  const off = tools.filter((t) => !t.enabled).length;

  return (
    <div className="mcp-tools">
      <div className="mcp-tools-counts">
        <span>{writes.length} write</span>
        <span>{reads.length} read</span>
        {off > 0 && <span className="mcp-tools-off">{off} disabled</span>}
      </div>

      {error && (
        <div className="mcp-tools-error" role="alert">
          {error}
        </div>
      )}

      {!unlocked && (
        <div className="mcp-tools-lock">
          <label htmlFor="mcp-pw">Database password</label>
          <input
            id="mcp-pw"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && unlock()}
          />
          <button onClick={unlock}>Unlock</button>
        </div>
      )}

      <h4>Writes</h4>
      <ul className="mcp-tool-list">
        {writes.map((t) => (
          <Row
            key={t.tool}
            tool={t}
            busy={busy === t.tool}
            locked={!unlocked}
            onToggle={toggle}
          />
        ))}
      </ul>
      <h4>Reads</h4>
      <ul className="mcp-tool-list">
        {reads.map((t) => (
          <Row
            key={t.tool}
            tool={t}
            busy={busy === t.tool}
            locked={!unlocked}
            onToggle={toggle}
          />
        ))}
      </ul>
    </div>
  );
}
