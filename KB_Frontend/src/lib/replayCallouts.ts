import { CENTER, VIEW_H } from "./replayLayout";
import { truncate } from "./replayLabels";

/**
 * The label column on the right of the replay graph: the current stage's top rows, in rank
 * order, each tied to its node by a leader line. A fixed column never collides, so the
 * top-ranked rows -- which cluster tightly on the graph -- are always named.
 */
export const CALLOUT_X = 770;
export const CALLOUT_ROW = 22;
const PAD = 24;
/** Rows that fit between the top and bottom padding. */
const MAX_ROWS = Math.floor((VIEW_H - 2 * PAD) / CALLOUT_ROW) + 1;

export interface Callout {
  id: string;
  text: string;
  /** Text baseline start. */
  x: number;
  y: number;
  /** The node the leader line points at. */
  nodeX: number;
  nodeY: number;
}

export function placeCallouts(rows: { id: string; rank: number }[],
                              pos: Map<string, { x: number; y: number }>,
                              title: (id: string) => string,
                              limit: number): { callouts: Callout[]; more: number } {
  const wanted = rows.slice(0, limit).filter((r) => pos.has(r.id));
  const fit = wanted.length > MAX_ROWS ? MAX_ROWS - 1 : wanted.length; // keep a row for "+N more"
  const top = Math.max(PAD, CENTER.y - ((fit - 1) * CALLOUT_ROW) / 2);
  const callouts = wanted.slice(0, fit).map((r, i) => {
    const n = pos.get(r.id)!;
    return { id: r.id, text: truncate(`${r.rank} ${title(r.id)}`), x: CALLOUT_X,
             y: top + i * CALLOUT_ROW + 4, nodeX: n.x, nodeY: n.y };
  });
  return { callouts, more: wanted.length - fit };
}
