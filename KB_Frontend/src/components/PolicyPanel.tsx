import { useEffect, useState } from "react";
import { fetchPolicy } from "../api/client";
import type { PolicyView } from "../types";

/** Read-only view of the policy the dashboard process loaded. Edits
 * happen in KB_Core/policy.toml followed by a daemon restart; nothing here writes. */
export function PolicyPanel() {
  const [view, setView] = useState<PolicyView | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    fetchPolicy().then(setView).catch((e: Error) => setError(e.message));
  }, []);
  if (error) return <p className="field-note" role="alert">{error}</p>;
  if (!view) return null;
  const p = view.policy;
  const rows: [string, string][] = [
    ["Source", view.source ?? "shipped defaults (no policy.toml)"],
    ["Decay",
     p.decay.length === 0
       ? "no rules"
       : `${p.decay.length} rule${p.decay.length === 1 ? "" : "s"}: ${p.decay
           .map((r) => `${r.name} (${r.half_life_days} d, floor ${r.floor})`).join(", ")}`],
    ["Projects",
     p.projects
       ? `${p.projects.folder_label}, depth ${p.projects.max_depth}, ${p.projects.dormancy_half_life_days} d half-life`
       : "not configured"],
    ["Importance",
     `recency ${p.importance.recency_half_life_days} d / floor ${p.importance.recency_floor}, access saturation ${p.importance.access_saturation}`],
    ["Concepts",
     `window ${p.concepts.goal_window_days} d, data ${p.concepts.data_recency_half_life_days} d, retrieval ${p.concepts.retrieval_recency_half_life_days} d, floor ${p.concepts.recency_floor}`],
    ["Taxonomy",
     `${p.taxonomy.categories.length} categor${p.taxonomy.categories.length === 1 ? "y" : "ies"}, ${Object.keys(p.taxonomy.kind_seeds).length} extra kind seed(s)`],
    ["Prompt overlays",
     view.overlays.length === 0
       ? (p.prompts.overlay_dir ? `${p.prompts.overlay_dir} (empty)` : "none")
       : view.overlays.join(", ")],
  ];
  return (
    <>
      <p className="field-note">
        Read-only. Edit <code>KB_Core/policy.toml</code> and restart the daemons; see
        <code> KB_Core/docs/policy-setup.md</code>.
      </p>
      <dl className="policy-list">
        {rows.map(([k, v]) => (
          <div className="policy-row" key={k}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </>
  );
}
