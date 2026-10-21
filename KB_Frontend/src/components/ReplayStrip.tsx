import { Fragment, useLayoutEffect, useRef } from "react";

import type { StripRow } from "../lib/replayStages";
import type { ReplayLabel } from "../types";

type Props = {
  rows: StripRow[];
  labels: Record<string, ReplayLabel>;
  /** Draw the limit line before this row index; null = none. */
  limitLine: number | null;
  motion: boolean;
  /** The memory whose path is traced -- its row stays marked. */
  pinned?: string | null;
  onSelect: (id: string) => void;
  onHover: (id: string | null) => void;
};

/** The ranked list for the current stage. Rows that move between stages slide (FLIP). */
export function ReplayStrip({ rows, labels, limitLine, motion, pinned = null, onSelect, onHover }: Props) {
  const els = useRef(new Map<string, HTMLLIElement>());
  const tops = useRef(new Map<string, number>());

  useLayoutEffect(() => {
    const next = new Map<string, number>();
    els.current.forEach((el, id) => {
      const top = el.offsetTop;
      next.set(id, top);
      const before = tops.current.get(id);
      if (motion && before !== undefined && before !== top && typeof el.animate === "function") {
        el.animate([{ transform: `translateY(${before - top}px)` }, { transform: "translateY(0)" }],
                   { duration: 400, easing: "ease-out" });
      }
    });
    tops.current = next;
  }, [rows, motion]);

  if (rows.length === 0) return <div className="linked-empty">No ranked list at this stage.</div>;
  return (
    <ol className="replay-strip">
      {rows.map((r, i) => {
        const label = labels[r.id];
        return (
          <Fragment key={r.id}>
            {limitLine !== null && i === limitLine && (
              <li className="replay-limit" aria-hidden="true">{`── limit ${limitLine} ──`}</li>
            )}
            <li ref={(el) => { if (el) els.current.set(r.id, el); else els.current.delete(r.id); }}
                className={`replay-strip-row${r.cut ? " cut" : ""}${pinned === r.id ? " pinned" : ""}`}
                onMouseEnter={() => onHover(r.id)} onMouseLeave={() => onHover(null)}
                onClick={() => onSelect(r.id)}>
              <span className="replay-rank">{r.rank}</span>
              <span className={label?.deleted ? "trace-deleted" : ""}>{label?.title ?? r.id}</span>
              {r.chips.map((c) => <span key={c} className="retrieval-chip">{c}</span>)}
            </li>
          </Fragment>
        );
      })}
    </ol>
  );
}
