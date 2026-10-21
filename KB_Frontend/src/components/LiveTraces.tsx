import { Fragment, useCallback, useEffect, useRef, useState } from "react";

import {
  fetchTraceDetail, fetchTraces, fetchTraceSummary, fetchTurnDetail, fetchTurns,
  postTraceJudgment, postTurnJudgment,
} from "../api/client";
import { useChangeFeed } from "../hooks/useChangeFeed";
import { useUrlParam } from "../hooks/useUrlParam";
import type { Trace, TraceDetail, TraceJudgment, TraceSummary, Turn, TurnDetail } from "../types";
import { ReplayOverlay } from "./ReplayOverlay";
import { Dt, type DefTerm } from "./defs";

const TOOLS = ["search_memories", "search_graph", "find_entity", "expand",
  "entity_activity", "get_memory",
  // plain read tools -- traced for payload cost (result_bytes/result_count)
  // only; they never carry confidence, strategy, or a results envelope
  "get_entity", "concept", "list_concepts", "list_doc_kinds", "list_memories",
  "list_merge_suggestions",
  // skill registry reads -- traced for usage on the Skills page
  "search_skills", "get_skill", "get_skill_file"];
const CONFS = ["strong", "weak", "keyword", "scoped", "none"];
const REPLAYABLE = new Set(["search_memories", "search_graph"]);

// Grouped-by-request is the default; "false" in storage turns it off. Per-viewer
// convenience only: storage can throw (private mode, blocked site data), and a
// failed read or write just means the default applies.
const GROUP_KEY = "kb.retrieval.groupByRequest";
function readGrouped(): boolean {
  try {
    return window.localStorage.getItem(GROUP_KEY) !== "false";
  } catch {
    return true;
  }
}
function writeGrouped(v: boolean): void {
  try {
    window.localStorage.setItem(GROUP_KEY, String(v));
  } catch {
    /* per-viewer convenience only */
  }
}

/** "40s", "3m 20s", "1h 5m" -- a request's wall-clock span; "—" if either end is unparseable. */
function fmtSpan(startIso: string, endIso: string): string {
  const secs = (new Date(endIso).getTime() - new Date(startIso).getTime()) / 1000;
  if (!Number.isFinite(secs)) return "—";
  const s = Math.max(0, Math.round(secs));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

const TOOL_SHORT: Record<string, string> = {
  search_memories: "search", search_graph: "graph", get_memory: "get",
  find_entity: "find", expand: "expand", entity_activity: "activity",
};

/** "4 search · 1 get" -- the request's calls by tool, busiest first. */
function toolMix(tools: Record<string, number>): string {
  return Object.entries(tools)
    .sort((a, b) => b[1] - a[1])
    .map(([t, n]) => `${n} ${TOOL_SHORT[t] ?? t}`)
    .join(" · ");
}

const pct = (v: number | null) => (v === null ? "—" : `${Math.round(v * 100)}%`);
const ms = (v: number | null) => (v === null ? "—" : `${Math.round(v)} ms`);
const num = (v: number | null) => (v === null ? "—" : v.toLocaleString());

function Tile({
  label,
  value,
  sub,
  term,
}: {
  label: string;
  value: string;
  sub?: string;
  term?: DefTerm;
}) {
  const labelEl = <span className="retrieval-tile-label">{label}</span>;
  return (
    <div className="retrieval-tile">
      <span className="retrieval-tile-value">{value}</span>
      {term ? <Dt term={term}>{labelEl}</Dt> : labelEl}
      {sub && <span className="retrieval-tile-sub">{sub}</span>}
    </div>
  );
}

const VERDICTS = ["good", "partial", "bad"] as const;
type Verdict = (typeof VERDICTS)[number];

/** Verdict buttons, optional note, and the judgment history -- shared by the
 *  per-call and the per-request drill-downs so the control exists once. */
function JudgmentPanel({ heading, judgments, posting, note, setNote, error, onJudge }: {
  heading: string;
  judgments: TraceJudgment[];
  posting: boolean;
  note: string;
  setNote: (note: string) => void;
  error: string | null;
  onJudge: (verdict: Verdict) => void;
}) {
  return (
    <div className="trace-judgment">
      <h4>{heading}</h4>
      {VERDICTS.map((v) => (
        <button key={v} disabled={posting} onClick={() => onJudge(v)}
                className={`judgment-btn verdict-${v}`}>
          {v}
        </button>
      ))}
      <input value={note} placeholder="note (optional)"
             onChange={(e) => setNote(e.target.value)} />
      {error && <span className="trace-error">{error}</span>}
      {judgments.map((j) => (
        <div key={j.id} className="trace-judgment-row">
          <span className={`retrieval-chip verdict-${j.verdict}`}>{j.verdict}</span>
          <span>{j.judged_by}</span>
          {j.notes && <span>{j.notes}</span>}
          <span>{new Date(j.at).toLocaleString()}</span>
        </div>
      ))}
    </div>
  );
}

function TraceDrilldown({ id, onJudged, replay }: { id: number; onJudged: () => void; replay?: { label: string; open: () => void } }) {
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [note, setNote] = useState("");
  const [posting, setPosting] = useState(false);
  // Two errors, two places: a failed fetch replaces the drill-down, a failed verdict
  // post shows inline in the panel and leaves the detail on screen.
  const [loadError, setLoadError] = useState<string | null>(null);
  const [postError, setPostError] = useState<string | null>(null);

  const load = useCallback(() => {
    fetchTraceDetail(id).then(setDetail).catch(() => setLoadError("failed to load trace"));
  }, [id]);
  useEffect(() => {
    load();
  }, [load]);

  if (loadError) return <div className="linked-empty">{loadError}</div>;
  if (detail === null) return <div className="linked-empty">Loading…</div>;

  const judge = (verdict: Verdict) => {
    setPosting(true);
    setPostError(null);
    postTraceJudgment(id, verdict, note || undefined)
      .then(() => {
        setNote("");
        load();
        onJudged();   // parent's chip + judged-filter reflect the new verdict too
      })
      .catch(() => setPostError("judgment failed"))
      .finally(() => setPosting(false));
  };

  return (
    <div className="trace-drilldown">
      <div className="trace-request">
        {replay && <button className="replay-btn" onClick={replay.open}>{replay.label}</button>}
        <h4>request</h4>
        <pre>{JSON.stringify(detail.args, null, 2)}</pre>
        {detail.trace && (
          <>
            <h4>crawl trace</h4>
            <pre>{JSON.stringify(detail.trace, null, 2)}</pre>
          </>
        )}
        {detail.error && <div className="trace-error">error: {detail.error}</div>}
      </div>
      <div className="trace-results">
        <h4>results ({detail.result_count})</h4>
        {(detail.results ?? []).length === 0 ? (
          <div className="linked-empty">Nothing returned.</div>
        ) : (
          <ol>
            {/* index key: the list is static per fetch, and `rank` restarts per
                expand() group -- `${id}-${rank}` collided across groups. */}
            {(detail.results ?? []).map((r, i) => (
              <li key={i}>
                <span className={r.label === "(deleted)" ? "trace-deleted" : ""}>
                  {r.label ?? r.id}
                </span>
                {r.via && <span className="retrieval-chip">{r.via}</span>}
                {r.similarity != null && <span className="trace-sim">{r.similarity.toFixed(3)}</span>}
              </li>
            ))}
          </ol>
        )}
        <JudgmentPanel heading="verdict" judgments={detail.judgments} posting={posting}
                       note={note} setNote={setNote} error={postError} onJudge={judge} />
      </div>
    </div>
  );
}

const TRACE_HEADER = (
  <tr>
    <th>when</th><th>tool</th><th>strategy</th><th>query / target</th>
    <th>ms</th><th>confidence</th><th>rows</th><th><Dt term="result_tokens">tokens</Dt></th><th>verdict</th><th>replay</th>
  </tr>
);

/** Call rows with their drill-down and replay button -- the flat table and every
 *  expanded request render calls through this one component. */
function TraceRows({ traces, open, setOpen, setReplayId, onJudged }: {
  traces: Trace[];
  open: number | null;
  setOpen: (id: number | null) => void;
  setReplayId: (id: number) => void;
  onJudged: () => void;
}) {
  return (
    <>
      {traces.map((t) => (
        <Fragment key={t.id}>
          <tr className="trace-row"
              onClick={() => setOpen(open === t.id ? null : t.id)}>
            <td>{new Date(t.at).toLocaleString()}</td>
            <td>
              {t.tool}
              {t.error && <span className="trace-error-dot" title={t.error}>●</span>}
            </td>
            <td>{t.strategy ?? "—"}</td>
            <td className="retrieval-query" title={t.query ?? t.target_id ?? ""}>
              {t.query ?? t.target_id ?? "—"}
            </td>
            <td>{t.latency_ms}</td>
            <td>
              <span className={`retrieval-chip conf-${t.confidence_level ?? "unknown"}`}>
                {t.confidence_level ?? "—"}
              </span>
            </td>
            <td>{t.result_count}</td>
            <td>{num(t.result_tokens)}</td>
            <td>
              {t.judgment
                ? <span className={`retrieval-chip verdict-${t.judgment}`}>{t.judgment}</span>
                : "—"}
            </td>
            <td>
              {REPLAYABLE.has(t.tool) && !t.error && (
                <button className="replay-btn"
                        onClick={(e) => { e.stopPropagation(); setReplayId(t.id); }}>
                  {t.has_replay ? "▶ Replay" : "▶ Replay (partial)"}
                </button>
              )}
            </td>
          </tr>
          {open === t.id && (
            <tr className="trace-drilldown-row">
              <td colSpan={10}>
                <TraceDrilldown id={t.id} onJudged={onJudged}
                  replay={REPLAYABLE.has(t.tool) && !t.error
                    ? { label: t.has_replay ? "▶ Replay" : "▶ Replay (partial)",
                        open: () => setReplayId(t.id) }
                    : undefined} />
              </td>
            </tr>
          )}
        </Fragment>
      ))}
    </>
  );
}

/** One request expanded: its verdict control first, then its calls as ordinary trace
 *  rows (each keeps its own drill-down, replay, and per-call verdict). `feedTick`
 *  bumps on every mcp_trace change-feed event so an open request picks up new calls. */
function TurnDrilldown({ id, feedTick, onJudged, setReplayId }: {
  id: number;
  feedTick: number;
  onJudged: () => void;
  setReplayId: (id: number) => void;
}) {
  const [detail, setDetail] = useState<TurnDetail | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [posting, setPosting] = useState(false);
  // As in TraceDrilldown: a failed fetch replaces the drill-down, a failed verdict
  // post shows inline and the call rows stay.
  const [loadError, setLoadError] = useState<string | null>(null);
  const [postError, setPostError] = useState<string | null>(null);

  const load = useCallback(() => {
    fetchTurnDetail(id).then(setDetail).catch(() => setLoadError("failed to load request"));
    // feedTick is a dependency only so a change-feed event re-runs the fetch.
  }, [id, feedTick]);
  useEffect(() => {
    load();
  }, [load]);

  if (loadError) return <div className="linked-empty">{loadError}</div>;
  if (detail === null) return <div className="linked-empty">Loading…</div>;

  const judge = (verdict: Verdict) => {
    setPosting(true);
    setPostError(null);
    postTurnJudgment(id, verdict, note || undefined)
      .then(() => {
        setNote("");
        load();
        onJudged();   // the request row's chip + judged filter reflect the verdict
      })
      .catch(() => setPostError("judgment failed"))
      .finally(() => setPosting(false));
  };

  return (
    <div className="turn-drilldown">
      <JudgmentPanel heading="request verdict" judgments={detail.judgments} posting={posting}
                     note={note} setNote={setNote} error={postError} onJudge={judge} />
      <table className="retrieval-table">
        <thead>{TRACE_HEADER}</thead>
        <tbody>
          <TraceRows traces={detail.traces} open={open} setOpen={setOpen}
                     setReplayId={setReplayId}
                     onJudged={() => { load(); onJudged(); }} />
        </tbody>
      </table>
    </div>
  );
}

/**
 * Live traces: every retrieval-shaped MCP call (searches, entity lookups,
 * expands, follow-up reads), filterable, with per-row drill-down and
 * adjudication. Grouped by request by default -- one row per user request
 * (the calls one agent made for it), expandable to its calls -- with a flat
 * per-call table behind the toggle. Self-contained: owns its fetches and
 * change-feed subscription.
 */
/** `isOwner` is threaded to the replay overlay's DetailPanel (owner-only class curation). */
export function LiveTraces({ isOwner }: { isOwner: boolean }) {
  const [summary, setSummary] = useState<TraceSummary | null>(null);
  const [traces, setTraces] = useState<Trace[] | null>(null);
  const [turns, setTurns] = useState<Turn[] | null>(null);
  const [grouped, setGrouped] = useState<boolean>(readGrouped);
  const [openTurn, setOpenTurn] = useState<number | null>(null);
  const [feedTick, setFeedTick] = useState(0);     // refreshes an expanded request
  const [tool, setTool] = useState("");
  const [conf, setConf] = useState("");
  const [judged, setJudged] = useState("");        // "" | "judged" | "unjudged"
  const [days, setDays] = useState(7);             // summary window (spec: 7d default)
  const [includeEvals, setIncludeEvals] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const [replayId, setReplayId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  // ?turn=<id>: a deep link from elsewhere (the Skills page's recent loads) opens one
  // request above the list, whether or not it is on the current page of requests.
  const [linkedTurn, setLinkedTurn] = useUrlParam<string>("turn", "");
  const linkedId = /^[1-9][0-9]{0,15}$/.test(linkedTurn) ? Number(linkedTurn) : null;

  // The section mounts only once the first load finishes, so scroll from a callback ref,
  // once per linked id.
  const scrolledFor = useRef<number | null>(null);
  const linkedRef = useCallback((el: HTMLElement | null) => {
    if (el && linkedId !== null && scrolledFor.current !== linkedId) {
      scrolledFor.current = linkedId;
      el.scrollIntoView?.({ block: "start" });
    }
  }, [linkedId]);

  const liveRef = useRef(true);
  useEffect(() => {
    liveRef.current = true;
    return () => {
      liveRef.current = false;
    };
  }, []);

  const load = useCallback(() => {
    const filters = {
      tool: tool || undefined,
      confidence: conf || undefined,
      judged: judged ? judged === "judged" : undefined,
      includeEvals,
    };
    if (grouped) {
      fetchTurns(filters).then((d) => {
        if (liveRef.current) setTurns(d.turns);
      }).catch(() => {
        if (liveRef.current) setError("failed to load traces");
      });
    } else {
      fetchTraces(filters).then((d) => {
        if (liveRef.current) setTraces(d.traces);
      }).catch(() => {
        if (liveRef.current) setError("failed to load traces");
      });
    }
    fetchTraceSummary(days, includeEvals).then((s) => {
      if (liveRef.current) setSummary(s);
    }).catch(() => {
      if (liveRef.current) setError("failed to load traces");
    });
  }, [tool, conf, judged, days, includeEvals, grouped]);

  useEffect(() => {
    load();
  }, [load]);

  useChangeFeed((tables) => {
    if (tables.has("mcp_trace")) {
      load();
      setFeedTick((n) => n + 1);   // the open TurnDrilldown refetches its calls too
    }
  });

  // A rejected initial fetch used to leave this section stuck on "Loading…"
  // forever -- surface it instead, the same way TraceDrilldown does for its
  // own fetch.
  if (error) return <div className="linked-empty">{error}</div>;
  if (summary === null || (grouped ? turns === null : traces === null)) {
    return <div className="linked-empty">Loading…</div>;
  }

  return (
    <div className="live-traces">
      <div className="live-traces-header">
        <div className="retrieval-scorecard">
          <Tile label={`calls (${summary.days}d)`} value={summary.calls.toLocaleString()} term="trace_calls" />
          <Tile label="requests" value={summary.turns.toLocaleString()}
                sub={`calls ≤ ${summary.turn_gap_s} s apart`} term="request" />
          <Tile label="calls / request" value={num(summary.calls_per_turn_median)}
                sub={summary.tokens_per_turn_median === null
                  ? "tokens unmeasured"
                  : `median ${summary.tokens_per_turn_median.toLocaleString()} tokens`}
                term="calls_per_request" />
          <Tile label="p50 latency" value={ms(summary.latency_p50_ms)}
                sub={summary.latency_p90_ms === null
                  ? "searches only"
                  : `p90 ${ms(summary.latency_p90_ms)} · searches only`}
                term="latency" />
          <Tile label="weak or none" value={pct(summary.weak_or_none_rate)} term="weak_or_none" />
          <Tile label="follow-up rate" value={pct(summary.followup_rate)}
                sub="requests that opened a result"
                term="followup_rate" />
          <Tile label="errors" value={String(summary.errors)} term="trace_errors" />
          <Tile label="median tokens" value={num(summary.tokens_median)}
                sub={`per call · ${summary.bytes_per_token.toLocaleString()} B/token`}
                term="result_tokens" />
          <Tile label={`tokens (${summary.days}d)`} value={num(summary.tokens_total)}
                sub={`over ${summary.tokens_measured.toLocaleString()} of ${summary.calls.toLocaleString()} calls`}
                term="tokens_total" />
          <Tile label="tokens / result" value={num(summary.tokens_per_result)}
                term="tokens_per_result" />
        </div>

        <div className="trace-filters">
          <label>
            tool{" "}
            <select aria-label="tool" value={tool} onChange={(e) => setTool(e.target.value)}>
              <option value="">all</option>
              {TOOLS.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </label>
          <label>
            confidence{" "}
            <select aria-label="confidence" value={conf} onChange={(e) => setConf(e.target.value)}>
              <option value="">all</option>
              {CONFS.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
          <label>
            judged{" "}
            <select aria-label="judged" value={judged} onChange={(e) => setJudged(e.target.value)}>
              <option value="">all</option>
              <option value="judged">judged</option>
              <option value="unjudged">unjudged</option>
            </select>
          </label>
          <label>
            window{" "}
            <select aria-label="window" value={days}
                    onChange={(e) => setDays(Number(e.target.value))}>
              <option value={7}>7d</option>
              <option value={30}>30d</option>
              <option value={90}>90d</option>
            </select>
          </label>
          <label className="trace-eval-toggle">
            <input type="checkbox" checked={grouped}
                   onChange={(e) => {
                     const v = e.target.checked;
                     setGrouped(v);
                     writeGrouped(v);
                     // Drop the mode being switched TO, so its stale list never shows
                     // for a round-trip; the loading guard covers the fetch.
                     if (v) setTurns(null);
                     else setTraces(null);
                   }} />{" "}
            group by request
          </label>
          <label className="trace-eval-toggle">
            <input type="checkbox" checked={includeEvals}
                   onChange={(e) => setIncludeEvals(e.target.checked)} />{" "}
            include eval traffic
          </label>
        </div>
      </div>

      {linkedId !== null && (
        <section className="linked-turn" ref={linkedRef} aria-label={`Request #${linkedId}`}>
          <div className="linked-turn-head">
            <h4>Request #{linkedId}</h4>
            <button className="link-btn" onClick={() => setLinkedTurn("")}>close</button>
          </div>
          <TurnDrilldown key={linkedId} id={linkedId} feedTick={feedTick} onJudged={load}
                         setReplayId={setReplayId} />
        </section>
      )}

      {summary.tokens_by_tool.length > 0 && (
        <details className="trace-tokens-by-tool">
          <summary><Dt term="tokens_by_tool">tokens by tool</Dt></summary>
          <table className="retrieval-table">
            <thead>
              <tr><th>tool</th><th>calls</th><th>median</th><th>total</th></tr>
            </thead>
            <tbody>
              {summary.tokens_by_tool.map((t) => (
                <tr key={t.tool}>
                  <td>{t.tool}</td>
                  <td>{t.calls.toLocaleString()}</td>
                  <td>{num(t.tokens_median)}</td>
                  <td>{num(t.tokens_total)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

      {grouped ? (
        (turns ?? []).length === 0 ? (
          <div className="linked-empty">No retrieval calls traced yet.</div>
        ) : (
          <table className="retrieval-table">
            <thead>
              <tr>
                <th>when</th><th>span</th><th>calls</th><th>first query</th>
                <th><Dt term="result_tokens">tokens</Dt></th><th>outcome</th><th>verdict</th>
              </tr>
            </thead>
            <tbody>
              {(turns ?? []).map((t) => (
                <Fragment key={t.id}>
                  <tr className="trace-row"
                      onClick={() => setOpenTurn(openTurn === t.id ? null : t.id)}>
                    <td>{new Date(t.started_at).toLocaleString()}</td>
                    <td>{fmtSpan(t.started_at, t.ended_at)}</td>
                    <td>
                      {t.calls}
                      <span className="turn-tools">{toolMix(t.tools)}</span>
                    </td>
                    <td className="retrieval-query" title={t.first_query ?? ""}>
                      {t.first_query ?? "—"}
                    </td>
                    <td>{num(t.result_tokens)}</td>
                    <td>
                      <span className={`retrieval-chip outcome-${t.outcome}`}>{t.outcome}</span>
                      {t.errors > 0 && (
                        <span className="trace-error-dot" title={`${t.errors} call(s) errored`}>●</span>
                      )}
                    </td>
                    <td>
                      {t.judgment
                        ? <span className={`retrieval-chip verdict-${t.judgment}`}>{t.judgment}</span>
                        : "—"}
                    </td>
                  </tr>
                  {openTurn === t.id && (
                    <tr className="trace-drilldown-row">
                      <td colSpan={7}>
                        <TurnDrilldown id={t.id} feedTick={feedTick} onJudged={load}
                                       setReplayId={setReplayId} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )
      ) : (traces ?? []).length === 0 ? (
        <div className="linked-empty">No retrieval calls traced yet.</div>
      ) : (
        <table className="retrieval-table">
          <thead>{TRACE_HEADER}</thead>
          <tbody>
            <TraceRows traces={traces ?? []} open={open} setOpen={setOpen}
                       setReplayId={setReplayId} onJudged={load} />
          </tbody>
        </table>
      )}
      {replayId !== null && <ReplayOverlay isOwner={isOwner} traceId={replayId} onClose={() => setReplayId(null)} />}
    </div>
  );
}
