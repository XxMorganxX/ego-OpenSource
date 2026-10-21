import { useCallback, useEffect, useState } from "react";
import { fetchAgeDecay, fetchPolicy, setAgeDecay } from "../api/client";
import { useChangeFeed } from "../hooks/useChangeFeed";
import type { PolicyDecayRule } from "../types";

/** Master switch for content-age decay — memories matched by the policy's [[decay]]
 * rules fading from concept clustering and entity promotion. Mirrors McpToolsPanel's
 * mechanics: no optimistic update (the switch shows what the server confirmed) and live
 * refresh via
 * the change feed when another tab flips it. The flip takes effect at the next
 * consolidation rebuild / promotion sweep. With no rules configured the switch is
 * inert, so it is shown disabled with the reason. */
export function AgeDecayToggle() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [rules, setRules] = useState<PolicyDecayRule[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    fetchAgeDecay()
      .then((s) => setEnabled(s.enabled))
      .catch(() => {});
    // The catch above only guards against an unhandled rejection: on failure, state is
    // left unchanged either way — enabled stays null on an initial-load failure (don't
    // render) and keeps the last confirmed value on a later refresh failure.
  }, []);
  useEffect(load, [load]);
  useEffect(() => {
    fetchPolicy()
      .then((p) => setRules(p.policy.decay))
      .catch(() => setRules([]));
  }, []);
  useChangeFeed((tables) => {
    if (tables.has("age_decay_state")) load();
  });

  if (enabled === null) return null;
  const noRules = rules !== null && rules.length === 0;
  const flip = () => {
    setBusy(true);
    setError(null);
    setAgeDecay(!enabled)
      .then((s) => setEnabled(s.enabled))
      .catch((e: Error) => {
        setError(e.message);
      })
      .finally(() => setBusy(false));
  };
  const hint = noRules
    ? "No decay rules configured — add [[decay]] rules to policy.toml"
    : rules === null
      ? "applies at next rebuild"
      : `Rules: ${rules.map((r) => `${r.name} (${r.half_life_days} d)`).join(", ")} · applies at next rebuild`;
  return (
    <div className="age-decay-toggle">
      <button
        role="switch"
        aria-checked={enabled}
        aria-label="Age decay"
        className="btn"
        disabled={busy || noRules}
        onClick={flip}
      >
        {enabled ? "On" : "Off"}
      </button>
      <div className="adt-text">
        <div className="adt-label">Age decay</div>
        <div className="adt-hint">{hint}</div>
        {error && (
          <div className="adt-error" role="alert">
            {error}
          </div>
        )}
      </div>
    </div>
  );
}
