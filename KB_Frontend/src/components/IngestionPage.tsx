import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  dismissStuckRecord,
  fetchIngestLog,
  fetchIngestStatus,
  retryFailedIngest,
} from "../api/client";
import { useChangeFeed } from "../hooks/useChangeFeed";
import { useUrlParam } from "../hooks/useUrlParam";
import {
  classifyError,
  countByEvent,
  EVENT_LABELS,
  errorText,
  FAILED_EVENT,
  groupByNode,
  groupFailures,
  providersOf,
  shortProviders,
  titleOf,
} from "../lib/ingest";
import { agentLastSeenLabel } from "../lib/identity";
import type { IngestEvent, IngestStatus, StuckRecord } from "../types";
import { ActivityIcon, ChevronIcon, FunnelIcon, LayersIcon } from "./icons";

/** Refresh relative timestamps ("3s ago") without refetching. */
const CLOCK_MS = 5000;
/** Safety-net refetch in case the live socket is down (primary refresh is push). */
const FALLBACK_POLL_MS = 20_000;
const LOG_LIMIT = 100;
/** A source is "active" if it emitted an event within this window. */
const ACTIVE_WINDOW_MS = 90_000;
/** Keep the "running" LED lit briefly after work drains, so it doesn't blink between nodes. */
const RUNNING_LINGER_MS = 4000;

/** Dot colors for the rollup chips and node-lifecycle timeline, by event type. */
const EVENT_COLORS: Record<string, string> = {
  enrichment_done: "var(--note)",
  node_created: "var(--accent-bright)",
  node_updated: "var(--edge)",
  node_deleted: "var(--text-faint)",
  enrichment_failed: "var(--danger)",
  ingest_failed: "var(--danger)",
  ingest_resolved: "var(--text-faint)",
};

/** Time funnel for the failures view (client-side narrowing of the fetched log). */
type TimeWindow = "all" | "1h" | "24h" | "7d";
const WINDOW_MS: Record<TimeWindow, number | null> = {
  all: null,
  "1h": 3_600_000,
  "24h": 86_400_000,
  "7d": 604_800_000,
};
const WINDOW_LABELS: Record<TimeWindow, string> = {
  all: "All time",
  "1h": "Last hour",
  "24h": "Last 24h",
  "7d": "Last 7 days",
};

// Enrichment statuses shown as live counters, in pipeline order.
const ENRICHMENT_STAGES: { key: string; label: string; tone: string }[] = [
  { key: "processing", label: "Processing", tone: "running" },
  { key: "pending", label: "Pending", tone: "queued" },
  { key: "failed", label: "Failed", tone: "danger" },
  { key: "done", label: "Done", tone: "done" },
];

// Locale-aware formatters, instantiated once (constructing Intl formatters per call is costly).
// `narrow` keeps the compact "3s ago / 4m ago / 2h ago / 5d ago" look while staying localizable.
const RELATIVE_FMT = new Intl.RelativeTimeFormat(undefined, {
  numeric: "always",
  style: "narrow",
});
const TIME_FMT = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

function timeAgo(iso: string, now: number): string {
  const diff = Math.max(0, now - new Date(iso).getTime());
  const s = Math.max(1, Math.round(diff / 1000));
  if (s < 60) return RELATIVE_FMT.format(-s, "second");
  const m = Math.round(s / 60);
  if (m < 60) return RELATIVE_FMT.format(-m, "minute");
  const h = Math.round(m / 60);
  if (h < 24) return RELATIVE_FMT.format(-h, "hour");
  return RELATIVE_FMT.format(-Math.round(h / 24), "day");
}

/** Wall-clock time for the detail panel — short, locale-aware, no date noise. */
function clockTime(iso: string): string {
  return TIME_FMT.format(new Date(iso));
}

/** Banner row identity — a stuck record is (source, external_id), not a node id. */
function stuckKey(r: StuckRecord): string {
  return `${r.source}:${r.external_id}`;
}

export function IngestionPage() {
  const [status, setStatus] = useState<IngestStatus | null>(null);
  const [events, setEvents] = useState<IngestEvent[]>([]);
  // Filters live in the URL so a funnel/filtered/grouped view is shareable and restorable.
  const [srcParam, setSrcParam] = useUrlParam<string>("src", "");
  const sourceFilter = srcParam || null;
  // When "1", the feed is narrowed to enrichment failures.
  const [failuresParam, setFailuresParam] = useUrlParam<string>("failures", "");
  const eventFilter = failuresParam === "1" ? FAILED_EVENT : null;
  // View-only time funnel for the failures list — narrows what's shown (and, via node ids,
  // what "Retry retryable" targets), but never the bulk "retry all" escape hatch.
  const [timeWindow, setTimeWindow] = useUrlParam<TimeWindow>("window", "all");
  // Collapse the flat log into one row per node (lifecycle view).
  const [groupParam, setGroupParam] = useUrlParam<string>("group", "");
  const groupMode = groupParam === "1";
  // Keys of expanded rows (event id in log view, node id in node view).
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loaded, setLoaded] = useState(false);
  // Accumulated source → last-seen event time, grown across loads and never shrunk, so the
  // rail stays stable when the table is filtered to one source (the filtered event fetch
  // can't be allowed to make the other source rows blink out of existence).
  const [sourceSeen, setSourceSeen] = useState<Map<string, string>>(new Map());
  // De-flickered "enrichment in flight" state (see the linger effect below).
  const [running, setRunning] = useState(false);
  // Re-rendered each poll so relative timestamps and "active" flags stay fresh.
  const [now, setNow] = useState(() => Date.now());
  // Retry action: in-flight guard + a transient result message.
  const [retrying, setRetrying] = useState(false);
  const [retryNote, setRetryNote] = useState<string | null>(null);
  // Dismiss action on the stuck banner: the keys ("source:external_id") currently in flight —
  // a Set so several rows can be dismissed concurrently without clobbering each other's busy
  // state. Not optimistic — a row leaves only when the reload after its write says so.
  const [dismissing, setDismissing] = useState<Set<string>>(() => new Set());
  // Node ids the user re-queued, watermarked at the failure event that was showing then.
  // The row is marked "re-queued" until a newer event for that node supersedes the watermark.
  const [requeued, setRequeued] = useState<Map<string, number>>(new Map());
  const filterRef = useRef(sourceFilter);
  filterRef.current = sourceFilter;
  const eventFilterRef = useRef(eventFilter);
  eventFilterRef.current = eventFilter;
  // Latest-event-per-node, read at retry time without making runRetry depend on it.
  const latestByNodeRef = useRef<Map<string, number>>(new Map());

  const load = useCallback(async () => {
    const [st, log] = await Promise.all([
      fetchIngestStatus(),
      fetchIngestLog(LOG_LIMIT, filterRef.current, eventFilterRef.current),
    ]);
    setStatus(st);
    setEvents(log.events);
    // Fold this load's sources into the accumulated set. Seed known names from sync state
    // (kept even if they emit no events), then record each source's newest event time.
    setSourceSeen((prev) => {
      const next = new Map(prev);
      for (const s of st.sources) if (!next.has(s.source)) next.set(s.source, "");
      for (const e of log.events) {
        const cur = next.get(e.source);
        if (cur === undefined || cur === "" || e.at > cur) next.set(e.source, e.at);
      }
      return next;
    });
    setNow(Date.now());
    setLoaded(true);
  }, []);

  // Re-queue failures for another enrichment pass. `nodeIds` targets the retryable subset
  // precisely; `source` is the blunt "everything for this source" escape hatch.
  const runRetry = useCallback(
    async (opts: { source?: string | null; nodeIds?: string[] }, affectedIds: string[]) => {
      setRetrying(true);
      setRetryNote(null);
      try {
        const { requeued: n } = await retryFailedIngest(opts);
        // Mark the affected memories as re-queued, watermarked at their current failure event,
        // so their rows read "re-queued" until the worker produces a fresh outcome for them.
        setRequeued((prev) => {
          const next = new Map(prev);
          for (const id of affectedIds) next.set(id, latestByNodeRef.current.get(id) ?? 0);
          return next;
        });
        setRetryNote(n > 0 ? `Re-queued ${n} for another pass` : "Nothing to re-queue");
        await load();
      } catch {
        setRetryNote("Retry failed — try again");
      } finally {
        setRetrying(false);
      }
    },
    [load],
  );

  // Close a stuck record from the banner. A still-failing pull record is re-opened by the
  // next tick (honest); a push failure, which never retries, leaves for good.
  const runDismiss = useCallback(
    async (r: StuckRecord) => {
      const key = stuckKey(r);
      setDismissing((prev) => new Set(prev).add(key));
      setRetryNote(null);
      try {
        await dismissStuckRecord(r.source, r.external_id);
        await load();
      } catch {
        setRetryNote("Dismiss failed — try again");
      } finally {
        setDismissing((prev) => {
          const next = new Set(prev);
          next.delete(key);
          return next;
        });
      }
    },
    [load],
  );

  // Clear the transient retry note after a few seconds so it doesn't linger.
  useEffect(() => {
    if (!retryNote) return;
    const t = window.setTimeout(() => setRetryNote(null), 5000);
    return () => window.clearTimeout(t);
  }, [retryNote]);

  // The time funnel only exists inside the failures view; reset it whenever the funnel
  // closes (by either toggle) so a stale window can't silently filter the full feed.
  useEffect(() => {
    if (eventFilter !== FAILED_EVENT) setTimeWindow("all");
  }, [eventFilter]);

  // Initial load + a re-query whenever a filter changes.
  useEffect(() => {
    load().catch(() => undefined);
  }, [load, sourceFilter, eventFilter]);

  // Live: refetch when nodes (enrichment status) or the ingest log changes.
  useChangeFeed((tables) => {
    if (tables.has("nodes") || tables.has("ingest_log")) {
      load().catch(() => undefined);
    }
  });

  // Keep relative timestamps fresh, and a slow fallback refetch if the socket is down.
  useEffect(() => {
    const clock = window.setInterval(() => setNow(Date.now()), CLOCK_MS);
    const fallback = window.setInterval(
      () => load().catch(() => undefined),
      FALLBACK_POLL_MS,
    );
    return () => {
      window.clearInterval(clock);
      window.clearInterval(fallback);
    };
  }, [load]);

  const processing = status?.enrichment.processing ?? 0;
  const pending = status?.enrichment.pending ?? 0;
  const failed = status?.enrichment.failed ?? 0;
  // "Running" means there's enrichment work in flight OR queued — a steady signal through a
  // batch, unlike the raw 'processing' count which flips 0↔1 per node and reads idle in the
  // gaps. `processing` alone drives the finickiness the LED showed.
  const inFlight = processing + pending;

  // Light the LED immediately when work appears; on drain, hold it for a short linger so the
  // brief 0-dips between consecutive nodes don't make it blink.
  useEffect(() => {
    if (inFlight > 0) {
      setRunning(true);
      return;
    }
    const t = window.setTimeout(() => setRunning(false), RUNNING_LINGER_MS);
    return () => window.clearTimeout(t);
  }, [inFlight]);

  // Sources that emitted an event within the active window — from the accumulated map, so
  // filtering the table doesn't drop other sources' "active" state.
  const activeSources = useMemo(() => {
    const s = new Set<string>();
    for (const [name, at] of sourceSeen) {
      if (at && now - new Date(at).getTime() <= ACTIVE_WINDOW_MS) s.add(name);
    }
    return s;
  }, [sourceSeen, now]);

  const knownSources = useMemo(() => [...sourceSeen.keys()].sort(), [sourceSeen]);

  const lastSyncBySource = useMemo(() => {
    const m = new Map<string, string>();
    for (const s of status?.sources ?? []) m.set(s.source, s.last_synced_at);
    return m;
  }, [status]);

  // The funnel is "open" when the feed is narrowed to failures.
  const failuresOnly = eventFilter === FAILED_EVENT;

  // Apply the time funnel client-side over the fetched log.
  const visibleEvents = useMemo(() => {
    const span = WINDOW_MS[timeWindow];
    if (span == null) return events;
    return events.filter((e) => now - new Date(e.at).getTime() <= span);
  }, [events, timeWindow, now]);

  const eventCounts = useMemo(() => countByEvent(visibleEvents), [visibleEvents]);
  const failureGroups = useMemo(() => groupFailures(visibleEvents), [visibleEvents]);
  const nodeGroups = useMemo(
    () => (groupMode ? groupByNode(visibleEvents) : []),
    [groupMode, visibleEvents],
  );

  // Newest loaded event id per node — the watermark we compare against to tell when a
  // re-queued node has produced a fresh outcome (and so should stop reading "re-queued").
  const latestEventByNode = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of events) {
      const cur = m.get(e.node_id);
      if (cur === undefined || e.id > cur) m.set(e.node_id, e.id);
    }
    return m;
  }, [events]);
  latestByNodeRef.current = latestEventByNode;

  // Nodes still showing as "re-queued": marked, and no newer event has arrived since.
  const requeuedActive = useMemo(() => {
    const s = new Set<string>();
    for (const [id, watermark] of requeued) {
      if (latestEventByNode.get(id) === watermark) s.add(id);
    }
    return s;
  }, [requeued, latestEventByNode]);

  // The failure breakdown minus what's already been re-queued — drives the rollup chips and
  // the retry buttons, so a re-queued failure drops out of the "to-do" pile immediately.
  const activeGroups = useMemo(
    () =>
      failureGroups
        .map((g) => ({ ...g, nodeIds: g.nodeIds.filter((id) => !requeuedActive.has(id)) }))
        .filter((g) => g.nodeIds.length > 0),
    [failureGroups, requeuedActive],
  );
  const retryableIds = useMemo(
    () => activeGroups.filter((g) => g.retryable).flatMap((g) => g.nodeIds),
    [activeGroups],
  );
  const terminalIds = useMemo(
    () => activeGroups.filter((g) => !g.retryable).flatMap((g) => g.nodeIds),
    [activeGroups],
  );

  const toggleExpand = (key: string) =>
    setExpanded((s) => {
      const next = new Set(s);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });

  if (!loaded) {
    return (
      <div className="ingest">
        <div className="guard">
          <div className="spinner" />
          <div className="guard-text">Loading ingestion activity…</div>
        </div>
      </div>
    );
  }

  const colSpan = groupMode ? 6 : 5;
  // This tenant's kb-agent liveness. Never-seen (null) and a pre-tenancy backend (field
  // absent) both render nothing.
  const agentSeen = agentLastSeenLabel(status?.agent_last_seen_at ?? null);

  return (
    <div className="ingest">
      <aside className="ingest-rail">
        <div className="ingest-running">
          <div className="ingest-running-head">
            <span className={`live-dot${running ? " on" : ""}`} />
            {running ? "Ingestion running" : "Idle"}
          </div>
          <div className="ingest-running-sub">
            {processing > 0
              ? `Enriching ${processing} node${processing === 1 ? "" : "s"} now`
              : pending > 0
                ? `${pending} queued for enrichment`
                : "No enrichment in flight"}
          </div>
        </div>

        <div className="rail-section-title">Enrichment</div>
        <div className="stat-grid">
          {ENRICHMENT_STAGES.map((s) => {
            const cls = `stat-card tone-${s.tone}${
              s.key === "processing" && processing > 0 ? " pulsing" : ""
            }${s.key === "failed" && failuresOnly ? " selected" : ""}`;
            const body = (
              <>
                <span className="stat-num">{status?.enrichment[s.key] ?? 0}</span>
                <span className="stat-label">{s.label}</span>
              </>
            );
            // The Failed counter doubles as the failures funnel toggle.
            if (s.key === "failed") {
              return (
                <button
                  key={s.key}
                  className={cls}
                  onClick={() => setFailuresParam(eventFilter === FAILED_EVENT ? "" : "1")}
                  title="Show only enrichment failures and their reasons"
                >
                  {body}
                </button>
              );
            }
            return (
              <div key={s.key} className={cls}>
                {body}
              </div>
            );
          })}
        </div>

        <div className="rail-section-title">Sources</div>
        <div className="source-list">
          {knownSources.length === 0 ? (
            <div className="rail-empty">No sources yet.</div>
          ) : (
            knownSources.map((name) => {
              const active = activeSources.has(name);
              const lastSync = lastSyncBySource.get(name);
              const lastEvent = sourceSeen.get(name);
              return (
                <button
                  key={name}
                  className={`source-row${sourceFilter === name ? " selected" : ""}`}
                  onClick={() => setSrcParam(sourceFilter === name ? "" : name)}
                  title="Filter activity by this source"
                >
                  <span className={`live-dot${active ? " on" : ""}`} />
                  <span className="source-name">{name}</span>
                  <span className="source-meta">
                    {active
                      ? "active"
                      : lastSync
                        ? timeAgo(lastSync, now)
                        : lastEvent
                          ? timeAgo(lastEvent, now)
                          : "—"}
                  </span>
                </button>
              );
            })
          )}
        </div>
      </aside>

      <main className="ingest-main">
        <div className="ingest-main-head">
          <div className="ingest-title">
            <ActivityIcon size={17} />
            Activity
            {sourceFilter && (
              <span className="filter-chip">
                {sourceFilter}
                <button
                  className="filter-clear"
                  aria-label="Clear source filter"
                  onClick={() => setSrcParam("")}
                >
                  ×
                </button>
              </span>
            )}
          </div>

          <div className="ingest-tools">
            <button
              className={`funnel-btn${failuresOnly ? " active" : ""}`}
              aria-pressed={failuresOnly}
              onClick={() => setFailuresParam(eventFilter === FAILED_EVENT ? "" : "1")}
              title="Funnel the feed to failed ingestion attempts"
            >
              <FunnelIcon size={14} />
              Failures
              {failed > 0 && <span className="funnel-count">{failed}</span>}
            </button>

            <button
              className={`funnel-btn${groupMode ? " active" : ""}`}
              aria-pressed={groupMode}
              onClick={() => setGroupParam(groupMode ? "" : "1")}
              title="Group events by node (lifecycle view)"
            >
              <LayersIcon size={14} />
              By node
            </button>

            {failuresOnly && (
              <select
                className="window-select"
                value={timeWindow}
                aria-label="Limit failures shown by time"
                onChange={(e) => setTimeWindow(e.target.value as TimeWindow)}
              >
                {(Object.keys(WINDOW_LABELS) as TimeWindow[]).map((w) => (
                  <option key={w} value={w}>
                    {WINDOW_LABELS[w]}
                  </option>
                ))}
              </select>
            )}
          </div>
        </div>

        <div className="ingest-rollup">
          <div className="roll-counts">
            <span className="roll-total">{visibleEvents.length} in view</span>
            {eventCounts.map((c) => (
              <span key={c.event} className="roll-chip">
                <span
                  className="roll-dot"
                  style={{ background: EVENT_COLORS[c.event] ?? "var(--text-faint)" }}
                />
                {c.count} {c.label}
              </span>
            ))}
          </div>

          {status && (status.pauses ?? []).length > 0 && (
            <div className="pause-banner">
              {status.pauses.map((p) => (
                <span key={p.provider}>
                  {p.provider} paused ·{" "}
                  {p.reason} · resumes{" "}
                  {new Date(p.until).toLocaleString([], {
                    hour: "numeric", minute: "2-digit",
                  })}{" "}
                  — queued work waits and restarts automatically
                </span>
              ))}
            </div>
          )}

          {agentSeen && <div className="pause-banner">{agentSeen}</div>}

          {status && (status.stuck ?? []).length > 0 && (
            <div className="stuck-banner" data-testid="stuck-banner">
              <span className="stuck-label">
                Stuck · {status.stuck.length} record{status.stuck.length === 1 ? "" : "s"} the
                engine could not store
              </span>
              {status.stuck.map((r) => (
                <span key={stuckKey(r)} className="stuck-row">
                  <span className="stuck-source">{r.source}</span>
                  <span className="stuck-item" title={r.path ?? r.external_id}>
                    {r.path ? titleOf(r.path) : r.external_id}
                  </span>
                  <span className="stuck-error" title={r.error ?? undefined}>
                    {r.error ?? "unknown error"}
                  </span>
                  <span className="stuck-since">since {timeAgo(r.since, now)}</span>
                  <button
                    className="btn ghost stuck-dismiss"
                    disabled={dismissing.has(stuckKey(r))}
                    onClick={() => runDismiss(r)}
                    aria-label={`Dismiss ${r.source} ${r.external_id}`}
                    title="Close this record. A pull record that still fails reappears on the next sync."
                  >
                    {dismissing.has(stuckKey(r)) ? "Dismissing…" : "Dismiss"}
                  </button>
                </span>
              ))}
            </div>
          )}

          {(activeGroups.length > 0 || requeuedActive.size > 0) && (
            <div className="roll-failures">
              <span className="roll-fail-label">Failures</span>
              {activeGroups.map((g) => (
                <span
                  key={g.key}
                  className={`fail-chip ${g.retryable ? "retryable" : "terminal"}`}
                  title={
                    g.retryable
                      ? "Transient — re-queuing can recover these"
                      : "Structural — needs a fix before it can succeed"
                  }
                >
                  {g.label} ×{g.nodeIds.length} · {g.retryable ? "retryable" : "needs fix"}
                </span>
              ))}
              {requeuedActive.size > 0 && (
                <span
                  className="fail-chip requeued"
                  title="Re-queued for another enrichment pass — awaiting the worker"
                >
                  ↻ {requeuedActive.size} re-queued
                </span>
              )}
              <span className="roll-actions">
                {retryableIds.length > 0 && (
                  <button
                    className="btn danger retry-failed"
                    disabled={retrying}
                    onClick={() => runRetry({ nodeIds: retryableIds }, retryableIds)}
                    title="Re-queue only the transient failures (skips ones that need a fix)"
                  >
                    {retrying ? "Re-queuing…" : `Retry ${retryableIds.length} retryable`}
                  </button>
                )}
                {terminalIds.length > 0 && (
                  <button
                    className="btn ghost retry-all"
                    disabled={retrying}
                    onClick={() =>
                      runRetry({ source: sourceFilter }, [...retryableIds, ...terminalIds])
                    }
                    title="Re-queue every failure, including ones that need a fix (they'll likely fail again)"
                  >
                    Retry all anyway
                  </button>
                )}
              </span>
            </div>
          )}

          {retryNote && <span className="roll-note">{retryNote}</span>}
        </div>

        <div className="ingest-table-wrap">
          {visibleEvents.length === 0 ? (
            <div className="rail-empty">
              {events.length === 0
                ? "No ingestion events recorded yet."
                : "No events match the current filter."}
            </div>
          ) : (
            <table className="ingest-table enriched">
              <thead>
                <tr>
                  <th scope="col" className="col-status">Status</th>
                  <th scope="col" className="col-item">Item</th>
                  <th scope="col" className="col-source">Source</th>
                  {groupMode && <th scope="col" className="col-life">Lifecycle</th>}
                  <th scope="col" className="col-when">When</th>
                  <th scope="col" className="col-exp" aria-label="Expand" />
                </tr>
              </thead>
              <tbody>
                {groupMode
                  ? nodeGroups.map((g) => (
                      <NodeRow
                        key={g.nodeId}
                        group={g}
                        now={now}
                        open={expanded.has(g.nodeId)}
                        requeued={requeuedActive.has(g.nodeId)}
                        colSpan={colSpan}
                        onToggle={() => toggleExpand(g.nodeId)}
                      />
                    ))
                  : visibleEvents.map((e) => (
                      <LogRow
                        key={e.id}
                        event={e}
                        now={now}
                        open={expanded.has(String(e.id))}
                        requeued={requeuedActive.has(e.node_id)}
                        colSpan={colSpan}
                        onToggle={() => toggleExpand(String(e.id))}
                      />
                    ))}
              </tbody>
            </table>
          )}
        </div>
      </main>
    </div>
  );
}

/** A keyed fragment that may render a main row plus an optional detail row. */
function FragmentRows({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}

type Classification = { retryable: boolean; label: string } | null;

/** Status cell: the event badge plus an optional failure / re-queued tag. */
function StatusCell({
  event,
  classification,
  requeued,
}: {
  event: string;
  classification: Classification;
  requeued: boolean;
}) {
  return (
    <td className="col-status">
      <span className={`event-badge ev-${event}`}>{EVENT_LABELS[event] ?? event}</span>
      {classification &&
        (requeued ? (
          <span className="fail-tag requeued">↻ Re-queued</span>
        ) : (
          <span className={`fail-tag ${classification.retryable ? "retryable" : "terminal"}`}>
            {classification.label}
          </span>
        ))}
    </td>
  );
}

/** Item cell: humanized title, machine path, and an optional error line. */
function ItemCell({ path, error }: { path: string | null; error: string | null }) {
  return (
    <td className="col-item">
      <span className="item-title">{titleOf(path)}</span>
      <span className="item-path">{path ?? "—"}</span>
      {error && <span className="item-error">{error}</span>}
    </td>
  );
}

/** "When" cell: relative time, with the absolute time on hover. */
function WhenCell({ at, now }: { at: string; now: number }) {
  return (
    <td className="col-when cell-time" title={`${clockTime(at)} · ${at}`}>
      {timeAgo(at, now)}
    </td>
  );
}

/** Keyboard-operable expand toggle cell (stops propagation so it doesn't double-toggle the row). */
function ExpandCell({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <td className="col-exp">
      <button
        type="button"
        className="exp-btn"
        aria-expanded={open}
        aria-label={open ? "Collapse details" : "Expand details"}
        onClick={(ev) => {
          ev.stopPropagation();
          onToggle();
        }}
      >
        <ChevronIcon size={13} className={`exp-chev${open ? " open" : ""}`} />
      </button>
    </td>
  );
}

type RowProps = {
  now: number;
  open: boolean;
  /** Raw "this node was re-queued" flag; effective state is AND-ed with the row's failure. */
  requeued: boolean;
  colSpan: number;
  onToggle: () => void;
};

/** One row of the flat event log, with an expandable raw-id detail row. */
function LogRow({
  event: e,
  now,
  open,
  requeued,
  colSpan,
  onToggle,
}: RowProps & { event: IngestEvent }) {
  const cls = classifyError(e);
  const err = errorText(e);
  const isRequeued = cls != null && requeued;
  return (
    <FragmentRows>
      <tr
        className={`row-main${cls ? (isRequeued ? " is-requeued" : " is-failed") : ""}`}
        onClick={onToggle}
      >
        <StatusCell event={e.event} classification={cls} requeued={isRequeued} />
        <ItemCell path={e.path} error={err} />
        <td className="col-source">
          <span className="src-chip">{e.source}</span>
        </td>
        <WhenCell at={e.at} now={now} />
        <ExpandCell open={open} onToggle={onToggle} />
      </tr>
      {open && (
        <tr className="row-detail">
          <td colSpan={colSpan}>
            <dl className="kv">
              <div>
                <dt>Node</dt>
                <dd className="mono">{e.node_id}</dd>
              </div>
              <div>
                <dt>External ID</dt>
                <dd className="mono">{e.external_id ?? "—"}</dd>
              </div>
              <div>
                <dt>Hash</dt>
                <dd className="mono">{e.content_hash ?? "—"}</dd>
              </div>
              <div>
                <dt>Time</dt>
                <dd>{clockTime(e.at)}</dd>
              </div>
              {err && (
                <div className="kv-wide">
                  <dt>Error</dt>
                  <dd className="err">{err}</dd>
                </div>
              )}
            </dl>
          </td>
        </tr>
      )}
    </FragmentRows>
  );
}

/** One row per node (lifecycle view), with an expandable event-history detail row. */
function NodeRow({
  group: g,
  now,
  open,
  requeued,
  colSpan,
  onToggle,
}: RowProps & { group: ReturnType<typeof groupByNode>[number] }) {
  // g.failure (from groupByNode) is only set for FAILED_EVENT (enrichment_failed); an
  // ingest_failed latest event carries a reason too (a record the engine refused to
  // store) but isn't a "failure classification" -- retry can't fix it. Surface its
  // reason text the same way without touching FAILED_EVENT/groupFailures/the retry pile.
  const err = g.failure || g.latest.event === "ingest_failed" ? errorText(g.latest) : null;
  const isRequeued = g.failure != null && requeued;
  const providers = providersOf(g);
  const shorts = shortProviders(providers);
  return (
    <FragmentRows>
      <tr
        className={`row-main${g.failure ? (isRequeued ? " is-requeued" : " is-failed") : ""}`}
        onClick={onToggle}
      >
        <StatusCell
          event={g.latest.event}
          classification={g.failure ?? null}
          requeued={isRequeued}
        />
        <ItemCell path={g.path} error={err} />
        <td className="col-source">
          <span className="src-chip">{g.source}</span>
          {shorts.length > 0 && (
            <span className="prov-chip" title={providers.join(", ")}>
              {shorts.join(" · ")}
            </span>
          )}
        </td>
        <td className="col-life">
          <span className="lifecycle">
            {[...g.events]
              .reverse()
              .slice(-10)
              .map((ev) => (
                <span
                  key={ev.id}
                  className="life-dot"
                  style={{ background: EVENT_COLORS[ev.event] ?? "var(--text-faint)" }}
                  title={`${EVENT_LABELS[ev.event] ?? ev.event} · ${timeAgo(ev.at, now)}`}
                />
              ))}
          </span>
        </td>
        <WhenCell at={g.latest.at} now={now} />
        <ExpandCell open={open} onToggle={onToggle} />
      </tr>
      {open && (
        <tr className="row-detail">
          <td colSpan={colSpan}>
            <ul className="history">
              {g.events.map((ev) => (
                <li key={ev.id}>
                  <span className={`event-badge ev-${ev.event}`}>
                    {EVENT_LABELS[ev.event] ?? ev.event}
                  </span>
                  <span className="hist-time" title={ev.at}>
                    {timeAgo(ev.at, now)}
                  </span>
                  {errorText(ev) && <span className="hist-error">{errorText(ev)}</span>}
                </li>
              ))}
            </ul>
            <dl className="kv">
              <div>
                <dt>Node</dt>
                <dd className="mono">{g.nodeId}</dd>
              </div>
              {providers.length > 0 && (
                <div>
                  <dt>Models</dt>
                  <dd className="mono">{providers.join(", ")}</dd>
                </div>
              )}
            </dl>
          </td>
        </tr>
      )}
    </FragmentRows>
  );
}
