import { useState } from "react";

import { MOBILE_QUERY } from "../hooks/useMediaQuery";
import { COLOR_SCHEMES } from "../lib/colorScheme";
import type { ColorSchemeId } from "../types";
import { ChevronIcon, FunnelIcon } from "./icons";

type Props = {
  /** The active color scheme id (from graph settings). */
  scheme: ColorSchemeId;
  /** Apply a different color scheme. */
  onSchemeChange: (id: ColorSchemeId) => void;
};

/**
 * The graph view-filter overlay (top-right of the memories graph). A collapsible config
 * panel for how nodes are rendered; today it picks the node COLOR scheme — the extension
 * point for future shape/style filters is a second section here, each driven by its own
 * scheme registry (see {@link COLOR_SCHEMES}). Memories-only; entities mode owns its palette.
 */
export function GraphFilterPanel({ scheme, onSchemeChange }: Props) {
  // Open by default on desktop; collapsed on mobile, where the expanded panel
  // would blanket the graph-only surface.
  const [open, setOpen] = useState(
    () =>
      !(
        typeof window.matchMedia === "function" &&
        window.matchMedia(MOBILE_QUERY).matches
      ),
  );

  return (
    <div className={`graph-filter${open ? " open" : ""}`}>
      <button
        type="button"
        className="graph-filter-head"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        title="View filters"
      >
        <FunnelIcon size={14} />
        <span className="graph-filter-title">View</span>
        <ChevronIcon size={13} className="graph-filter-caret" />
      </button>
      {open && (
        <div className="graph-filter-body">
          <div className="graph-filter-group" role="radiogroup" aria-label="Node color scheme">
            <span className="graph-filter-label">Color</span>
            {COLOR_SCHEMES.map((s) => (
              <button
                key={s.id}
                type="button"
                role="radio"
                aria-checked={s.id === scheme}
                className={`graph-filter-opt${s.id === scheme ? " active" : ""}`}
                onClick={() => onSchemeChange(s.id)}
              >
                <span className="graph-filter-opt-label">{s.label}</span>
                <span className="graph-filter-opt-hint">{s.description}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
