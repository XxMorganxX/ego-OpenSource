import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { TreeNode } from "../types";
import { TreeView } from "./TreeView";

const roots: TreeNode[] = [
  {
    id: "1", path: "projects", slug: "projects", parent_id: null, type: "folder",
    title: null, tags: [], importance: 1,
    children: [
      { id: "2", path: "projects.note", slug: "note", parent_id: "1", type: "note",
        title: "Note", tags: [], importance: 1, children: [] },
    ],
  },
];

describe("TreeView", () => {
  it("selects a folder and reveals its children on click", async () => {
    const onFolder = vi.fn();
    render(<TreeView roots={roots} selectedPath={null} selectedNodeId={null}
                     onSelectFolder={onFolder} onSelectRoot={vi.fn()} onSelectNode={vi.fn()} />);
    // child hidden until parent expanded
    expect(screen.queryByText("Note")).toBeNull();
    await userEvent.click(screen.getByText("projects"));
    expect(onFolder).toHaveBeenCalledWith("projects");
    expect(screen.getByText("Note")).toBeInTheDocument();
  });

  it("selects a note via its callback", async () => {
    const onNode = vi.fn();
    render(<TreeView roots={roots} selectedPath={null} selectedNodeId={null}
                     onSelectFolder={vi.fn()} onSelectRoot={vi.fn()} onSelectNode={onNode} />);
    await userEvent.click(screen.getByText("projects")); // expand
    await userEvent.click(screen.getByText("Note"));
    expect(onNode).toHaveBeenCalledWith("2");
  });

  it("labels nodes by title so a rename shows in the explorer, falling back to slug", async () => {
    render(<TreeView roots={roots} selectedPath={null} selectedNodeId={null}
                     onSelectFolder={vi.fn()} onSelectRoot={vi.fn()} onSelectNode={vi.fn()} />);
    // folder has no title → shows its slug
    expect(screen.getByText("projects")).toBeInTheDocument();
    await userEvent.click(screen.getByText("projects"));
    // note has title "Note" (slug is "note") → the title is what's shown
    expect(screen.getByText("Note")).toBeInTheDocument();
    expect(screen.queryByText("note")).toBeNull();
  });

  it("marks a folder removed from analysis with the excluded icon modifier", () => {
    const excluded: TreeNode[] = [
      { id: "1", path: "scratch", slug: "scratch", parent_id: null, type: "folder",
        title: null, tags: [], importance: 1, analysis_excluded: true, children: [] },
    ];
    const { container } = render(
      <TreeView roots={excluded} selectedPath={null} selectedNodeId={null}
                onSelectFolder={vi.fn()} onSelectRoot={vi.fn()} onSelectNode={vi.fn()} />,
    );
    expect(container.querySelector(".tw-icon.folder.excluded")).not.toBeNull();
  });

  it("dims an inherited descendant of an excluded folder (effective, own-flag unset)", async () => {
    const inherited: TreeNode[] = [
      { id: "1", path: "scratch", slug: "scratch", parent_id: null, type: "folder",
        title: null, tags: [], importance: 1,
        analysis_excluded: true, analysis_excluded_effective: true,
        children: [
          { id: "2", path: "scratch.note", slug: "note", parent_id: "1", type: "note",
            title: "Note", tags: [], importance: 1,
            analysis_excluded: false, analysis_excluded_effective: true, children: [] },
        ] },
    ];
    const { container } = render(
      <TreeView roots={inherited} selectedPath={null} selectedNodeId={null}
                onSelectFolder={vi.fn()} onSelectRoot={vi.fn()} onSelectNode={vi.fn()} />,
    );
    await userEvent.click(screen.getByText("scratch")); // expand to reveal the child
    // both the folder and its inherited child carry the excluded modifier
    expect(container.querySelector(".tw-icon.folder.excluded")).not.toBeNull();
    expect(container.querySelector(".tw-icon.note.excluded")).not.toBeNull();
  });

  it("does not mark an analyzed folder as excluded", () => {
    const { container } = render(
      <TreeView roots={roots} selectedPath={null} selectedNodeId={null}
                onSelectFolder={vi.fn()} onSelectRoot={vi.fn()} onSelectNode={vi.fn()} />,
    );
    expect(container.querySelector(".tw-icon.excluded")).toBeNull();
  });

  it("returns to the root view when the Root row is clicked", async () => {
    const onRoot = vi.fn();
    render(<TreeView roots={roots} selectedPath="projects" selectedNodeId={null}
                     onSelectFolder={vi.fn()} onSelectRoot={onRoot} onSelectNode={vi.fn()} />);
    await userEvent.click(screen.getByText("Root"));
    expect(onRoot).toHaveBeenCalledTimes(1);
  });

  it("opens the detail panel for a folder by reporting its node id on click", async () => {
    const onFolder = vi.fn();
    const onNode = vi.fn();
    render(<TreeView roots={roots} selectedPath={null} selectedNodeId={null}
                     onSelectFolder={onFolder} onSelectRoot={vi.fn()} onSelectNode={onNode} />);
    await userEvent.click(screen.getByText("projects"));
    expect(onFolder).toHaveBeenCalledWith("projects");
    expect(onNode).toHaveBeenCalledWith("1");          // folder's id -> DetailPanel opens
  });
});
