import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";

import { fetchRetrievalEvals } from "../api/client";
import type { EvalsPayload, GridCell } from "../types";
import { LiveTraces } from "./LiveTraces";
import { McpToolsPanel } from "./McpToolsPanel";
import { TrendChart, type TrendSeries } from "./TrendChart";
import { DEFS, Dt, GLOSSARY_ORDER, type DefTerm } from "./defs";

const fmt = (v: number | null | undefined, digits = 3) =>
  v === null || v === undefined ? "—" : v.toFixed(digits);

function Tile({
  label,
  value,
  sub,
  term,
  caption,
}: {
  label: string;
  value: string;
  sub?: string;
  term?: DefTerm;
  caption?: ReactNode;
}) {
  const labelEl = <span className="retrieval-tile-label">{label}</span>;
  return (
    <div className="retrieval-tile">
      <span className="retrieval-tile-value">{value}</span>
      {term ? <Dt term={term}>{labelEl}</Dt> : labelEl}
      {sub && <span className="retrieval-tile-sub">{sub}</span>}
      {caption && <span className="retrieval-tile-caption">{caption}</span>}
    </div>
  );
}

function GridTable({ cells }: { cells: GridCell[] }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <table className="retrieval-table">
      <thead>
        <tr>
          <th>cell</th><th>set</th><th><Dt term="strategy">strategy</Dt></th><th><Dt term="judge">judge</Dt></th>
          <th><Dt term="must_recall">recall@20</Dt></th><th><Dt term="neg_hits">neg hits</Dt></th><th><Dt term="latency">p50</Dt></th><th><Dt term="latency">p90</Dt></th>
        </tr>
      </thead>
      <tbody>
        {cells.map((c) => (
          <Fragment key={c.cell}>
            <tr
              className="retrieval-grid-row"
              onClick={() => setOpen(open === c.cell ? null : c.cell)}
            >
              <td>{c.cell}</td>
              <td>{c.set ?? "—"}</td>
              <td>{c.strategy ?? "—"}</td>
              <td>{c.judge ?? "—"}</td>
              <td>{fmt(c.must_recall)}</td>
              <td>{c.neg_hits ?? "—"}</td>
              <td>{c.latency_p50_ms ?? "—"}</td>
              <td>{c.latency_p90_ms ?? "—"}</td>
            </tr>
            {open === c.cell && (
              <tr className="retrieval-arch-row">
                <td colSpan={8}>
                  {Object.entries(c.by_archetype).map(([a, v]) => (
                    <span key={a} className="retrieval-arch">
                      {a}: {fmt(v)}
                    </span>
                  ))}
                </td>
              </tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </table>
  );
}

/**
 * Retrieval quality: eval history (benchmark grids + reference-run trends, parsed
 * from the committed result files) plus a Live traces section over every
 * retrieval-shaped MCP call (searches, entity lookups, expands, follow-up reads).
 *
 * `isOwner` false (a tenant) hides the owner-only panels — the eval band (backed by the
 * owner-only /api/retrieval/evals, so it is not even fetched) and the MCP tool switches —
 * leaving Live traces. Required: App passes it once /api/me has settled, and an unknown
 * identity is a non-owner — there is no fail-open default.
 */
export function RetrievalPage({ isOwner }: { isOwner: boolean }) {
  const [evals, setEvals] = useState<EvalsPayload | null>(null);
  const [warningsOpen, setWarningsOpen] = useState(true);

  // Guard async resolutions the way ProvenancePage does: drop them after unmount.
  const liveRef = useRef(true);
  useEffect(() => {
    liveRef.current = true;
    return () => {
      liveRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!isOwner) return;
    fetchRetrievalEvals().then((d) => {
      if (liveRef.current) setEvals(d);
    });
  }, [isOwner]);

  if (isOwner && evals === null) {
    return (
      <div className="retrieval-page">
        <div className="linked-empty">Loading…</div>
      </div>
    );
  }

  const latestGrid = evals ? evals.grids[evals.grids.length - 1] ?? null : null;
  const referenceRuns = evals?.reference_runs ?? [];
  const flatRecall =
    latestGrid?.cells.find((c) => c.set === "flat" && c.strategy === "hybrid")
      ?.must_recall ?? null;
  const packageRecall =
    latestGrid?.cells.find((c) => c.set === "package" && c.strategy === "hybrid")
      ?.must_recall ?? null;

  const trend: TrendSeries[] = [
    {
      label: "must_recall@20",
      color: "#4f8cff",
      points: referenceRuns.map((r, i) => ({
        x: i,
        y: r.overall_must_recall,
        label: r.ran_at,
      })),
    },
    {
      label: "mrr",
      color: "#f2a447",
      points: referenceRuns.map((r, i) => ({
        x: i,
        y: r.mrr,
        label: r.ran_at,
      })),
    },
  ];

  return (
    <div className="retrieval-page">
      <div className="retrieval-header">
        <h2 className="prov-title">Retrieval</h2>
        {latestGrid && (
          <span className="prov-count">
            latest grid {latestGrid.date} · {latestGrid.git_heads.join(", ")}
          </span>
        )}
      </div>

      <details className="retrieval-glossary">
        <summary>What these numbers mean</summary>
        <dl>
          {GLOSSARY_ORDER.map((t) => (
            <Fragment key={t}>
              <dt>{DEFS[t].label}</dt>
              <dd>{DEFS[t].long}</dd>
            </Fragment>
          ))}
        </dl>
      </details>

      {evals && evals.warnings.length > 0 && warningsOpen && (
        <div className="retrieval-warnings">
          <span>{evals.warnings.join(" · ")}</span>
          <button onClick={() => setWarningsOpen(false)} aria-label="Dismiss">
            ×
          </button>
        </div>
      )}

      {isOwner && evals && (
        <section className="retrieval-eval-band">
          <div className="eval-band-tiles">
            <Tile label="flat recall@20" value={fmt(flatRecall)} sub="hybrid, latest grid" term="must_recall"
                  caption={<Dt term="flat_set">find-the-note set</Dt>} />
            <Tile label="package recall@20" value={fmt(packageRecall)} sub="hybrid, latest grid" term="must_recall"
                  caption={<Dt term="package_set">set-membership — the hard set (expect lower)</Dt>} />
            <Tile
              label="junk confidence"
              value={evals.off_corpus?.verdict ?? "—"}
              term="junk_confidence"
              caption={evals.off_corpus && evals.off_corpus.worst_best_similarity != null && evals.off_corpus.threshold != null
                ? `worst junk probe ${evals.off_corpus.worst_best_similarity} vs the ${evals.off_corpus.threshold} "weak" bar`
                : undefined}
            />
          </div>
          <div className="eval-band-chart">
            <h3><Dt term="reference_run">Reference-run trend</Dt></h3>
            <TrendChart series={trend} />
          </div>
          <div className="eval-band-grid">
            <h3><Dt term="grid_cell">Benchmark grid</Dt></h3>
            {latestGrid === null ? (
              <div className="linked-empty">No benchmark grids yet.</div>
            ) : (
              <GridTable cells={latestGrid.cells} />
            )}
          </div>
        </section>
      )}

      <section className="retrieval-section">
        <h3>Live traces</h3>
        <LiveTraces isOwner={isOwner} />
      </section>

      {isOwner && (
        <section className="retrieval-section">
          <h3>MCP tools</h3>
          <McpToolsPanel />
        </section>
      )}
    </div>
  );
}
