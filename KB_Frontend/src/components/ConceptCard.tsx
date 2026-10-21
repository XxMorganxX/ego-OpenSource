import { useState } from "react";

import {
  dismissConcept,
  removeConceptMember,
  updateConceptLabel,
} from "../api/client";
import type { Concept, ConceptMember } from "../types";
import { CheckIcon, CloseIcon, PencilIcon, TrashIcon } from "./icons";

type Props = {
  concept: Concept | null;
  /** Name of the level-0 parent when this is a level-1 sub-concept. */
  parentName: string | null;
  onClose: () => void;
  /** Members are real KB nodes — this opens the regular node detail panel. */
  onSelectNode: (id: string) => void;
  /** Called after any curation action (rename, dismiss, remove member) succeeds, so the
   *  caller can reload the concept list/graph from the server. */
  onChanged: () => void;
};

function MemberList({
  members,
  onSelectNode,
  onRemove,
}: {
  members: ConceptMember[];
  onSelectNode: (id: string) => void;
  onRemove: (nodeId: string) => void;
}) {
  return (
    <ul className="member-list">
      {members.map((m) => (
        <li key={m.node_id} className="member-row">
          <button className="link-btn" onClick={() => onSelectNode(m.node_id)}>
            {m.label ?? m.node_id}
          </button>
          {m.doc_kind ? (
            <span className="concept-kind">{m.doc_kind}</span>
          ) : null}
          <span className="member-meta">
            <span className="member-score">{m.score.toFixed(2)}</span>
            <button
              className="icon-btn"
              aria-label={`Remove ${m.label ?? m.node_id} from this concept`}
              onClick={() => onRemove(m.node_id)}
            >
              ×
            </button>
          </span>
        </li>
      ))}
    </ul>
  );
}

/** Card for one discovered concept: what it is and its scored members, plus curation
 *  actions (rename, dismiss, remove a member). Consolidation re-derives everything shown
 *  here, but a locked label and a dismissed status survive the next run. */
export function ConceptCard({ concept, parentName, onClose, onSelectNode, onChanged }: Props) {
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState("");
  const [confirmDismiss, setConfirmDismiss] = useState(false);

  if (!concept) return null;
  const entities = concept.members.filter((m) => m.member_kind === "entity");
  const memories = concept.members.filter((m) => m.member_kind === "memory");

  const startRename = () => {
    setDraft(concept.name);
    setRenaming(true);
  };

  const saveRename = () => {
    updateConceptLabel(concept.id, { name: draft }).then(() => {
      setRenaming(false);
      onChanged();
    });
  };

  const dismiss = () => {
    dismissConcept(concept.id).then(() => {
      onChanged();
      onClose();
    });
  };

  const removeMember = (nodeId: string) => {
    removeConceptMember(concept.id, nodeId).then(onChanged);
  };

  return (
    <aside className="detail">
      <div className="concept-head">
        <span className={`concept-kind kind-${concept.kind}`}>{concept.kind}</span>
        {renaming ? (
          <input
            aria-label="Concept name"
            className="concept-title-input"
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
        ) : (
          <h2 className="concept-title">{concept.name}</h2>
        )}
        {!renaming && concept.label_locked && (
          <span className="concept-lock" title="Label locked by you">
            locked
          </span>
        )}
        {renaming ? (
          <button className="icon-btn" aria-label="Save name" onClick={saveRename}>
            <CheckIcon size={14} />
          </button>
        ) : (
          <button className="icon-btn" aria-label="Rename concept" onClick={startRename}>
            <PencilIcon size={14} />
          </button>
        )}
        {confirmDismiss ? (
          <button className="btn danger" onClick={dismiss}>
            Confirm dismiss
          </button>
        ) : (
          <button
            className="icon-btn"
            aria-label="Dismiss concept"
            onClick={() => setConfirmDismiss(true)}
          >
            <TrashIcon size={14} />
          </button>
        )}
        <button className="icon-btn" aria-label="Close" onClick={onClose}>
          <CloseIcon size={16} />
        </button>
      </div>
      {parentName && <div className="concept-sub">part of {parentName}</div>}
      {concept.description && <p className="concept-desc">{concept.description}</p>}
      <div className="concept-sub">
        {concept.member_count} members · derived by consolidation — rebuilt every run
      </div>
      {entities.length > 0 && (
        <>
          <h3 className="concept-section">Entities</h3>
          <MemberList members={entities} onSelectNode={onSelectNode} onRemove={removeMember} />
        </>
      )}
      {memories.length > 0 && (
        <>
          <h3 className="concept-section">Memories</h3>
          <MemberList members={memories} onSelectNode={onSelectNode} onRemove={removeMember} />
        </>
      )}
    </aside>
  );
}
