import { useCallback, useEffect, useRef, useState } from "react";

import { fetchClaudeAuth, hasDbPassword, postClaudeRelogin } from "../api/client";
import type { ClaudeAuthStatus } from "../types";

const POLL_MS = 30 * 60 * 1000;   // a 29-day deadline needs nothing tighter

/** "expires in 6 days" / "expires in 1 day" / "expires in 7 hours" / clamped at zero. */
function formatDaysLeft(daysLeft: number): string {
  if (daysLeft >= 1) {
    const days = Math.floor(daysLeft);
    return `expires in ${days} day${days === 1 ? "" : "s"}`;
  }
  const hours = Math.floor(daysLeft * 24);
  if (hours < 1) return "expires in less than an hour";
  return `expires in ${hours} hour${hours === 1 ? "" : "s"}`;
}

/**
 * Warns before the Claude credential that drives enrichment expires, and — only when
 * the server opted in (KB_DASHBOARD_ALLOW_RELOGIN, reported as `relogin_enabled`) —
 * offers a one-click re-auth that opens a Terminal on the host. Without the opt-in
 * the banner shows the command to run instead.
 *
 * Renders NOTHING while healthy — that is the feature, not an optimisation. The
 * credential is fine ~22 of every 29 days, so the banner's appearance is itself the
 * signal; a permanently-visible status pill would be trained out within one cycle.
 */
export function ClaudeAuthBanner() {
  const [status, setStatus] = useState<ClaudeAuthStatus | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const refresh = useCallback(() => {
    fetchClaudeAuth()
      .then((s) => {
        if (aliveRef.current) setStatus(s);
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    refresh();
    // Auth state lives in a file and a subprocess, so no DB NOTIFY will ever fire
    // for it — the shared change feed cannot carry this and we poll instead.
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  const relogin = () => {
    setNotice(null);
    if (!hasDbPassword()) {
      // dbHeaders() reads a per-tab sessionStorage key only ever set by the
      // Database page / MCP tools unlock form. A fresh tab has none, so
      // posting anyway would 401 and wrongly blame the user for "the wrong
      // password" when they never entered one.
      setNotice({ kind: "err",
                  text: "Unlock on the Database page first, then click Re-authenticate." });
      return;
    }
    postClaudeRelogin()
      .then(() => {
        if (aliveRef.current) {
          setNotice({ kind: "ok",
                      text: "Terminal opened on the host — finish the login there." });
        }
      })
      .catch((e: Error) => {
        if (!aliveRef.current) return;
        // asJson throws `${status}: ${body}`, so the status code is the message
        // prefix — the two gate failures need different fixes and must say so.
        const text = e.message.startsWith("401")
          ? "Wrong database password."
          : e.message.startsWith("429")
            ? "Too many wrong passwords — try again later."
            : e.message.startsWith("503")
              ? "Database password not configured (KB_DB_VIEWER_PASSWORD)."
              : e.message.startsWith("404")
                ? "One-click re-auth is turned off — run the command below on the host."
                : "Could not open a Terminal — run the command below on the host.";
        setNotice({ kind: "err", text });
      });
  };

  if (!status || status.level === "ok") return null;

  const urgent = status.level === "critical" || status.level === "out";
  const headline =
    status.level === "out"
      ? "Claude auth is signed out — enrichment is stranding"
      : `Claude auth ${formatDaysLeft(status.days_left ?? 0)}`;

  return (
    <div className={`claude-auth-banner ${urgent ? "claude-auth-critical" : "claude-auth-warn"}`}
         role="status">
      <span className="claude-auth-headline">{headline}</span>
      {status.expires_at && (
        <span className="claude-auth-detail">
          {new Date(status.expires_at).toLocaleDateString()}
          {status.email ? ` · ${status.email}` : ""}
        </span>
      )}
      {status.relogin_enabled && (
        <button className="claude-auth-btn" onClick={relogin}>
          Re-authenticate
        </button>
      )}
      <code className="claude-auth-cmd">claude auth login</code>
      {notice && (
        <span className={notice.kind === "ok" ? "claude-auth-notice" : "claude-auth-error"}>
          {notice.text}
        </span>
      )}
    </div>
  );
}
