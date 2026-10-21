import { useEffect, useState } from "react";
import { fetchDocKindMemories, fetchDocKinds } from "../api/client";
import { useChangeFeed } from "../hooks/useChangeFeed";
import type { DocKindRow, NodeMeta } from "../types";

/** Aggregate view of the emergent document-kind facet: kinds ranked by support, an
 * established/all toggle for the long tail, click-through to each kind's memories. */
export function DocKindsPanel() {
  const [kinds, setKinds] = useState<DocKindRow[] | null>(null);
  const [showTail, setShowTail] = useState(false);
  const [openKind, setOpenKind] = useState<string | null>(null);
  const [memories, setMemories] = useState<NodeMeta[] | null>(null);

  useEffect(() => {
    let live = true;
    fetchDocKinds(showTail).then((d) => {
      if (live) setKinds(d.kinds);
    });
    return () => {
      live = false;
    };
  }, [showTail]);
  useChangeFeed((tables) => {
    if (tables.has("nodes")) fetchDocKinds(showTail).then((d) => setKinds(d.kinds));
  });

  useEffect(() => {
    if (openKind === null) {
      setMemories(null);
      return;
    }
    let live = true;
    setMemories(null);
    fetchDocKindMemories(openKind).then((d) => {
      if (live) setMemories(d.memories);
    });
    return () => {
      live = false;
    };
  }, [openKind]);

  return (
    <div className="dockinds-page">
      <header className="dockinds-head">
        <h2>Document kinds</h2>
        <div className="prov-type-filter" role="group" aria-label="Kind visibility">
          <button
            aria-pressed={!showTail}
            onClick={() => setShowTail(false)}
          >
            Established
          </button>
          <button aria-pressed={showTail} onClick={() => setShowTail(true)}>
            All
          </button>
        </div>
      </header>
      {kinds === null ? (
        <p className="dockinds-empty">Loading…</p>
      ) : kinds.length === 0 ? (
        <p className="dockinds-empty">
          No kinds yet — they emerge as enrichment classifies memories.
        </p>
      ) : (
        <ul className="dockinds-list">
          {kinds.map((k) => (
            <li key={k.kind} className="dockinds-row">
              <button
                className="link-btn"
                aria-expanded={openKind === k.kind}
                onClick={() => setOpenKind(openKind === k.kind ? null : k.kind)}
              >
                <span className="badge dockind">
                  <span className="badge-dot" />
                  {k.kind}
                </span>
              </button>
              <span className="dockinds-count">{k.count} memories</span>
              {!k.established ? (
                <span className="concept-kind">long tail</span>
              ) : null}
              {openKind === k.kind ? (
                memories === null ? (
                  <p className="dockinds-empty">Loading…</p>
                ) : (
                  <ul className="dockinds-memories">
                    {memories.map((m) => (
                      <li key={m.id}>
                        <span className="dockinds-mem-title">
                          {m.title ?? m.slug}
                        </span>
                        <span className="dockinds-mem-path">{m.path}</span>
                      </li>
                    ))}
                  </ul>
                )
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
