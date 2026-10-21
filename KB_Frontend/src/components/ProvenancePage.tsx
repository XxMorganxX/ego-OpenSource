import { useEffect, useMemo, useState } from "react";

import { fetchProvenance } from "../api/client";
import { useChangeFeed } from "../hooks/useChangeFeed";
import type { ProvenanceEntity } from "../types";
import { DiamondIcon, NoteIcon } from "./icons";

type TypeFilter = "all" | "class" | "instance";

/** One active entity and the memories it was found in. */
function EntityGroup({ entity }: { entity: ProvenanceEntity }) {
  return (
    <section className="prov-group">
      <header className="prov-entity">
        <span className="prov-entity-icon">
          <DiamondIcon size={13} />
        </span>
        <span className="prov-entity-name" title={entity.path}>
          {entity.name}
        </span>
        {entity.entity_type && (
          <span className="badge entity prov-entity-type">{entity.entity_type}</span>
        )}
        <span className="prov-entity-count">
          {entity.memories.length} {entity.memories.length === 1 ? "memory" : "memories"}
        </span>
      </header>

      {entity.memories.length === 0 ? (
        <div className="linked-empty">No memories link to this entity.</div>
      ) : (
        <ul className="linked-memories">
          {entity.memories.map((m) => (
            <li key={m.id}>
              <div className="linked-memory prov-memory" title={m.path}>
                <span className="lm-icon">
                  <NoteIcon size={14} />
                </span>
                <span className="lm-text">
                  <span className="lm-title">{m.title ?? m.slug}</span>
                  <span className="lm-path">{m.path}</span>
                </span>
                <span className="prov-link-type" title={`link type: ${m.link_type}`}>
                  {m.link_type === "belongs_to" ? "classified" : "mentions"}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * Provenance view: every ACTIVE entity grouped with the memories it came from (its
 * entity_links in-links). Answers "what entities came from what memories". Live — refetches
 * when the DB's nodes change. Filterable by name/memory text and by class vs instance.
 */
export function ProvenancePage() {
  const [entities, setEntities] = useState<ProvenanceEntity[] | null>(null);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");

  useEffect(() => {
    let live = true;
    fetchProvenance().then((data) => {
      if (live) setEntities(data.entities);
    });
    return () => {
      live = false;
    };
  }, []);

  // Keep it live: the entity↔memory graph shifts as enrichment runs, so refetch on node changes.
  useChangeFeed((tables) => {
    if (tables.has("nodes")) fetchProvenance().then((data) => setEntities(data.entities));
  });

  const visible = useMemo(() => {
    if (!entities) return [];
    const q = query.trim().toLowerCase();
    return entities.filter((e) => {
      if (typeFilter !== "all" && e.entity_type !== typeFilter) return false;
      if (!q) return true;
      if (e.name.toLowerCase().includes(q)) return true;
      return e.memories.some(
        (m) =>
          (m.title ?? m.slug).toLowerCase().includes(q) ||
          m.path.toLowerCase().includes(q),
      );
    });
  }, [entities, query, typeFilter]);

  if (entities === null) {
    return (
      <div className="prov-page">
        <div className="linked-empty">Loading…</div>
      </div>
    );
  }

  return (
    <div className="prov-page">
      <div className="prov-header">
        <h2 className="prov-title">Provenance</h2>
        <span className="prov-count">
          {visible.length} active {visible.length === 1 ? "entity" : "entities"}
        </span>
        <div className="prov-controls">
          <div className="prov-type-filter" role="group" aria-label="Filter by entity type">
            {(["all", "class", "instance"] as TypeFilter[]).map((t) => (
              <button
                key={t}
                className={`prov-type-btn${typeFilter === t ? " active" : ""}`}
                aria-pressed={typeFilter === t}
                onClick={() => setTypeFilter(t)}
              >
                {t === "all" ? "All" : t === "class" ? "Classes" : "Instances"}
              </button>
            ))}
          </div>
          <input
            className="prov-search"
            type="search"
            placeholder="Filter by entity or memory…"
            aria-label="Filter provenance"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
      </div>

      {entities.length === 0 ? (
        <div className="linked-empty">No active entities yet.</div>
      ) : visible.length === 0 ? (
        <div className="linked-empty">No entities match this filter.</div>
      ) : (
        <div className="prov-list">
          {visible.map((e) => (
            <EntityGroup key={e.id} entity={e} />
          ))}
        </div>
      )}
    </div>
  );
}
