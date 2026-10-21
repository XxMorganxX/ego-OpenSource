import { useEffect, useState, type ReactNode } from "react";

import { applyClassCuration, fetchClass, fetchClasses, previewCuration } from "../api/client";
import type { ClassDetail, ImpactReport } from "../types";
import { ConfirmDialog } from "./ConfirmDialog";
import { SaveIcon, TrashIcon } from "./icons";

type Props = {
  /** Class (category) name; identity for the curation endpoints. */
  name: string;
  onChanged: () => void;
  onClose: () => void;
  /** Extra content rendered at the end of the scroll body (e.g. the Found-in list). */
  footer?: ReactNode;
  /** A tenant: show the class (description, stats, footer) without any curation control —
   *  the /api/curation/* routes are owner-only and would answer 403. */
  readOnly?: boolean;
};

type Pending = { action: string; args: Record<string, string>; report: ImpactReport };

const CLOSES_PANEL = new Set(["prune", "merge", "rename"]);

/** Curation hub for a selected entity class: shows its taxonomy stats, edits its (lockable)
 *  description, and runs merge / rename / prune / pin / restore — each destructive action
 *  routed through a preview→confirm step that reports the blast radius. Rendered by DetailPanel. */
export function ClassCuration({ name, onChanged, onClose, footer, readOnly = false }: Props) {
  const [cls, setCls] = useState<ClassDetail | null>(null);
  const [description, setDescription] = useState("");
  const [classes, setClasses] = useState<ClassDetail[]>([]);
  const [mergeTarget, setMergeTarget] = useState("");
  const [renameTo, setRenameTo] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);

  useEffect(() => {
    let live = true;
    fetchClass(name).then((c) => {
      if (!live) return;
      setCls(c);
      setDescription(c.description ?? "");
    });
    // The class list only feeds the merge picker, which read-only mode does not render.
    if (!readOnly) {
      fetchClasses().then((r) => {
        if (live) setClasses(r.classes);
      });
    }
    return () => {
      live = false;
    };
  }, [name, readOnly]);

  if (!cls) {
    return (
      <div className="detail-body">
        <div className="linked-empty">Loading…</div>
      </div>
    );
  }

  const preview = async (action: string, args: Record<string, string>) => {
    setPending({ action, args, report: await previewCuration(action, args) });
  };

  const confirm = async () => {
    if (!pending) return;
    const { action, args } = pending;
    await applyClassCuration(action, args);
    setPending(null);
    onChanged();
    if (CLOSES_PANEL.has(action)) onClose();
  };

  const run = async (action: string, args: Record<string, string>) => {
    await applyClassCuration(action, args);
    onChanged();
  };

  const saveDescription = () => run("set_description", { name, description });

  const others = classes.filter((c) => c.name !== cls.name && c.status !== "blocked");

  return (
    <>
      <div className="detail-body">
        {readOnly ? (
          <div className="field">
            <span className="field-label">Description</span>
            <p className="field-help">{cls.description ?? "No description yet."}</p>
          </div>
        ) : (
          <label className="field">
            <span className="field-label">
              Description{cls.description_locked ? " · locked" : ""}
            </span>
            <textarea
              name="description"
              rows={4}
              value={description}
              placeholder="What kind of thing belongs in this category…"
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
        )}

        <div className="meta-grid">
          <span className="meta-key">Status</span>
          <span className="meta-val">
            {cls.status}
            {cls.pinned ? " · pinned" : ""}
          </span>
          <span className="meta-key">Members</span>
          <span className="meta-val">{cls.members}</span>
          <span className="meta-key">Instances</span>
          <span className="meta-val">{cls.instances}</span>
          <span className="meta-key">Feeding memories</span>
          <span className="meta-val">{cls.voters}</span>
        </div>

        {!readOnly && (
          <>
            <div className="field">
              <span className="field-label">Merge into</span>
              <div className="field-control">
                <select
                  aria-label="Merge target"
                  name="merge-target"
                  value={mergeTarget}
                  onChange={(e) => setMergeTarget(e.target.value)}
                >
                  <option value="">Choose a category…</option>
                  {others.map((c) => (
                    <option key={c.name} value={c.name}>
                      {c.name}
                    </option>
                  ))}
                </select>
                <button
                  className="btn"
                  disabled={!mergeTarget}
                  onClick={() => preview("merge", { source: name, target: mergeTarget })}
                >
                  Merge
                </button>
              </div>
              <span className="field-help">
                Folds this category's members into the target, then removes it.
              </span>
            </div>

            <div className="field">
              <span className="field-label">Rename to</span>
              <div className="field-control">
                <input
                  aria-label="Rename to"
                  name="rename-to"
                  autoComplete="off"
                  spellCheck={false}
                  value={renameTo}
                  placeholder="New name"
                  onChange={(e) => setRenameTo(e.target.value)}
                />
                <button
                  className="btn"
                  disabled={!renameTo}
                  onClick={() => preview("rename", { source: name, target: renameTo })}
                >
                  Rename
                </button>
              </div>
            </div>
          </>
        )}

        {footer}
      </div>

      {!readOnly && (
        <div className="detail-foot">
          <button className="btn primary" onClick={saveDescription}>
            <SaveIcon />
            Save
          </button>
          <button className="btn" onClick={() => run(cls.pinned ? "unpin" : "pin", { name })}>
            {cls.pinned ? "Unpin" : "Pin"}
          </button>
          {cls.status === "blocked" ? (
            <button className="btn" onClick={() => run("unblock", { name })}>
              Restore
            </button>
          ) : (
            <button className="btn danger" onClick={() => preview("prune", { name })}>
              <TrashIcon />
              Remove
            </button>
          )}
        </div>
      )}

      {pending && (
        <ConfirmDialog label="Confirm curation" onClose={() => setPending(null)}>
          <p>{pending.report.summary}</p>
          <ul>
            <li>{pending.report.affected_memories} memories affected</li>
            <li>{pending.report.affected_instances} instances purged</li>
            <li>{pending.report.queued_reenrichment} memories re-enriched</li>
          </ul>
          {pending.report.warnings.map((w) => (
            <p key={w} className="warn">
              {w}
            </p>
          ))}
          {!pending.report.reversible && <p className="warn">This cannot be undone.</p>}
          <div className="detail-foot">
            <button className="btn danger" onClick={confirm}>
              Confirm
            </button>
            <button className="btn" onClick={() => setPending(null)}>
              Cancel
            </button>
          </div>
        </ConfirmDialog>
      )}
    </>
  );
}
