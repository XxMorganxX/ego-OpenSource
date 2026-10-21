import { type ReactNode, useEffect, useRef, useState } from "react";

import { fetchAgent, fetchSkill, fetchSkillFile } from "../api/client";
import {
  fmtAgo, fmtBytes, fmtWhen, orderedFiles, SKILL_FILE, skillMiss, splitLines,
} from "../lib/skills";
import type { LibraryKind, ReviewStatus, SkillDetailData, SkillRevisionEntry } from "../types";

/** History rows shown before "Show all", so the rail stays short. */
const HISTORY_PREVIEW = 3;

const reviewLabel = (s: ReviewStatus) => (s === "pending" ? "pending review" : s);

/** "Rejected by owner: “notes”" for a notice's second line; null when the review left
 * neither a reviewer label nor notes. */
function rejectedTrail(e: { reviewer_label: string | null; review_notes: string | null }): string | null {
  if (!e.reviewer_label && !e.review_notes) return null;
  return `Rejected${e.reviewer_label ? ` by ${e.reviewer_label}` : ""}${e.review_notes ? `: “${e.review_notes}”` : ""}`;
}

/**
 * What the viewed revision's state means, for the two states no notice covers. It has to
 * agree with KB_Core skills.py: approving a revision marks every older PENDING revision
 * superseded, so a superseded revision was never itself reviewed -- unless reviewer data
 * says otherwise, in which case the sentence does not claim it.
 */
function reviewMeaning(d: SkillDetailData, entry: SkillRevisionEntry | undefined): string | null {
  const reviewed = Boolean(d.reviewer_label || entry?.reviewed_at);
  if (d.review_status === "superseded") {
    return reviewed ? "Not served: a newer revision was approved."
      : "Retired without review when a newer revision was approved. Never served.";
  }
  if (d.review_status === "approved" && d.revision !== d.approved_revision) {
    return `Approved earlier; agents now get revision ${d.approved_revision ?? "none"}.`;
  }
  return null;
}

function StatusDot({ status }: { status: ReviewStatus }) {
  return <span className={`skill-dot skill-dot-${status}`} aria-hidden="true" />;
}

/**
 * The review facts: which revision is on screen, which one agents get, what waits or was
 * turned down, and who reviewed the viewed one. The header's pill and accent carry the
 * viewed revision's state, so this card does not repeat it as a headline.
 */
function ReviewCard({ d, entry }: { d: SkillDetailData; entry: SkillRevisionEntry | undefined }) {
  // The newest revision, when it is neither on screen nor the one waiting (a rejected
  // newest would otherwise go unmentioned here).
  const newest = d.latest_revision !== d.revision && d.latest_revision !== d.pending_revision
    ? d.history.find((h) => h.revision === d.latest_revision) ?? null : null;
  const reviewedAt = entry?.reviewed_at ?? null;
  const meaning = reviewMeaning(d, entry);
  return (
    <section className="skill-card skill-review" aria-label="Review">
      <h3 className="skill-card-title">Review</h3>
      <dl className="skill-kv">
        <div>
          <dt>Viewing</dt>
          <dd><StatusDot status={d.review_status} />rev {d.revision}{d.revision === d.current_revision ? " · newest" : ""}</dd>
        </div>
        <div>
          <dt>Agents get</dt>
          <dd>{d.approved_revision === null ? "none approved yet"
            : `rev ${d.approved_revision}${d.approved_revision === d.revision ? " · this one" : ""}`}</dd>
        </div>
        {d.pending_revision !== null && (
          <div>
            <dt>Awaiting review</dt>
            <dd>rev {d.pending_revision}{d.pending_revision === d.revision ? " · this one" : ""}</dd>
          </div>
        )}
        {newest && (
          <div>
            <dt>Newest</dt>
            <dd><StatusDot status={newest.review_status} />rev {newest.revision} · {reviewLabel(newest.review_status)}</dd>
          </div>
        )}
        {(d.reviewer_label || reviewedAt) && (
          <div>
            <dt>Reviewed by</dt>
            <dd title={reviewedAt ? fmtWhen(reviewedAt) : undefined}>
              {d.reviewer_label ?? "unlabelled"}{reviewedAt ? ` · ${fmtAgo(reviewedAt)}` : ""}
            </dd>
          </div>
        )}
      </dl>
      {/* A rejected revision's notes already sit in the header's rejected notice. */}
      {d.review_notes && d.review_status !== "rejected" && (
        <blockquote className="skill-review-notes">{d.review_notes}</blockquote>
      )}
      {meaning && <p className="skill-review-meaning">{meaning}</p>}
    </section>
  );
}

/**
 * One skill at one revision: a reader (header with notices, file tabs, the file) beside a
 * metadata rail (review, `aside`, history). Read-only -- skills change only through
 * KB_MCP. `revision` null = current. Every fetch is numbered so a slow reply for a skill
 * or revision the viewer already left can never replace the one on screen. Files show
 * raw, exactly what an agent receives: the line numbers are CSS counters, never part of
 * the text or a copy.
 */
export function SkillDetail({ name, revision, refreshTick, onSelectRevision, kind = "skill", aside = null }: {
  kind?: LibraryKind;
  /** Rendered in the metadata rail between review and history (the usage panel). */
  aside?: ReactNode;
  name: string;
  revision: number | null;
  refreshTick: number;
  onSelectRevision: (revision: number | null) => void;
}) {
  const [detail, setDetail] = useState<SkillDetailData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const get = kind === "agent" ? fetchAgent : fetchSkill;
  const main = kind === "agent" ? "AGENT.md" : SKILL_FILE;
  const label = kind === "agent" ? "Agent" : "Skill";
  const [path, setPath] = useState(main);
  const [file, setFile] = useState<{ key: string; content: string } | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const seq = useRef(0);
  const fileSeq = useRef(0);
  const currentEntry = useRef<HTMLLIElement>(null);

  // A different skill starts on SKILL.md; a different revision keeps the open file
  // when that revision has it (activePath below falls back otherwise).
  const [allHistory, setAllHistory] = useState(false);
  useEffect(() => {
    setPath(main);
    setAllHistory(false);
  }, [name, main]);
  useEffect(() => {
    setDetail(null);
    setFile(null);
  }, [name, revision, kind]);

  useEffect(() => {
    const mine = ++seq.current;
    setError(null);
    get(name, revision)
      .then((d) => {
        if (mine === seq.current) setDetail(d);
      })
      .catch((err) => {
        if (mine !== seq.current) return;
        setDetail(null);
        const miss = skillMiss(err);
        setError(miss === "skill" ? `${label} not found.`
          : miss === "revision" ? `No revision ${revision} of ${name}.`
          : `Failed to load ${label.toLowerCase()}.`);
      });
  }, [name, revision, refreshTick, kind]);

  const activePath = detail && detail.files.some((f) => f.path === path) ? path : main;
  const shownName = detail?.name;
  const shownRevision = detail?.revision;

  useEffect(() => {
    if (shownName === undefined || shownRevision === undefined || activePath === main) {
      return;
    }
    const mine = ++fileSeq.current;
    const key = `${shownName}@${shownRevision}:${activePath}`;
    setFileError(null);
    fetchSkillFile(shownName, activePath, shownRevision)
      .then((f) => {
        if (mine === fileSeq.current) setFile({ key, content: f.content });
      })
      .catch(() => {
        if (mine === fileSeq.current) setFileError("Failed to load file.");
      });
  }, [shownName, shownRevision, activePath, main]);

  const head = error ? (
    <div className="skills-empty skills-pad" role="alert">{error}</div>
  ) : !detail ? (
    <div className="skills-empty skills-pad">Loading…</div>
  ) : null;

  const key = detail ? `${detail.name}@${detail.revision}:${activePath}` : "";
  const content = !detail ? null
    : activePath === main ? (detail.skill_md ?? detail.agent_md ?? "")
    : file?.key === key ? file.content : null;
  const entry = detail?.history.find((h) => h.revision === detail.revision);
  const toRevision = (r: number) => onSelectRevision(detail && r === detail.current_revision ? null : r);
  const pinned = !!detail && revision !== null && detail.revision !== detail.current_revision;
  const pending = !!detail && detail.pending_revision !== null
    && detail.pending_revision !== detail.revision;
  // The newest revision was turned down: say so where it is read, not only in history.
  const newestEntry = detail?.history[0]?.revision === detail?.latest_revision ? detail?.history[0] : undefined;
  const rejectedNewest = !!detail && newestEntry?.review_status === "rejected"
    && detail.revision !== detail.latest_revision ? newestEntry : undefined;
  const files = detail ? orderedFiles(detail.files) : [];
  const activeFile = files.find((f) => f.path === activePath);
  const lines = content === null ? null : splitLines(content);
  const served = detail?.approved_revision ?? null;
  // By position: two manifest paths can differ only in characters an id cannot carry.
  const tabId = (i: number) => `skill-file-tab-${i}`;
  const activeIndex = files.findIndex((f) => f.path === activePath);
  const ownTrail = detail ? rejectedTrail(detail) : null;
  // A history entry's notes stay out of the rail when a rejected notice above already
  // quotes them (the viewed revision, or the newest one when it was turned down).
  const notesInNotice = (r: number) => !!detail
    && ((r === detail.revision && detail.review_status === "rejected")
      || r === rejectedNewest?.revision);

  // The rail scrolls on its own: when an older revision is on screen, bring its
  // history entry into view there, also while the cards above it (usage) settle, until
  // the viewer scrolls the rail themselves. The newest revision is history's first
  // entry and the review card above says all about it, so the rail stays at its top.
  const viewingOlder = detail !== null && detail.revision !== detail.current_revision;
  useEffect(() => {
    const el = currentEntry.current;
    const rail = el?.closest<HTMLElement>(".skill-rail");
    if (!viewingOlder || !el || !rail) return;
    const reveal = () => {
      if (rail.scrollHeight <= rail.clientHeight) return;   // not a scroller (narrow)
      const r = rail.getBoundingClientRect();
      const e = el.getBoundingClientRect();
      const pad = 12;
      if (e.bottom > r.bottom - pad) rail.scrollTop += Math.min(e.bottom - r.bottom + pad, e.top - r.top - pad);
      else if (e.top < r.top + pad) rail.scrollTop -= r.top + pad - e.top;
    };
    reveal();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(reveal);
    Array.from(rail.children).forEach((c) => ro.observe(c));
    const stop = () => ro.disconnect();
    const events = ["wheel", "touchstart", "pointerdown", "keydown"] as const;
    events.forEach((t) => rail.addEventListener(t, stop, { passive: true }));
    return () => {
      ro.disconnect();
      events.forEach((t) => rail.removeEventListener(t, stop));
    };
  }, [shownName, shownRevision, viewingOlder]);

  // The rail's slots keep their positions in every state, so `aside` (the usage
  // panel) stays mounted while the skill or revision reloads.
  return (
    <div className="skill-workspace">
      {head ?? (detail && (
        <article className="skill-detail" aria-label={`${label} ${detail.name}`}>
          <header className={`skill-detail-head skill-head-${detail.review_status}`}>
            <div className="skill-detail-title">
              <h2>{detail.name}</h2>
              {detail.archived && <span className="skill-badge">archived</span>}
              <span className="skill-rev">rev {detail.revision} of {detail.current_revision}</span>
              {/* The VIEWED revision's own state: pending text is never shown unlabelled. */}
              <span className={`skill-badge skill-badge-${detail.review_status}`}>
                {reviewLabel(detail.review_status)}
              </span>
              {/* When the revision on screen was written; the list's time is the item's
                  last activity (reviews and archiving included), so it is not "updated". */}
              <span className="skill-updated" title={`revision ${detail.revision} written ${fmtWhen(detail.created_at)}`}>
                written {fmtAgo(detail.created_at)}
              </span>
            </div>
            <p className="skill-desc">{detail.description}</p>

            {(pinned || pending || rejectedNewest || detail.review_status === "pending"
              || detail.review_status === "rejected") && (
              <div className="skill-notices">
                {pinned && (
                  <div className="skill-notice skill-notice-pinned" role="status">
                    <span className="skill-notice-text">
                      Viewing revision {detail.revision} — newest is {detail.current_revision}.
                    </span>
                    <button className="skill-notice-btn" onClick={() => onSelectRevision(null)}>Show newest</button>
                  </div>
                )}
                {detail.review_status === "pending" && (
                  <div className="skill-notice skill-notice-pending" role="status">
                    <span className="skill-notice-text">
                      This revision is waiting for review; agents and devices get
                      {served === null ? " nothing" : ` revision ${served}`} until it is approved.
                    </span>
                    {served !== null && (
                      <button className="skill-notice-btn" onClick={() => toRevision(served)}>
                        Read revision {served}
                      </button>
                    )}
                  </div>
                )}
                {detail.review_status === "rejected" && (
                  <div className="skill-notice skill-notice-rejected" role="status">
                    <span className="skill-notice-text">
                      This revision was rejected on review; agents and devices
                      {served === null ? " get nothing" : ` get revision ${served}`}.
                      {ownTrail && <span className="skill-notice-trail">{ownTrail}</span>}
                    </span>
                    {served !== null && (
                      <button className="skill-notice-btn" onClick={() => toRevision(served)}>
                        Read revision {served}
                      </button>
                    )}
                  </div>
                )}
                {pending && (
                  <div className="skill-notice skill-notice-pending" role="status">
                    <span className="skill-notice-text">
                      Revision {detail.pending_revision} is waiting for review; agents and devices get
                      revision {detail.approved_revision ?? "none"} until it is approved.
                    </span>
                    {/* While pinned, "Show newest" already opens the newest revision. */}
                    {!(pinned && detail.pending_revision === detail.current_revision) && (
                      <button className="skill-notice-btn"
                              onClick={() => toRevision(detail.pending_revision as number)}>
                        Read revision {detail.pending_revision}
                      </button>
                    )}
                  </div>
                )}
                {rejectedNewest && (
                  <div className="skill-notice skill-notice-rejected" role="status">
                    <span className="skill-notice-text">
                      Revision {detail.latest_revision} was rejected on review; agents and devices
                      keep revision {detail.approved_revision ?? "none"}.
                      {rejectedTrail(rejectedNewest) && (
                        <span className="skill-notice-trail">{rejectedTrail(rejectedNewest)}</span>
                      )}
                    </span>
                    {!pinned && (
                      <button className="skill-notice-btn"
                              onClick={() => toRevision(detail.latest_revision)}>
                        Read revision {detail.latest_revision}
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}
          </header>

          <div className="skill-editor">
            <div className="skill-files" role="tablist" aria-label="Files">
              {files.map((f, fi) => (
                <button key={f.path} role="tab" id={tabId(fi)} aria-selected={f.path === activePath}
                        aria-controls="skill-file-panel" tabIndex={f.path === activePath ? 0 : -1}
                        className={`skill-file-tab${f.path === activePath ? " active" : ""}`}
                        onClick={() => setPath(f.path)}
                        onKeyDown={(e) => {
                          if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
                          const j = (activeIndex + (e.key === "ArrowRight" ? 1 : files.length - 1)) % files.length;
                          setPath(files[j].path);
                          document.getElementById(tabId(j))?.focus();
                        }}>
                  <span className="skill-file-tab-path" title={f.path}>{f.path}</span>
                  <span className="skill-file-size">{fmtBytes(f.bytes)}</span>
                </button>
              ))}
            </div>
            <div role="tabpanel" id="skill-file-panel" aria-labelledby={tabId(Math.max(0, activeIndex))}
                 className="skill-file-panel">
              <div className="skill-doc-bar">
                <span className="skill-doc-path">
                  <span className="skill-doc-root">{detail.name}/</span>{activePath}
                </span>
                <span className="skill-doc-facts">
                  {lines && <span>{lines.length} {lines.length === 1 ? "line" : "lines"}</span>}
                  {activeFile && <span>{fmtBytes(activeFile.bytes)}</span>}
                  <span>rev {detail.revision}</span>
                </span>
              </div>
              {fileError && activePath !== main ? (
                <div className="skills-empty skills-pad" role="alert">{fileError}</div>
              ) : lines === null ? (
                <pre className="skill-file-content loading" aria-label={`${activePath} content`}>Loading…</pre>
              ) : (
                <pre className="skill-file-content" aria-label={`${activePath} content`}>
                  {lines.map((line, i) => (
                    <span key={i} className="skill-line">{line}{"\n"}</span>
                  ))}
                </pre>
              )}
            </div>
          </div>
        </article>
      ))}

      <aside className="skill-rail">
        {detail && !head && <ReviewCard d={detail} entry={entry} />}
        {aside}
        {detail && !head && (
          <section className="skill-card skill-history" aria-label="History">
            <h3 className="skill-card-title">History</h3>
            <ol>
              {detail.history.filter((h, i) => allHistory || i < HISTORY_PREVIEW
                || h.revision === detail.revision).map((h) => (
                <li key={h.revision} ref={h.revision === detail.revision ? currentEntry : undefined}
                    className={`skill-history-item review-${h.review_status}${h.revision === detail.revision ? " current" : ""}`}>
                  <StatusDot status={h.review_status} />
                  <div className="skill-history-body">
                    <div className="skill-history-line">
                      <button className="link-btn skill-history-rev"
                              aria-current={h.revision === detail.revision ? "true" : undefined}
                              onClick={() => onSelectRevision(
                                h.revision === detail.current_revision ? null : h.revision)}>
                        rev {h.revision}
                      </button>
                      {h.revision === detail.approved_revision && (
                        <span className="skill-tag">served</span>
                      )}
                      <span className="skill-history-when" title={fmtWhen(h.created_at)}>{fmtAgo(h.created_at)}</span>
                    </div>
                    <span className="skill-history-summary">{h.change_summary}</span>
                    <span className="skill-history-who">
                      {h.actor ?? "no verified author"}
                      {h.agent_label ? ` · ${h.agent_label}` : ""}
                    </span>
                    <span className={`skill-history-review review-${h.review_status}`}
                          title={[h.reviewed_at ? `reviewed ${fmtWhen(h.reviewed_at)}` : "",
                            h.review_notes && notesInNotice(h.revision) ? h.review_notes : ""]
                            .filter(Boolean).join(" · ") || undefined}>
                      {reviewLabel(h.review_status)}
                      {/* "superseded by X" would name X as the one who superseded it. */}
                      {h.reviewer_label
                        ? (h.review_status === "superseded" ? ` · reviewed by ${h.reviewer_label}` : ` by ${h.reviewer_label}`)
                        : ""}
                      {h.review_notes && !notesInNotice(h.revision) ? ` · ${h.review_notes}` : ""}
                    </span>
                  </div>
                </li>
              ))}
            </ol>
            {detail.history.length > HISTORY_PREVIEW && (
              <button className="link-btn skill-history-more" aria-expanded={allHistory}
                      onClick={() => setAllHistory((v) => !v)}>
                {allHistory ? "Show fewer" : `Show all ${detail.history.length} revisions`}
              </button>
            )}
            {detail.current_revision > detail.history.length && (
              <p className="skill-meta">Showing the newest {detail.history.length} revisions.</p>
            )}
          </section>
        )}
      </aside>
    </div>
  );
}
