import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { EntityGraphData, EntityNodeMeta } from "../types";
import { EntityNav } from "./EntityNav";

function ent(over: Partial<EntityNodeMeta> & { id: string; role: EntityNodeMeta["role"] }): EntityNodeMeta {
  return {
    path: over.id, parent_id: null, type: over.role === "root" ? "folder" : "entity",
    slug: over.id, title: null, tags: [], importance: 1, entity_type: null, ...over,
  };
}

const data: EntityGraphData = {
  nodes: [
    ent({ id: "root", role: "root" }),
    ent({ id: "people", role: "class", parent_id: "root", entity_type: "class" }),
    ent({ id: "places", role: "class", parent_id: "root", entity_type: "class" }),
    ent({ id: "avery", role: "instance", parent_id: "root", entity_type: "instance" }),
  ],
  // category membership is a link now (class -> instance), not parent_id
  links: [{ source: "people", target: "avery", kind: "containment" }],
};

function renderNav(overrides: Partial<React.ComponentProps<typeof EntityNav>> = {}) {
  return render(
    <EntityNav
      entityData={data}
      selectedClass={null}
      selectedNodeId={null}
      onSelectClass={vi.fn()}
      onSelectNode={vi.fn()}
      {...overrides}
    />,
  );
}

describe("EntityNav", () => {
  it("lists classes with their instance counts", () => {
    renderNav();
    expect(screen.getByText("All entities")).toBeInTheDocument();
    expect(screen.getByText("people")).toBeInTheDocument();
    expect(screen.getByText("places")).toBeInTheDocument();
    // "people" has one instance (avery); "places" has none
    expect(screen.getByText("1")).toBeInTheDocument();
  });

  it("focuses a class and opens its detail on click, then clears focus via All entities", async () => {
    const onSelectClass = vi.fn();
    const onSelectNode = vi.fn();
    renderNav({ onSelectClass, onSelectNode });
    await userEvent.click(screen.getByText("people"));
    expect(onSelectClass).toHaveBeenCalledWith("people");
    expect(onSelectNode).toHaveBeenCalledWith("people");
    await userEvent.click(screen.getByText("All entities"));
    expect(onSelectClass).toHaveBeenCalledWith(null);
  });

  it("expands a class to reveal instances and scopes an instance click to the class it's listed under", async () => {
    const onSelectClass = vi.fn();
    const onSelectNode = vi.fn();
    renderNav({ onSelectClass, onSelectNode });
    expect(screen.queryByText("avery")).toBeNull(); // hidden until expanded
    await userEvent.click(screen.getByText("people"));
    expect(screen.getByText("avery")).toBeInTheDocument();
    // Isolate the instance click from the class-expand click above, so the assertion can't
    // be satisfied by the earlier onSelectClass("people").
    onSelectClass.mockClear();
    await userEvent.click(screen.getByText("avery"));
    // An instance click scopes the graph to the CLASS it's listed under — derived from the
    // membership link, NOT inst.parent_id (which is the flat instances root, "root" here).
    expect(onSelectClass).toHaveBeenCalledWith("people");
    expect(onSelectClass).not.toHaveBeenCalledWith("root");
    expect(onSelectNode).toHaveBeenCalledWith("avery");
  });

  it("shows a loading hint until the entity graph arrives", () => {
    renderNav({ entityData: null });
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });
});
