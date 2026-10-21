import { useCallback, useEffect, useState } from "react";

import { fetchExcludedMemories, reviveMemory } from "../api/client";
import { GRAVITY_MAX, GRAVITY_MIN, SPACING_MAX, SPACING_MIN } from "../lib/settings";
import type { ExcludedMemory, GraphSettings, Me } from "../types";
import { CloseIcon, LogoutIcon } from "./icons";
import { PolicyPanel } from "./PolicyPanel";

// Cloudflare Access logout endpoint, handled at the edge when the dashboard sits behind
// an Access application: clears the session cookie so the next visit signs in again.
const LOGOUT_URL = "/cdn-cgi/access/logout";

type Tab = "graph" | "memories" | "policy";
type Filter = "all" | "noise" | "dead";

type Props = {
  // The signed-in identity (GET /api/me); null/absent on a pre-tenancy backend.
  me?: Me | null;
  settings: GraphSettings;
  onChange: (settings: GraphSettings) => void;
  onClose: () => void;
};

export function SettingsModal({ me = null, settings, onChange, onClose }: Props) {
  const [tab, setTab] = useState<Tab>("graph");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <div className="modal-head-left">
            <span className="modal-title">Settings</span>
            {me && (
              <span className="brand-sub">
                {me.email ? `· ${me.email} · ${me.tenant}` : "· identity unknown"}
              </span>
            )}
            <div className="tabs" role="tablist" aria-label="Settings sections">
              <button
                className={tab === "graph" ? "tab active" : "tab"}
                role="tab"
                aria-selected={tab === "graph"}
                onClick={() => setTab("graph")}
              >
                Graph
              </button>
              <button
                className={tab === "memories" ? "tab active" : "tab"}
                role="tab"
                aria-selected={tab === "memories"}
                onClick={() => setTab("memories")}
              >
                Memories
              </button>
              <button
                className={tab === "policy" ? "tab active" : "tab"}
                role="tab"
                aria-selected={tab === "policy"}
                onClick={() => setTab("policy")}
              >
                Policy
              </button>
            </div>
          </div>
          <button className="icon-btn" aria-label="Close settings" onClick={onClose}>
            <CloseIcon />
          </button>
        </div>
        <div className="modal-body">
          {tab === "graph" ? (
            <GraphPanel settings={settings} onChange={onChange} />
          ) : tab === "memories" ? (
            <MemoriesPanel />
          ) : (
            <PolicyPanel />
          )}
        </div>
      </div>
    </div>
  );
}

function GraphPanel({
  settings,
  onChange,
}: {
  settings: GraphSettings;
  onChange: (settings: GraphSettings) => void;
}) {
  return (
    <>
      <label className="field">
        <div className="field-row">
          <span className="field-label">Dispersal</span>
          <span className="field-value">{settings.spacing}</span>
        </div>
        <input
          type="range"
          className="slider"
          min={SPACING_MIN}
          max={SPACING_MAX}
          step={5}
          value={settings.spacing}
          aria-label="Graph dispersal"
          onChange={(e) => onChange({ ...settings, spacing: Number(e.target.value) })}
        />
        <div className="field-hint">
          <span>Tighter</span>
          <span>Looser</span>
        </div>
      </label>
      <p className="field-note">
        Spreads both connected nodes and the gaps between separate directories.
      </p>

      <label className="field">
        <div className="field-row">
          <span className="field-label">Cluster gravity</span>
          <span className="field-value">{settings.gravity}</span>
        </div>
        <input
          type="range"
          className="slider"
          min={GRAVITY_MIN}
          max={GRAVITY_MAX}
          step={1}
          value={settings.gravity}
          aria-label="Cluster gravity"
          onChange={(e) => onChange({ ...settings, gravity: Number(e.target.value) })}
        />
        <div className="field-hint">
          <span>Repel</span>
          <span>Attract</span>
        </div>
      </label>
      <p className="field-note">
        Repels clusters from their layout targets below zero; attracts them above zero.
      </p>

      <p className="field-note">
        Node coloring (default, by depth, by importance) now lives in the <strong>View</strong>{" "}
        filter on the graph.
      </p>

      <div className="modal-section">
        <span className="field-label">Session</span>
        <a className="btn logout-btn" href={LOGOUT_URL}>
          <LogoutIcon />
          Log out
        </a>
      </div>
    </>
  );
}

// The "shut off" memories: those excluded from the going-forward entity system. Noise (no
// extractable meaning — its prior influence was retracted) and dead (no longer chronologically
// relevant — retired/frozen, but its prior influence kept). Dead memories can be revived; noise
// recovers on its own when the content gains meaning and is re-analyzed.
function MemoriesPanel() {
  const [filter, setFilter] = useState<Filter>("all");
  const [memories, setMemories] = useState<ExcludedMemory[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reviving, setReviving] = useState<string | null>(null);

  const load = useCallback(() => {
    setMemories(null);
    setError(null);
    fetchExcludedMemories(filter === "all" ? undefined : filter)
      .then((r) => setMemories(r.memories))
      .catch((e) => setError(String(e)));
  }, [filter]);

  useEffect(() => load(), [load]);

  const onRevive = (id: string) => {
    setReviving(id);
    reviveMemory(id)
      .then(() => setMemories((cur) => (cur ? cur.filter((m) => m.id !== id) : cur)))
      .catch((e) => setError(String(e)))
      .finally(() => setReviving(null));
  };

  return (
    <div className="memories-panel">
      <p className="field-note">
        Memories shut off from the entity system. They stay searchable — they just no longer
        vote on or shape entities. <strong>Noise</strong> had no real content (its influence was
        undone); <strong>dead</strong> is no longer current (frozen, its past influence kept).
      </p>

      <div className="seg" role="tablist" aria-label="Filter excluded memories">
        {(["all", "noise", "dead"] as Filter[]).map((f) => (
          <button
            key={f}
            className={filter === f ? "seg-btn active" : "seg-btn"}
            role="tab"
            aria-selected={filter === f}
            onClick={() => setFilter(f)}
          >
            {f === "all" ? "All" : f === "noise" ? "Noise" : "Dead"}
          </button>
        ))}
      </div>

      {error && <p className="field-note error">{error}</p>}
      {memories === null && !error && <p className="field-note">Loading…</p>}
      {memories !== null && memories.length === 0 && (
        <p className="empty-note">No {filter === "all" ? "shut-off" : filter} memories.</p>
      )}

      <ul className="excluded-list">
        {memories?.map((m) => (
          <li className="excluded-row" key={m.id}>
            <div className="excluded-main">
              <div className="excluded-title-row">
                <span className={`badge ${m.lifecycle}`}>
                  <span className="badge-dot" />
                  {m.lifecycle}
                </span>
                <span className="excluded-title">{m.title || m.slug}</span>
              </div>
              <span className="excluded-path">{m.path}</span>
              {m.reason && <span className="excluded-reason">{m.reason}</span>}
            </div>
            {m.lifecycle === "dead" ? (
              <button
                className="btn"
                disabled={reviving === m.id}
                onClick={() => onRevive(m.id)}
              >
                {reviving === m.id ? "Reviving…" : "Revive"}
              </button>
            ) : (
              <span className="excluded-hint">recovers when edited</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
