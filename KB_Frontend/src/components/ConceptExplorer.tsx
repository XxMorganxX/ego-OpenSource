import { useMemo, useState } from "react";

import { CONCEPT_KIND_COLORS } from "../lib/concepts";
import type { Concept } from "../types";

type Props = {
  concepts: Concept[] | null;
  selectedConceptId: string | null;
  error: string | null;
  onSelect: (id: string) => void;
  onRetry: () => void;
};

const byName = (left: Concept, right: Concept) =>
  left.name.localeCompare(right.name, undefined, { sensitivity: "base" });

const matches = (concept: Concept, query: string) =>
  [concept.name, concept.kind, concept.description ?? ""]
    .some((value) => value.toLocaleLowerCase().includes(query));

function ConceptRow({
  concept,
  parentName,
  selected,
  onSelect,
}: {
  concept: Concept;
  /** Set for sub-clusters — indents the row and shows the description. */
  parentName: string | null;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <button
      className={`concept-row${parentName ? " concept-row-child" : ""}${selected ? " selected" : ""}`}
      aria-current={selected ? "true" : undefined}
      aria-label={`${concept.name}, ${parentName ? `sub-cluster of ${parentName}, ` : ""}${concept.kind}, ${concept.member_count} members`}
      onClick={() => onSelect(concept.id)}
    >
      <span
        className="concept-row-marker"
        style={{
          background: CONCEPT_KIND_COLORS[concept.kind],
          boxShadow: `0 0 9px ${CONCEPT_KIND_COLORS[concept.kind]}`,
        }}
      />
      <span className="concept-row-main">
        <span className="concept-row-name" data-testid="concept-name">{concept.name}</span>
        <span className="concept-row-kind">{concept.kind}</span>
        {parentName && concept.description && (
          <span className="concept-row-desc" title={concept.description}>{concept.description}</span>
        )}
      </span>
      <span className="concept-row-count">{concept.member_count}</span>
    </button>
  );
}

export function ConceptExplorer({ concepts, selectedConceptId, error, onSelect, onRetry }: Props) {
  const [query, setQuery] = useState("");
  const active = useMemo(() => (concepts ?? []).filter((concept) => concept.status === "active"), [concepts]);
  const activeIds = useMemo(() => new Set(active.map((concept) => concept.id)), [active]);
  // A concept is a root when no ACTIVE concept claims it as a child — it has no parent,
  // or its parent is dismissed/retired/absent. These two predicates are complements, so
  // every active concept lands in exactly one branch and the tree cannot silently lose
  // one. It could before: `roots` also required level === 0, so a parentless level-1
  // concept matched neither this filter nor childrenOf below (which skips null parents)
  // and rendered nowhere. Such concepts were invisible that way — the backend now
  // re-levels such orphans in apply_concept_run, and this keeps the view robust anyway.
  const hasActiveParent = (concept: Concept) =>
    concept.parent_id !== null && activeIds.has(concept.parent_id);
  const roots = useMemo(
    () => active.filter((concept) => !hasActiveParent(concept)).sort(byName),
    [active, activeIds],
  );
  const childrenOf = useMemo(() => {
    const map = new Map<string, Concept[]>();
    for (const concept of active) {
      if (!hasActiveParent(concept)) continue;
      map.set(concept.parent_id!, [...(map.get(concept.parent_id!) ?? []), concept]);
    }
    for (const children of map.values()) children.sort(byName);
    return map;
  }, [active]);

  const normalizedQuery = query.trim().toLocaleLowerCase();
  // A query hit on a sub-cluster keeps its parent visible; a hit on the parent shows all children.
  const visible = roots.flatMap((root) => {
    const children = childrenOf.get(root.id) ?? [];
    if (matches(root, normalizedQuery)) return [{ root, children }];
    const matching = children.filter((child) => matches(child, normalizedQuery));
    return matching.length > 0 ? [{ root, children: matching }] : [];
  });

  return (
    <section className="concept-explorer" aria-label="Concepts">
      <div className="pane-head">
        Concepts <span className="count">{roots.length}</span>
      </div>
      {error ? (
        <div className="concept-explorer-state" role="alert">
          <span>Concepts could not be loaded</span>
          <button className="btn" onClick={onRetry}>Retry</button>
        </div>
      ) : concepts === null ? (
        <div className="concept-explorer-state" role="status"><span className="spinner" />Loading concepts…</div>
      ) : roots.length === 0 ? (
        <div className="concept-explorer-state">No top-level concepts yet</div>
      ) : (
        <>
          <div className="concept-search">
            <input type="search" aria-label="Search concepts" placeholder="Search concepts" value={query} onChange={(event) => setQuery(event.target.value)} />
          </div>
          <div className="concept-list">
            {visible.map(({ root, children }) => (
              <div key={root.id} className="concept-group">
                <ConceptRow concept={root} parentName={null} selected={selectedConceptId === root.id} onSelect={onSelect} />
                {children.map((child) => (
                  <ConceptRow key={child.id} concept={child} parentName={root.name} selected={selectedConceptId === child.id} onSelect={onSelect} />
                ))}
              </div>
            ))}
            {visible.length === 0 && <div className="concept-explorer-state">No concepts match “{query.trim()}”</div>}
          </div>
        </>
      )}
    </section>
  );
}
