import { useMemo, useState } from "react";

import { entityMembership } from "../lib/tree";
import type { EntityGraphData, EntityNodeMeta } from "../types";
import { ChevronIcon, DiamondIcon, GraphIcon } from "./icons";

type Props = {
  /** Full entity graph; null while it loads. */
  entityData: EntityGraphData | null;
  /** Focused class id (null = all entities). */
  selectedClass: string | null;
  /** Currently open node in the detail panel, for row highlighting. */
  selectedNodeId: string | null;
  onSelectClass: (id: string | null) => void;
  onSelectNode: (id: string) => void;
};

/**
 * Left-pane navigator for the entity graph — the entity-mode counterpart to TreeView.
 * Reuses the tree pane shell but is deliberately styled apart from the memory folder
 * tree (diamond glyphs, lavender accent). Classes expand to reveal their instances;
 * clicking any entity focuses it in the graph and opens its detail on the right.
 */
export function EntityNav({
  entityData,
  selectedClass,
  selectedNodeId,
  onSelectClass,
  onSelectNode,
}: Props) {
  const [open, setOpen] = useState<Set<string>>(new Set());

  const classes = useMemo(() => {
    const list = entityData
      ? entityData.nodes.filter((n) => n.role === "class")
      : [];
    return [...list].sort((a, b) => a.slug.localeCompare(b.slug));
  }, [entityData]);

  const instancesByClass = useMemo(() => {
    const map = new Map<string, EntityNodeMeta[]>();
    if (!entityData) return map;
    // Membership is a link now (an instance can sit under several classes), not parent_id.
    const byId = new Map(entityData.nodes.map((n) => [n.id, n]));
    const { classInstances } = entityMembership(entityData);
    for (const [classId, instanceIds] of classInstances) {
      const arr = instanceIds
        .map((id) => byId.get(id))
        .filter((n): n is EntityNodeMeta => !!n);
      arr.sort((a, b) => a.slug.localeCompare(b.slug));
      map.set(classId, arr);
    }
    return map;
  }, [entityData]);

  const toggle = (id: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  // Focus the class in the graph (instance clicks scope to their parent class) and open
  // the clicked entity's detail.
  const selectClass = (id: string) => {
    onSelectClass(id);
    onSelectNode(id);
  };
  // Scope to the class the instance is listed under (membership is a link — an instance can
  // appear beneath several classes — so the class comes from the render context, not parent_id,
  // which is the flat instances root).
  const selectInstance = (inst: EntityNodeMeta, classId: string) => {
    onSelectClass(classId);
    onSelectNode(inst.id);
  };

  return (
    <nav className="tree entities" aria-label="Entity classes">
      <div className="pane-head">
        Entities
        <span className="count">{classes.length} classes</span>
      </div>
      <div className="tree-scroll">
        <button
          type="button"
          className={`tree-item${selectedClass === null ? " selected" : ""}`}
          style={{ paddingLeft: 8 }}
          onClick={() => onSelectClass(null)}
          aria-current={selectedClass === null ? "true" : undefined}
          title="All entities — show every class"
        >
          <span className="tw-chevron open">
            <ChevronIcon />
          </span>
          <span className="tw-icon root">
            <GraphIcon size={16} />
          </span>
          <span className="tw-label">All entities</span>
        </button>
        {entityData === null ? (
          <div className="tree-empty">Loading…</div>
        ) : classes.length === 0 ? (
          <div className="tree-empty">No entities yet.</div>
        ) : (
          classes.map((c) => {
            const instances = instancesByClass.get(c.id) ?? [];
            const isOpen = open.has(c.id);
            return (
              <div key={c.id}>
                <button
                  type="button"
                  className={`tree-item${c.id === selectedNodeId ? " selected" : ""}`}
                  style={{ paddingLeft: 8 + 15 }}
                  onClick={() => {
                    if (instances.length) toggle(c.id);
                    selectClass(c.id);
                  }}
                  aria-current={c.id === selectedNodeId ? "true" : undefined}
                  aria-expanded={instances.length ? isOpen : undefined}
                  title={c.title ?? c.slug}
                >
                  <span
                    className={`tw-chevron${
                      instances.length ? (isOpen ? " open" : "") : " leaf"
                    }`}
                  >
                    <ChevronIcon />
                  </span>
                  <span className="tw-icon entity">
                    <DiamondIcon size={13} />
                  </span>
                  <span className="tw-label">{c.slug}</span>
                  <span className="tw-count">{instances.length}</span>
                </button>
                {isOpen &&
                  instances.map((inst) => (
                    <button
                      type="button"
                      key={inst.id}
                      className={`tree-item${inst.id === selectedNodeId ? " selected" : ""}`}
                      style={{ paddingLeft: 8 + 30 }}
                      onClick={() => selectInstance(inst, c.id)}
                      aria-current={inst.id === selectedNodeId ? "true" : undefined}
                      title={inst.title ?? inst.slug}
                    >
                      <span className="tw-chevron leaf">
                        <ChevronIcon />
                      </span>
                      <span className="tw-icon entity instance">
                        <DiamondIcon size={11} />
                      </span>
                      <span className="tw-label">{inst.slug}</span>
                    </button>
                  ))}
              </div>
            );
          })
        )}
      </div>
    </nav>
  );
}
