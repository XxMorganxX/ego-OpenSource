import { useMemo } from "react";

import { CALLOUT_ROW, CALLOUT_X, placeCallouts } from "../lib/replayCallouts";
import { placeLabels, type LabelRequest } from "../lib/replayLabels";
import { CENTER, R_RING, VIEW_H, VIEW_W, type Layout, type LayoutNode } from "../lib/replayLayout";
import type { Stage } from "../lib/replayStages";
import type { ReplayLabel } from "../types";

type Props = {
  layout: Layout;
  stage: Stage;
  /** The previous stage, so edges new to this one can pulse. */
  prev: Stage | null;
  labels: Record<string, ReplayLabel>;
  /** Hub node id (`hub:<id>`) → display label. */
  hubLabels: Record<string, string>;
  /** Final-page memory ids -- hub labels keep clear of their circles. */
  page: ReadonlySet<string>;
  /** How many of the stage's ranked rows the callout column names. */
  limit: number;
  motion: boolean;
  /** A memory hovered outside the graph (strip, fusion columns) -- ringed here too. */
  focus?: string | null;
  /** The memory whose path is being traced: its node, edges and callout stay marked. */
  pinned?: string | null;
  onHover: (id: string | null) => void;
  onSelect: (id: string) => void;
};

const nodeRadius = (n: LayoutNode) => (n.kind === "query" ? 14 : n.kind === "hub" ? 10 : 6);
/** The fixed "query" caption above the centre node — labels must keep clear of it. */
const QUERY_LABEL = { x1: CENTER.x - 20, y1: CENTER.y - 32, x2: CENTER.x + 20, y2: CENTER.y - 16 };
/** The callout column -- hub labels must not run into it. */
const CALLOUT_COLUMN = { x1: CALLOUT_X - 20, y1: 0, x2: VIEW_W, y2: VIEW_H };

/** One replay stage drawn on the fixed layout; stages only toggle classes, nothing moves. */
export function ReplayGraph({ layout, stage, prev, labels, hubLabels, page, limit, motion, focus = null,
                             pinned = null, onHover, onSelect }: Props) {
  const pos = new Map(layout.nodes.map((n) => [n.id, n]));
  // Hub names are placed once per replay, not per stage, so a label never jumps as later
  // stages reveal more nodes; what cannot fit is dropped (the hover tooltip still names it).
  // Memories are named in the callout column instead, which never collides.
  const placed = useMemo(() => {
    const hubs: LabelRequest[] = layout.nodes
      .filter((n) => n.kind === "hub" && hubLabels[n.id])
      .map((n) => ({ id: n.id, x: n.x, y: n.y, r: nodeRadius(n), text: hubLabels[n.id] }));
    // Labels also keep off the circles that matter -- the query, hubs and final-page rows --
    // so a hub name never runs across a neighbouring hub.
    const circles = layout.nodes
      .filter((n) => n.kind !== "memory" || page.has(n.id))
      .map((n) => { const r = nodeRadius(n) + 1;
                    return { x1: n.x - r, y1: n.y - r, x2: n.x + r, y2: n.y + r }; });
    return placeLabels(hubs, { width: VIEW_W, height: VIEW_H }, [QUERY_LABEL, CALLOUT_COLUMN, ...circles]);
  }, [layout, hubLabels, page]);
  const { callouts, more } = placeCallouts(stage.strip.filter((r) => stage.nodes.has(r.id)), pos,
                                           (id) => labels[id]?.title ?? id, limit);
  const mark = (id: string) => [pinned === id && "pinned", focus === id && "focus"].filter(Boolean).join(" ");
  return (
    <svg className={`replay-graph${motion ? " replay-motion" : ""}`}
         viewBox={`0 0 ${VIEW_W} ${VIEW_H}`} role="img" aria-label="retrieval replay graph">
      <circle className="replay-ring" cx={CENTER.x} cy={CENTER.y} r={R_RING} />
      {layout.edges.map((e) => {
        const a = pos.get(e.from);
        const b = pos.get(e.to);
        if (!a || !b) return null;
        const on = stage.edges.has(e.id);
        const isNew = on && !(prev?.edges.has(e.id) ?? false);
        return (
          <line key={e.id} data-edge={e.id}
                className={`replay-edge edge-${e.kind}${on ? " on" : ""}${isNew ? " new" : ""}${on && pinned === e.to ? " pinned" : ""}`}
                x1={a.x} y1={a.y} x2={b.x} y2={b.y} />
        );
      })}
      {callouts.map((c) => (
        <polyline key={`leader:${c.id}`} data-leader={c.id} className={`replay-leader ${mark(c.id)}`}
                  points={`${c.nodeX},${c.nodeY} ${c.x - 16},${c.y - 4} ${c.x - 4},${c.y - 4}`} />
      ))}
      {layout.nodes.map((n) => {
        const on = stage.nodes.has(n.id);
        const cls = ["replay-node", `node-${n.kind}`, on && "on",
          stage.highlight.has(n.id) && "hl", stage.dimmed.has(n.id) && "dim", mark(n.id)].filter(Boolean).join(" ");
        return (
          <g key={n.id} data-node={n.id} className={cls} transform={`translate(${n.x},${n.y})`}
             onMouseEnter={() => onHover(n.id)} onMouseLeave={() => onHover(null)}
             onClick={() => n.kind === "memory" && onSelect(n.id)}>
            <circle r={nodeRadius(n)} />
            {n.kind === "query" && <text className="replay-label" y={-20} textAnchor="middle">query</text>}
          </g>
        );
      })}
      {[...placed.values()].map((p) => stage.nodes.has(p.id) && (
        <text key={`label:${p.id}`} className={`replay-label${stage.dimmed.has(p.id) ? " dim" : ""}`}
              x={p.x} y={p.y} textAnchor={p.anchor}>{p.text}</text>
      ))}
      {callouts.map((c) => (
        <text key={`callout:${c.id}`} data-callout={c.id} className={`replay-callout ${mark(c.id)}`}
              x={c.x} y={c.y} onMouseEnter={() => onHover(c.id)} onMouseLeave={() => onHover(null)}
              onClick={() => onSelect(c.id)}>{c.text}</text>
      ))}
      {more > 0 && callouts.length > 0 && (
        <text className="replay-callout-more" x={CALLOUT_X}
              y={callouts[callouts.length - 1].y + CALLOUT_ROW}>{`+${more} more`}</text>
      )}
    </svg>
  );
}
