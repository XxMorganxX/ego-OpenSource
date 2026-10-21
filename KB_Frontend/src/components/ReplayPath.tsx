import { useState } from "react";

import type { StageId } from "../lib/replayStages";
import type { PathStep } from "../lib/replayTrace";
import type { ReplayLabel } from "../types";

const MAX_MATCHES = 8;

/** One pinned memory's standing at every stage; the step for the stage on screen is lit. */
export function ReplayPath({ title, steps, current, deleted, onOpen, onUnpin }: {
  title: string;
  steps: PathStep[];
  current: StageId;
  deleted: boolean;
  onOpen: () => void;
  onUnpin: () => void;
}) {
  return (
    <section className="replay-path" role="region" aria-label="memory path">
      <div className="replay-path-head">
        <strong className={deleted ? "trace-deleted" : ""}>{title}</strong>
        <button className="replay-btn" onClick={onOpen} disabled={deleted}>open memory</button>
        <button className="icon-btn" aria-label="unpin" onClick={onUnpin}>×</button>
      </div>
      <ol>
        {steps.map((s) => (
          <li key={s.title} className={`step-${s.status}${s.stage === current ? " current" : ""}`}>
            <span className="replay-path-stage">{s.title}</span>
            <span>{s.text}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

/**
 * Find a memory among the replay's candidates by title or path. A miss is itself an answer:
 * no arm of the search surfaced that memory at all.
 */
export function ReplayFind({ candidates, labels, missText, onPick }: {
  candidates: string[];
  labels: Record<string, ReplayLabel>;
  /** The sentence shown when nothing matches; receives the typed text. */
  missText: (q: string) => string;
  onPick: (id: string) => void;
}) {
  const [q, setQ] = useState("");
  const needle = q.trim().toLowerCase();
  const matches = needle
    ? candidates.filter((id) => {
        const l = labels[id];
        return (l?.title ?? id).toLowerCase().includes(needle) || (l?.path ?? "").toLowerCase().includes(needle);
      })
    : [];
  return (
    <div className="replay-find">
      <input type="search" aria-label="find a memory" placeholder="find a memory…" value={q}
             onChange={(e) => setQ(e.target.value)} />
      {needle && (matches.length === 0 ? (
        <div className="replay-find-miss">{missText(q.trim())}</div>
      ) : (
        <ul>
          {matches.slice(0, MAX_MATCHES).map((id) => (
            <li key={id}>
              <button onClick={() => { onPick(id); setQ(""); }}>{labels[id]?.title ?? id}</button>
            </li>
          ))}
          {matches.length > MAX_MATCHES && <li className="replay-find-more">{`+${matches.length - MAX_MATCHES} more`}</li>}
        </ul>
      ))}
    </div>
  );
}
