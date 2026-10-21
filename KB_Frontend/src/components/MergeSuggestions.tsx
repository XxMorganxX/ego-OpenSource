import { useEffect, useState } from "react";

import { approveMerge, fetchMergeSuggestions, rejectMerge } from "../api/client";
import { CheckIcon, MergeArrowIcon, RefreshIcon } from "./icons";
import type { MergeSuggestion, MergeSuggestionSide } from "../types";

/** One selectable half of a merge pair. Choosing a side makes it the merge
    target — the entity that survives with the other's mentions and aliases. */
function EntitySide({
  side,
  suggested,
  checked,
  group,
  onChoose,
}: {
  side: MergeSuggestionSide;
  suggested: boolean;
  checked: boolean;
  group: string;
  onChoose: () => void;
}) {
  return (
    <label className={`merge-side${checked ? " selected" : ""}`}>
      <input
        type="radio"
        name={group}
        value={side.id}
        checked={checked}
        onChange={onChoose}
        aria-label={side.name}
      />
      <span className="merge-side-top">
        <span className="merge-entity-name">{side.name}</span>
        {checked ? (
          <span className="merge-keep">
            <CheckIcon size={11} />
            Keep
          </span>
        ) : suggested ? (
          <span className="merge-suggested">Suggested</span>
        ) : null}
      </span>
      {side.aliases.length > 0 && (
        <span className="merge-aliases">
          {side.aliases.map((alias) => (
            <span key={alias} className="merge-alias">
              {alias}
            </span>
          ))}
        </span>
      )}
      <span className="merge-mentions">
        {side.mention_count} mention{side.mention_count === 1 ? "" : "s"}
      </span>
    </label>
  );
}

/** A pending suggestion card: pick the surviving entity, then approve or reject. */
function SuggestionRow({
  suggestion,
  onResolved,
}: {
  suggestion: MergeSuggestion;
  onResolved: (id: number) => void;
}) {
  const [chosenTarget, setChosenTarget] = useState<string>(
    suggestion.suggested_target_id ?? suggestion.left.id,
  );
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const keepLeft = chosenTarget === suggestion.left.id;
  const winner = keepLeft ? suggestion.left : suggestion.right;
  const loser = keepLeft ? suggestion.right : suggestion.left;
  const similarity = Math.max(0, Math.min(1, 1 - suggestion.distance));

  const handleApprove = async () => {
    setBusy("approve");
    setError(null);
    try {
      await approveMerge(suggestion.id, chosenTarget);
      onResolved(suggestion.id);
    } catch {
      setError("Couldn’t approve the merge — try again.");
    } finally {
      setBusy(null);
    }
  };

  const handleReject = async () => {
    setBusy("reject");
    setError(null);
    try {
      await rejectMerge(suggestion.id);
      onResolved(suggestion.id);
    } catch {
      setError("Couldn’t reject the suggestion — try again.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <article className="merge-card">
      <header className="merge-card-head">
        <span className="merge-kind">{suggestion.kind}</span>
        <span
          className="merge-sim"
          title={`Semantic distance ${suggestion.distance.toFixed(3)} — lower means more alike`}
        >
          <span className="merge-sim-track" aria-hidden="true">
            <span className="merge-sim-fill" style={{ width: `${similarity * 100}%` }} />
          </span>
          <span className="merge-sim-value">{suggestion.distance.toFixed(3)}</span>
        </span>
      </header>

      <div className="merge-pair">
        <EntitySide
          side={suggestion.left}
          suggested={suggestion.suggested_target_id === suggestion.left.id}
          checked={keepLeft}
          group={`merge-target-${suggestion.id}`}
          onChoose={() => setChosenTarget(suggestion.left.id)}
        />
        <span className={`merge-arrow${keepLeft ? " to-left" : ""}`} aria-hidden="true">
          <MergeArrowIcon size={15} />
        </span>
        <EntitySide
          side={suggestion.right}
          suggested={suggestion.suggested_target_id === suggestion.right.id}
          checked={!keepLeft}
          group={`merge-target-${suggestion.id}`}
          onChoose={() => setChosenTarget(suggestion.right.id)}
        />
      </div>

      {suggestion.judge_reason && (
        <div className="merge-reason">
          <span className="merge-reason-label">Model’s reasoning</span>
          <p className="merge-reason-text">{suggestion.judge_reason}</p>
        </div>
      )}

      <footer className="merge-card-foot">
        {error ? (
          <p className="merge-error" role="alert">
            {error}
          </p>
        ) : (
          <p className="merge-outcome">
            Merges <b>“{loser.name}”</b> into <b>“{winner.name}”</b> — mentions and
            aliases move to the kept entity.
          </p>
        )}
        <div className="merge-actions">
          <button className="btn danger" onClick={handleReject} disabled={busy !== null}>
            {busy === "reject" ? "Rejecting…" : "Reject"}
          </button>
          <button className="btn primary" onClick={handleApprove} disabled={busy !== null}>
            {busy === "approve" ? "Approving…" : "Approve merge"}
          </button>
        </div>
      </footer>
    </article>
  );
}

/** Pending entity-merge review queue: each card pairs two likely-duplicate
    entities; the reviewer picks the surviving one and approves, or rejects. */
export function MergeSuggestions() {
  const [suggestions, setSuggestions] = useState<MergeSuggestion[] | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    let live = true;
    fetchMergeSuggestions().then((data) => {
      if (live) setSuggestions(data);
    });
    return () => {
      live = false;
    };
  }, []);

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      setSuggestions(await fetchMergeSuggestions());
    } finally {
      setRefreshing(false);
    }
  };

  const handleResolved = (id: number) => {
    setSuggestions((prev) => prev?.filter((s) => s.id !== id) ?? null);
  };

  if (suggestions === null) {
    return (
      <div className="merge-page">
        <div className="merge-loading">
          <div className="spinner" />
          <p>Loading pending merges…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="merge-page">
      <div className="merge-wrap">
        <header className="merge-header">
          <div className="merge-header-text">
            <h2 className="merge-title">Pending Merges</h2>
            <p className="merge-sub">
              Pairs of entities the analysis pass flagged as likely duplicates. Pick
              the name to keep, then approve the merge — or reject if they are
              genuinely different things.
            </p>
          </div>
          <span className="merge-count" aria-live="polite">
            <b>{suggestions.length}</b> suggestion{suggestions.length !== 1 ? "s" : ""}
          </span>
          <button
            className="icon-btn"
            aria-label="Refresh suggestions"
            title="Refresh suggestions"
            onClick={handleRefresh}
            disabled={refreshing}
          >
            <RefreshIcon size={15} className={refreshing ? "spinning" : undefined} />
          </button>
        </header>

        {suggestions.length === 0 ? (
          <div className="merge-empty">
            <div className="merge-empty-glyph">
              <CheckIcon size={22} />
            </div>
            <p className="merge-empty-title">All caught up</p>
            <p className="merge-empty-text">
              No pending merges. New suggestions appear here after the next analysis
              pass.
            </p>
          </div>
        ) : (
          <div className="merge-list">
            {suggestions.map((s) => (
              <SuggestionRow key={s.id} suggestion={s} onResolved={handleResolved} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
