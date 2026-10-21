import { useState } from "react";

import { applyCuration, parseCuration } from "../api/client";
import type { CurationParse } from "../types";

type Props = {
  /** Called after ops apply so the parent refetches the concept layer. */
  onApplied: () => void;
};

/** The NL curation box for concepts mode: an instruction is compiled server-side into
 *  structured ops (grounded, fail-closed), previewed here in plain language, and only
 *  written when the human confirms. NL is the interface; the stored overrides are the
 *  structured rules. */
export function ConceptCommandBar({ onApplied }: Props) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<CurationParse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const doParse = async () => {
    setBusy(true);
    setError(null);
    setPreview(null);
    try {
      setPreview(await parseCuration(text));
    } catch (e) {
      setError(e instanceof Error ? e.message : "parse failed");
    } finally {
      setBusy(false);
    }
  };

  const doApply = async () => {
    if (!preview) return;
    setBusy(true);
    try {
      const res = await applyCuration(preview.ops);
      setError(res.errors.length ? res.errors.join("; ") : null);
      setPreview(null);
      setText("");
      onApplied();
    } catch (e) {
      setError(e instanceof Error ? e.message : "apply failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="concept-cmdbar">
      <textarea
        aria-label="Curation instruction"
        placeholder='e.g. "Vandelay is not part of the Acme club — keep them separate"'
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={2}
      />
      <div className="cmdbar-actions">
        <button className="btn" disabled={busy || !text.trim()} onClick={doParse}>
          Preview
        </button>
      </div>
      {error && <div className="cmdbar-error">{error}</div>}
      {preview && (
        <div className="cmdbar-preview">
          {preview.summary.length === 0 && (
            <div className="cmdbar-empty">No actionable ops — nothing will change.</div>
          )}
          <ul>
            {preview.summary.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ul>
          {preview.warnings.map((w) => (
            <div className="cmdbar-warning" key={w}>
              {w}
            </div>
          ))}
          <div className="cmdbar-actions">
            {preview.ops.length > 0 && (
              <button className="btn primary" disabled={busy} onClick={doApply}>
                Apply
              </button>
            )}
            <button className="btn" disabled={busy} onClick={() => setPreview(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
