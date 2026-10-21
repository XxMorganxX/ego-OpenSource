import { useState } from "react";

import type { TreeNode } from "../types";
import { ChevronIcon, FolderIcon, GraphIcon, NoteIcon } from "./icons";

type Props = {
  roots: TreeNode[];
  selectedPath: string | null;
  selectedNodeId: string | null;
  onSelectFolder: (path: string) => void;
  onSelectRoot: () => void;
  onSelectNode: (id: string) => void;
};

export function TreeView(props: Props) {
  const rootSelected = props.selectedPath === null;
  return (
    <nav className="tree" aria-label="Knowledge base tree">
      <div className="pane-head">
        Tree
        <span className="count">{props.roots.length} roots</span>
      </div>
      <div className="tree-scroll">
        <button
          type="button"
          className={`tree-item${rootSelected ? " selected" : ""}`}
          style={{ paddingLeft: 8 }}
          onClick={props.onSelectRoot}
          aria-current={rootSelected ? "true" : undefined}
          title="Root — show the whole graph"
        >
          <span className="tw-chevron open">
            <ChevronIcon />
          </span>
          <span className="tw-icon root">
            <GraphIcon size={16} />
          </span>
          <span className="tw-label">Root</span>
        </button>
        {props.roots.length === 0 ? (
          <div className="tree-empty">No nodes yet.</div>
        ) : (
          props.roots.map((n) => (
            <TreeItem key={n.id} node={n} depth={1} {...props} />
          ))
        )}
      </div>
    </nav>
  );
}

function TreeItem({
  node,
  depth,
  selectedPath,
  selectedNodeId,
  onSelectFolder,
  onSelectNode,
}: { node: TreeNode; depth: number } & Omit<Props, "roots" | "onSelectRoot">) {
  const [open, setOpen] = useState(false);
  const isFolder = node.type === "folder";
  // Off-analysis = the node itself or an ancestor folder is excluded, so descendants of an
  // excluded folder dim too. Fall back to the own-flag if effective isn't supplied.
  const excluded = node.analysis_excluded_effective ?? node.analysis_excluded ?? false;
  const selected =
    (isFolder && node.path === selectedPath) ||
    (!isFolder && node.id === selectedNodeId);

  const onClick = () => {
    if (isFolder) {
      setOpen((o) => !o);
      onSelectFolder(node.path);
      onSelectNode(node.id);          // also open the folder's detail panel (exclude toggle, etc.)
    } else {
      onSelectNode(node.id);
    }
  };

  return (
    <>
      <button
        type="button"
        className={`tree-item${selected ? " selected" : ""}`}
        style={{ paddingLeft: 8 + depth * 15 }}
        onClick={onClick}
        aria-current={selected ? "true" : undefined}
        aria-expanded={isFolder ? open : undefined}
        title={node.path}
      >
        <span className={`tw-chevron${isFolder ? (open ? " open" : "") : " leaf"}`}>
          <ChevronIcon />
        </span>
        <span
          className={`tw-icon ${isFolder ? "folder" : "note"}${excluded ? " excluded" : ""}`}
        >
          {isFolder ? <FolderIcon /> : <NoteIcon />}
        </span>
        <span className="tw-label">{node.title || node.slug}</span>
      </button>
      {isFolder &&
        open &&
        node.children.map((c) => (
          <TreeItem
            key={c.id}
            node={c}
            depth={depth + 1}
            selectedPath={selectedPath}
            selectedNodeId={selectedNodeId}
            onSelectFolder={onSelectFolder}
            onSelectNode={onSelectNode}
          />
        ))}
    </>
  );
}
