import { useEffect, useMemo, useState } from "react";

import { fetchTraceReplay } from "../api/client";
import { useDialog } from "../hooks/useDialog";
import { usePrefersReducedMotion, useReplayPlayer, type Speed } from "../hooks/useReplayPlayer";
import { buildLayout, similarityMap } from "../lib/replayLayout";
import { buildStages, posMap } from "../lib/replayStages";
import { outcomeText, replayOutcome, tracePath } from "../lib/replayTrace";
import type { TraceReplay } from "../types";
import { DetailPanel } from "./DetailPanel";
import { ReplayFusion } from "./ReplayFusion";
import { ReplayGraph } from "./ReplayGraph";
import { ReplayFind, ReplayPath } from "./ReplayPath";
import { ReplayStrip } from "./ReplayStrip";

const SPEEDS: Speed[] = [0.5, 1, 2];
const fmt = (v: number | null | undefined, d = 3) => (v == null ? "—" : v.toFixed(d));

/**
 * Animated replay of one recorded search: the graph is the main
 * panel, the ranked strip sits beside it, the transport bar steps the stages.
 * Everything shown comes from what the search recorded at the time.
 */
export function ReplayOverlay({
  traceId,
  onClose,
  isOwner,
}: {
  traceId: number;
  onClose: () => void;
  /** Threaded to DetailPanel: a tenant gets the class view without curation controls. */
  isOwner: boolean;
}) {
  const [data, setData] = useState<TraceReplay | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  // The memory whose path is traced; clicking a node, row or callout pins it.
  const [pinned, setPinned] = useState<string | null>(null);
  const ref = useDialog<HTMLDivElement>(onClose);
  const reduced = usePrefersReducedMotion();

  useEffect(() => {
    let live = true;
    fetchTraceReplay(traceId)
      .then((d) => { if (live) setData(d); })
      .catch(() => { if (live) setError("failed to load replay"); });
    return () => { live = false; };
  }, [traceId]);

  const layout = useMemo(() => (data ? buildLayout(data.replay) : null), [data]);
  const stages = useMemo(() => (data ? buildStages(data.replay, data.args) : []), [data]);
  const player = useReplayPlayer(stages.map((s) => s.available));

  const facts = useMemo(() => {
    if (!data) return null;
    const r = data.replay;
    const hubLabels: Record<string, string> = {};
    // Hover text for hubs: their graph label can be dropped for space, and a hub has no
    // memory label to fall back on.
    const hubInfo: Record<string, string> = {};
    const provenance = new Map<string, { via: string; hop: number; admitted: boolean }>();
    for (const hop of r.crawl?.hops ?? [])
      for (const o of hop.offered ?? []) {
        for (const p of o.parents) {
          hubLabels[`hub:${p.id}`] = p.label ?? p.id;
          hubInfo[`hub:${p.id}`] ??= `${p.kind} reached in hop ${hop.hop}`;
        }
        if (!provenance.has(o.id))
          provenance.set(o.id, {
            via: o.parents.map((p) => p.label ?? p.id).join(", "), hop: hop.hop, admitted: o.admitted,
          });
      }
    // Plain names on the graph: the entry stage's caption already says how each was reached.
    for (const e of r.crawl?.entry ?? []) {
      hubLabels[`hub:${e.id}`] = e.label ?? e.id;
      hubInfo[`hub:${e.id}`] = `starting point · ${e.via}`;
    }
    const page = (r.crawl?.final ?? r.flat.fused).map((x) => x.id);
    const crawled = (r.crawl?.entry.length ?? 0) > 0;
    return {
      candidates: (layout?.nodes ?? []).filter((n) => n.kind === "memory").map((n) => n.id),
      missText: (q: string) => data.completeness === "partial"
        ? `No returned result matches “${q}” — this trace predates full capture, so only the returned page was recorded.`
        : `No candidate matches “${q}” — ${crawled ? "keyword, meaning and crawl search all" : "keyword and meaning search both"} missed it.`,
      outcome: outcomeText(replayOutcome(r)),
      page: new Set(page.slice(0, r.limit)),
      kw: posMap((r.flat.fts ?? []).map((x) => x.id)),
      mean: posMap((r.flat.knn ?? []).map((x) => x.id)),
      final: posMap(page),
      rrf: new Map((r.crawl?.final ?? r.flat.fused).map((x) => [x.id, x.rrf ?? null] as const)),
      sims: similarityMap(r),
      hubLabels,
      hubInfo,
      provenance,
      listPos: {
        sim: posMap(r.crawl?.lists?.sim ?? []),
        admit: posMap(r.crawl?.lists?.admit ?? []),
        corrob: posMap(r.crawl?.lists?.corrob ?? []),
      },
    };
  }, [data, layout]);

  const stage = stages[player.index];
  const prev = player.index > 0 ? stages[player.index - 1] : null;
  const hoverLabel = hover && data ? data.labels[hover] : undefined;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div ref={ref} className="modal replay-modal" role="dialog" aria-modal="true"
           aria-label="Retrieval replay" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">
            Replay{data ? ` · ${data.tool} · “${data.query ?? ""}”` : ""}
            {data?.completeness === "partial" && <span className="retrieval-chip">partial</span>}
          </span>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>×</button>
        </div>
        {error ? (
          <div className="linked-empty">{error}</div>
        ) : !data || !layout || !facts || !stage ? (
          <div className="linked-empty">Loading…</div>
        ) : (
          <>
            <div className="replay-body">
              <div className="replay-main">
                <ReplayGraph layout={layout} stage={stage} prev={prev} labels={data.labels}
                             hubLabels={facts.hubLabels} page={facts.page} limit={data.replay.limit}
                             motion={!reduced} focus={hover} pinned={pinned}
                             onHover={setHover} onSelect={setPinned} />
                {stage.id === "fusion" && data.replay.crawl?.lists && data.replay.crawl.final && (
                  <ReplayFusion lists={data.replay.crawl.lists}
                                final={data.replay.crawl.final.map((x) => x.id)}
                                limit={data.replay.limit} labels={data.labels} hover={hover}
                                motion={!reduced} onHover={setHover} />
                )}
                {hover && facts.hubInfo[hover] && (
                  <div className="replay-tooltip" role="tooltip">
                    <strong>{facts.hubLabels[hover]}</strong>
                    <div>{facts.hubInfo[hover]}</div>
                  </div>
                )}
                {hoverLabel && hover && (
                  <div className="replay-tooltip" role="tooltip">
                    <strong>{hoverLabel.title}</strong>
                    {hoverLabel.path && <div>{hoverLabel.path}</div>}
                    <div>
                      {facts.kw.has(hover) && `kw#${facts.kw.get(hover)} · `}
                      {facts.mean.has(hover) && `mean#${facts.mean.get(hover)} · `}
                      {facts.final.has(hover) && `final#${facts.final.get(hover)} · `}
                      rrf {fmt(facts.rrf.get(hover))} · sim {fmt(facts.sims.get(hover))}
                    </div>
                    {facts.provenance.has(hover) && (
                      <div>
                        via {facts.provenance.get(hover)!.via} (hop {facts.provenance.get(hover)!.hop})
                        {" · "}{facts.provenance.get(hover)!.admitted ? "admitted" : "rejected"}
                        {(["sim", "admit", "corrob"] as const)
                          .filter((k) => facts.listPos[k].has(hover))
                          .map((k) => ` · ${k}#${facts.listPos[k].get(hover)}`)}
                      </div>
                    )}
                  </div>
                )}
              </div>
              <aside className="replay-side">
                {selected ? (
                  <DetailPanel
                    nodeId={selected}
                    isOwner={isOwner}
                    onClose={() => setSelected(null)}
                    onChanged={() => {}}
                  />
                ) : (
                  <>
                    <ReplayFind candidates={facts.candidates} labels={data.labels}
                                missText={facts.missText} onPick={setPinned} />
                    {pinned && (
                      <ReplayPath title={data.labels[pinned]?.title ?? pinned}
                                  steps={tracePath(data.replay, pinned)} current={stage.id}
                                  deleted={data.labels[pinned]?.deleted ?? false}
                                  onOpen={() => setSelected(pinned)} onUnpin={() => setPinned(null)} />
                    )}
                    {stage.id === "cut" && (
                      <div className="replay-outcome" role="note" aria-label="outcome">{facts.outcome}</div>
                    )}
                    <h4>results</h4>
                    <ReplayStrip rows={stage.strip} labels={data.labels} limitLine={stage.limitLine}
                                 motion={!reduced} pinned={pinned} onSelect={setPinned} onHover={setHover} />
                  </>
                )}
              </aside>
            </div>
            <div className="replay-transport">
              <button aria-label="previous stage" onClick={player.prev}>◀</button>
              {player.playing
                ? <button aria-label="pause" onClick={player.pause}>⏸</button>
                : <button aria-label="play" onClick={player.play}>▶</button>}
              <button aria-label="next stage" onClick={player.next}>▶|</button>
              {SPEEDS.map((s) => (
                <button key={s} aria-pressed={player.speed === s} onClick={() => player.setSpeed(s)}>{s}×</button>
              ))}
              <div className="replay-stagebar">
                {stages.map((s, i) => (
                  <button key={s.key} aria-label={s.title} title={s.caption} disabled={!s.available}
                          className={i === player.index ? "current" : ""} onClick={() => player.goTo(i)} />
                ))}
              </div>
              <span className="replay-caption">
                {`stage ${player.index + 1}/${stages.length}: ${stage.title} — ${stage.caption}`}
              </span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
