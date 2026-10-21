import type { GraphMode } from "../types";

/** The top-left graph controls: the Memories | Entities | Concepts mode toggle and the
 *  "show instances" / "show inactive" switches (visible only in the all-entities,
 *  unfocused view). */
export function GraphControls({
  mode,
  entityClass,
  showInstances,
  onModeChange,
  onShowInstancesChange,
  showInactive,
  onShowInactiveChange,
}: {
  mode: GraphMode;
  entityClass: string | null;
  showInstances: boolean;
  onModeChange: (mode: GraphMode) => void;
  onShowInstancesChange: (value: boolean) => void;
  showInactive: boolean;
  onShowInactiveChange: (value: boolean) => void;
}) {
  return (
    <div className="graph-controls">
      <div className="seg" role="group" aria-label="Graph mode">
        <button
          className={`seg-btn${mode === "memories" ? " active" : ""}`}
          aria-pressed={mode === "memories"}
          onClick={() => onModeChange("memories")}
        >
          Memories
        </button>
        <button
          className={`seg-btn${mode === "entities" ? " active" : ""}`}
          aria-pressed={mode === "entities"}
          onClick={() => onModeChange("entities")}
        >
          Entities
        </button>
        <button
          className={`seg-btn${mode === "concepts" ? " active" : ""}`}
          aria-pressed={mode === "concepts"}
          onClick={() => onModeChange("concepts")}
        >
          Concepts
        </button>
      </div>
      {mode === "entities" && !entityClass && (
        <label className="inst-toggle" title="Reveal the instances filed under each class">
          <input
            type="checkbox"
            checked={showInstances}
            onChange={(e) => onShowInstancesChange(e.target.checked)}
          />
          Show instances
        </label>
      )}
      {mode === "entities" && !entityClass && (
        <label className="inst-toggle" title="Reveal categories that never activated">
          <input
            type="checkbox"
            checked={showInactive}
            onChange={(e) => onShowInactiveChange(e.target.checked)}
          />
          Show inactive
        </label>
      )}
    </div>
  );
}
