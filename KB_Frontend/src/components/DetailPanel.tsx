import { useEffect, useState } from "react";

import {
  clearAnalysisExcluded,
  deleteNode,
  fetchNode,
  fetchNodeMemories,
  reanalyzeNode,
  setAnalysisExcluded,
  setSignificance,
  updateNode,
} from "../api/client";
import type { LinkedMemory, NodeDetail } from "../types";
import { ClassCuration } from "./ClassCuration";
import { CloseIcon, NoteIcon, RecrawlIcon, SaveIcon, TrashIcon } from "./icons";

/** Page size for the lazily-fetched "Found in" memory list. */
const MEMORY_PAGE = 25;

const shortDate = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, {
    month: "short", day: "numeric", year: "numeric",
  });

type Props = {
  nodeId: string;
  onClose: () => void;
  onChanged: () => void;
  /** Open another node's detail (e.g. a linked memory). */
  onSelectNode?: (id: string) => void;
  /** When the selected node is an entity class, its name — switches the panel to curation. */
  categoryName?: string;
  /** False for a tenant (or an unknown identity): the class view drops its owner-only
   *  curation controls. Required — callers decide; there is no fail-open default. */
  isOwner: boolean;
};

export function DetailPanel({
  nodeId,
  onClose,
  onChanged,
  onSelectNode,
  categoryName,
  isOwner,
}: Props) {
  const [node, setNode] = useState<NodeDetail | null>(null);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  // Memories this entity was found in — paged (MEMORY_PAGE at a time), only fetched for
  // entity nodes; null while the first page loads. total drives the "Show more" button.
  const [memories, setMemories] = useState<LinkedMemory[] | null>(null);
  const [memTotal, setMemTotal] = useState(0);
  const [memError, setMemError] = useState(false);
  const [memBusy, setMemBusy] = useState(false);
  // Manual re-crawl: in-flight guard + a transient status line.
  const [recrawling, setRecrawling] = useState(false);
  const [recrawlNote, setRecrawlNote] = useState<string | null>(null);
  // Entity-analysis exclusion: whether the going-forward/retract confirm is open, and an in-flight
  // guard shared by exclude/include.
  const [confirmExclude, setConfirmExclude] = useState(false);
  const [excludeBusy, setExcludeBusy] = useState(false);
  const [sig, setSig] = useState("0");
  const [sigBusy, setSigBusy] = useState(false);
  const [sigNote, setSigNote] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setMemories(null);
    setMemError(false);
    fetchNode(nodeId).then((n) => {
      if (!live) return;
      setNode(n);
      setTitle(n.title ?? "");
      setContent(n.content);
      setSig(String(n.importance_modifiers?.manual ?? 0));
      if (n.type === "entity") {
        fetchNodeMemories(nodeId, { limit: MEMORY_PAGE })
          .then((res) => {
            if (live) {
              setMemories(res.memories);
              setMemTotal(res.total);
            }
          })
          .catch(() => {
            if (live) setMemError(true);
          });
      }
    });
    return () => {
      live = false;
    };
  }, [nodeId]);

  // Clear the transient re-crawl status line after a few seconds so it doesn't linger.
  useEffect(() => {
    if (!recrawlNote) return;
    const t = window.setTimeout(() => setRecrawlNote(null), 5000);
    return () => window.clearTimeout(t);
  }, [recrawlNote]);

  // Clear the transient significance status line after a few seconds.
  useEffect(() => {
    if (!sigNote) return;
    const t = window.setTimeout(() => setSigNote(null), 5000);
    return () => window.clearTimeout(t);
  }, [sigNote]);

  if (!node) {
    return (
      <aside className="detail">
        <div className="guard">
          <div className="spinner" />
          <div className="guard-text">Loading…</div>
        </div>
      </aside>
    );
  }

  const save = async () => {
    await updateNode(nodeId, { title, content });
    onChanged();
  };

  // Fetch the next page of "Found in" memories (also the retry path — offset is 0 while
  // the first page hasn't landed).
  const loadMoreMemories = async () => {
    if (memBusy) return;
    setMemBusy(true);
    setMemError(false);
    try {
      const res = await fetchNodeMemories(nodeId, {
        limit: MEMORY_PAGE, offset: memories?.length ?? 0,
      });
      setMemories([...(memories ?? []), ...res.memories]);
      setMemTotal(res.total);
    } catch {
      setMemError(true);
    } finally {
      setMemBusy(false);
    }
  };

  // The paged "Found in" list — rendered in the normal entity view and, for entity
  // classes, as the curation panel's footer (same lazily-fetched data either way).
  const foundInBlock = (
    <div className="field">
      <span className="field-label">
        Found in{memories !== null ? ` · ${memTotal}` : ""}
      </span>
      {memories === null && !memError ? (
        <div className="linked-empty">Loading…</div>
      ) : memories !== null && memTotal === 0 ? (
        <div className="linked-empty">No memories reference this entity yet.</div>
      ) : (
        <>
          {memories !== null && memories.length > 0 && (
            <ul className="linked-memories">
              {memories.map((m) => (
                <li key={m.id}>
                  <button
                    type="button"
                    className="linked-memory"
                    onClick={() => onSelectNode?.(m.id)}
                    title={m.weight != null ? `${m.path} · weight ${m.weight}` : m.path}
                  >
                    <span className="lm-icon">
                      <NoteIcon size={14} />
                    </span>
                    <span className="lm-text">
                      <span className="lm-title">{m.title ?? m.slug}</span>
                      <span className="lm-path">{m.path}</span>
                    </span>
                    <span className="lm-meta">
                      <span className="lm-date">{shortDate(m.event_at ?? m.updated_at)}</span>
                      <span className={`lm-type lm-type-${m.link_type}`}>
                        {m.link_type === "belongs_to" ? "member" : "mention"}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {memError ? (
            <div className="linked-empty">
              Couldn&apos;t load memories.{" "}
              <button type="button" className="linked-more" onClick={loadMoreMemories}>
                Retry
              </button>
            </div>
          ) : memories !== null && memories.length < memTotal ? (
            <button
              type="button"
              className="linked-more"
              onClick={loadMoreMemories}
              disabled={memBusy}
            >
              {memBusy ? "Loading…" : `Show more (${memTotal - memories.length} remaining)`}
            </button>
          ) : null}
        </>
      )}
    </div>
  );

  const applySignificance = async () => {
    setSigBusy(true);
    try {
      const updated = await setSignificance(nodeId, Number(sig));
      setNode(updated);
      setSig(String(updated.importance_modifiers?.manual ?? 0));
      onChanged();
    } catch {
      setSigNote("Failed to apply significance — try again");
    } finally {
      setSigBusy(false);
    }
  };

  // Ask the re-analysis daemon to re-crawl this memory now. The work runs in the background
  // (surfaced via the live change feed), so we only report that the request was accepted.
  const recrawl = async () => {
    setRecrawling(true);
    setRecrawlNote(null);
    try {
      await reanalyzeNode(nodeId);
      setRecrawlNote("Re-crawl requested — re-analyzing in the background…");
    } catch {
      setRecrawlNote("Re-crawl failed — try again");
    } finally {
      setRecrawling(false);
    }
  };

  const exclude = async (retract: boolean) => {
    setExcludeBusy(true);
    try {
      const updated = await setAnalysisExcluded(nodeId, { retract });
      setNode(updated);
      onChanged();
    } finally {
      setExcludeBusy(false);
      setConfirmExclude(false);
    }
  };

  const include = async () => {
    setExcludeBusy(true);
    try {
      const updated = await clearAnalysisExcluded(nodeId);
      setNode(updated);
      onChanged();
    } finally {
      setExcludeBusy(false);
    }
  };

  const isFolder = node.type === "folder";
  const isEntity = node.type === "entity";
  // Retired (dead) memories are frozen and never re-crawled, so they don't get the button.
  const canRecrawl = !isFolder && !isEntity && node.lifecycle !== "dead";
  // The original source file, for file-backed nodes. external_id is the source's identifier:
  // a file path for obsidian (e.g. "Projects/Atlas.md") but an opaque UUID for agent/webhook
  // notes — only show it when it actually looks like a file (has a path sep or extension),
  // so a renamed note still reveals its origin without surfacing a meaningless id.
  const originalFile =
    !isEntity && node.external_id && /[/.]/.test(node.external_id)
      ? node.external_id
      : null;

  const remove = async () => {
    const msg = isFolder
      ? "Delete this folder AND its entire subtree?"
      : isEntity
        ? "Delete this entity?"
        : "Delete this note?";
    if (!window.confirm(msg)) return;
    await deleteNode(nodeId, isFolder);
    onChanged();
    onClose();
  };

  const segments = node.path.split(".");
  const significance = Number.isFinite(node.importance)
    ? node.importance.toFixed(2)
    : "—";

  return (
    <aside className="detail">
      <div className="detail-head">
        <div className="detail-head-main">
          <span
            className={`badge ${isFolder ? "folder" : isEntity ? "entity" : "note"}`}
          >
            <span className="badge-dot" />
            {node.type}
          </span>
          {node.doc_kind ? (
            <span className="badge dockind">
              <span className="badge-dot" />
              {node.doc_kind}
            </span>
          ) : null}
          <p className="breadcrumb">
            {segments.map((seg, i) => (
              <span key={i} className={i === segments.length - 1 ? "leaf" : undefined}>
                {seg}
                {i < segments.length - 1 ? " / " : ""}
              </span>
            ))}
          </p>
        </div>
        <button className="icon-btn" onClick={onClose} aria-label="Close">
          <CloseIcon />
        </button>
      </div>

      {categoryName ? (
        <ClassCuration
          name={categoryName}
          onChanged={onChanged}
          onClose={onClose}
          footer={isEntity ? foundInBlock : null}
          readOnly={!isOwner}
        />
      ) : (
        <>
          <div className="detail-body">
            <div className="significance">
              <div className="significance-head">
                <span className="significance-label">Significance</span>
                <span className="significance-value">{significance}</span>
              </div>
              {node.effective_importance !== undefined && (
                <span className="significance-effective">
                  effective {node.effective_importance.toFixed(2)} ·{" "}
                  {Math.round(node.age_days ?? 0)}d old
                </span>
              )}
              {node.factors && node.factors.length > 0 && (
                <ul className="significance-factors">
                  {node.factors.map((f) => (
                    <li key={f.name}>
                      <span className="sf-name">{f.name}</span>
                      <span className="sf-contrib">
                        {f.contribution >= 0 ? "+" : ""}
                        {f.contribution.toFixed(2)}
                      </span>
                      {f.name === "inherited" &&
                        node.inherited_from &&
                        node.inherited_from.length > 0 && (
                          <span className="sf-from">
                            from{" "}
                            {node.inherited_from
                              .map((s) => `${s.path} ${s.value >= 0 ? "+" : ""}${s.value}`)
                              .join(", ")}
                          </span>
                        )}
                      {f.name === "project_dormancy" &&
                        node.project_dormant_days != null && (
                          <span className="sf-from">
                            project quiet {Math.round(node.project_dormant_days)}d
                          </span>
                        )}
                    </li>
                  ))}
                </ul>
              )}
              {!isEntity && (
                <div className="significance-edit">
                  <span className="field-label">
                    {isFolder
                      ? `Adjust — applies to this folder and everything inside it (${node.descendant_count ?? 0} items)`
                      : "Adjust"}
                  </span>
                  <div className="significance-edit-row">
                    <input
                      type="number"
                      step="0.1"
                      inputMode="decimal"
                      name="significance"
                      aria-label="Significance adjustment"
                      value={sig}
                      onChange={(e) => setSig(e.target.value)}
                    />
                    <button
                      className="btn"
                      disabled={sigBusy || Number.isNaN(Number(sig))}
                      onClick={applySignificance}
                    >
                      {sigBusy ? "Applying…" : "Apply"}
                    </button>
                  </div>
                  {sigNote && (
                    <div className="detail-note" role="status">
                      {sigNote}
                    </div>
                  )}
                </div>
              )}
            </div>

            <label className="field">
              <span className="field-label">Title</span>
              <input
                name="title"
                autoComplete="off"
                value={title}
                placeholder="Untitled"
                onChange={(e) => setTitle(e.target.value)}
              />
            </label>

            <label className="field">
              <span className="field-label">Content</span>
              <textarea
                name="content"
                rows={12}
                value={content}
                placeholder="Write something…"
                onChange={(e) => setContent(e.target.value)}
              />
            </label>

            {isEntity && foundInBlock}

            {node.tags.length > 0 && (
              <div className="field">
                <span className="field-label">Tags</span>
                <div className="tags">
                  {node.tags.map((t) => (
                    <span key={t} className="tag">
                      {t}
                    </span>
                  ))}
                </div>
              </div>
            )}

            <div className="meta-grid">
              {originalFile && (
                <>
                  <span className="meta-key">Original file</span>
                  <span className="meta-val" title={originalFile}>
                    {originalFile}
                  </span>
                </>
              )}
              <span className="meta-key">Source</span>
              <span className="meta-val">{node.source}</span>
              <span className="meta-key">ID</span>
              <span className="meta-val">{node.id}</span>
              {node.uri && (
                <>
                  <span className="meta-key">URI</span>
                  <span className="meta-val">{node.uri}</span>
                </>
              )}
            </div>

            {!isEntity && (
              <div className="field analysis-exclude">
                <span className="field-label">Entity analysis</span>
                {node.excluded_by_path && !node.analysis_excluded ? (
                  <p className="exclude-inherited">
                    Excluded via folder: <code>{node.excluded_by_path}</code>
                  </p>
                ) : node.analysis_excluded ? (
                  <button className="btn" disabled={excludeBusy} onClick={include}>
                    Include in entity analysis
                  </button>
                ) : confirmExclude ? (
                  <div className="exclude-confirm">
                    {isFolder && (
                      <p className="exclude-count">
                        ↳ applies to {node.descendant_doc_count ?? 0} docs
                      </p>
                    )}
                    <button className="btn" disabled={excludeBusy} onClick={() => exclude(false)}>
                      Exclude going forward
                    </button>
                    <button
                      className="btn danger"
                      disabled={excludeBusy}
                      onClick={() => exclude(true)}
                    >
                      Exclude &amp; retract past entities
                    </button>
                    <button
                      className="btn ghost"
                      disabled={excludeBusy}
                      onClick={() => setConfirmExclude(false)}
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <button className="btn" disabled={excludeBusy} onClick={() => setConfirmExclude(true)}>
                    Exclude from entity analysis
                  </button>
                )}
              </div>
            )}
          </div>

          {recrawlNote && (
            <div className="detail-note" role="status">
              {recrawlNote}
            </div>
          )}

          <div className="detail-foot">
            <button className="btn primary" onClick={save}>
              <SaveIcon />
              Save
            </button>
            {canRecrawl && (
              <button
                className="btn"
                onClick={recrawl}
                disabled={recrawling}
                title="Re-run the LLM analysis over this memory now"
              >
                <RecrawlIcon />
                {recrawling ? "Requesting…" : "Re-crawl"}
              </button>
            )}
            <button className="btn danger" onClick={remove}>
              <TrashIcon />
              Delete
            </button>
          </div>
        </>
      )}
    </aside>
  );
}
