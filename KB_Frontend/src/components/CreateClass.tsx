import { useState } from "react";

import { applyClassCuration, previewCuration } from "../api/client";
import type { ImpactReport } from "../types";
import { ConfirmDialog } from "./ConfirmDialog";

type Props = { onCreated: () => void };

/** A small form to create a new (pinned) category. Previews first so the user sees warnings —
 *  e.g. the name is blocked or already exists — before committing. */
export function CreateClass({ onCreated }: Props) {
  const [name, setName] = useState("");
  const [report, setReport] = useState<ImpactReport | null>(null);

  const preview = async () => {
    if (!name.trim()) return;
    setReport(await previewCuration("create", { name }));
  };

  const confirm = async () => {
    await applyClassCuration("create", { name });
    setReport(null);
    setName("");
    onCreated();
  };

  return (
    <div className="create-class">
      <label className="field">
        <span className="field-label">New category</span>
        <input
          aria-label="New category"
          name="category"
          autoComplete="off"
          spellCheck={false}
          value={name}
          placeholder="Name a category…"
          onChange={(e) => setName(e.target.value)}
        />
        <button className="btn" disabled={!name.trim()} onClick={preview}>
          Create
        </button>
      </label>

      {report && (
        <ConfirmDialog label="Confirm create" onClose={() => setReport(null)}>
          <p>{report.summary}</p>
          {report.warnings.map((w) => (
            <p key={w} className="warn">
              {w}
            </p>
          ))}
          <div className="detail-foot">
            <button className="btn primary" onClick={confirm}>
              Confirm
            </button>
            <button className="btn" onClick={() => setReport(null)}>
              Cancel
            </button>
          </div>
        </ConfirmDialog>
      )}
    </div>
  );
}
