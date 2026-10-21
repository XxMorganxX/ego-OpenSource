import { truncate } from "../lib/replayLabels";
import type { ReplayLabel } from "../types";

/** The four rank lists the crawl fused, exactly as recorded (`crawl.lists`). */
export interface FusionLists {
  flat: string[];
  sim: string[];
  admit: string[];
  corrob: string[];
}

type Props = {
  lists: FusionLists;
  /** The assembled order (`crawl.final` ids); its first `limit` ids are the returned page. */
  final: string[];
  limit: number;
  labels: Record<string, ReplayLabel>;
  hover: string | null;
  motion: boolean;
  onHover: (id: string | null) => void;
};

const SHOWN = 10;
const TITLE_CHARS = 24;
const COLUMNS = ["flat", "sim", "admit", "corrob", "final"] as const;
type Column = (typeof COLUMNS)[number];

// Why a list can legitimately be empty. admit is gated off whenever no hop's admission was
// query-conditioned judgment -- always, under the deterministic judge (kb_retrieval/crawl/judge.py).
const EMPTY: Record<Column, string> = {
  flat: "empty",
  sim: "empty: no query vector",
  admit: "empty: deterministic judge",
  corrob: "empty: no bridge parents",
  final: "empty",
};

/**
 * Stage 7 of the replay: the four lists reciprocal-rank fusion
 * combined, side by side with the order it produced. Rows that made the returned page are
 * marked in every column, and hovering a row lights the same memory everywhere -- which is
 * how a final rank is read back to the list positions that earned it.
 */
export function ReplayFusion({ lists, final, limit, labels, hover, motion, onHover }: Props) {
  const page = new Set(final.slice(0, limit));
  const ids: Record<Column, string[]> = { ...lists, final };
  return (
    <section className={`replay-fusion${motion ? " fusion-motion" : ""}`} role="region"
             aria-label="final merge lists">
      {COLUMNS.map((col) => {
        const rows = ids[col];
        return (
          <div key={col} className={`fusion-col fusion-col-${col}`}>
            <h5>{col}</h5>
            <ol aria-label={col}>
              {rows.length === 0 && <li className="fusion-empty">{EMPTY[col]}</li>}
              {rows.slice(0, SHOWN).map((id, i) => {
                const cls = ["fusion-row", page.has(id) && "page", hover === id && "hover"]
                  .filter(Boolean).join(" ");
                return (
                  <li key={id} data-id={id} className={cls}
                      style={motion ? { animationDelay: `${i * 50}ms` } : undefined}
                      onMouseEnter={() => onHover(id)} onMouseLeave={() => onHover(null)}>
                    <span className="fusion-rank">{i + 1}</span>
                    <span className={labels[id]?.deleted ? "trace-deleted" : ""}>
                      {truncate(labels[id]?.title ?? id, TITLE_CHARS)}
                    </span>
                  </li>
                );
              })}
              {rows.length > SHOWN && <li className="fusion-more">+{rows.length - SHOWN} more</li>}
            </ol>
          </div>
        );
      })}
    </section>
  );
}
