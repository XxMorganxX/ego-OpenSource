import { describe, expect, it } from "vitest";

import type { EntityGraphData } from "../types";
import { categoryNameForNode } from "./entities";

const data: EntityGraphData = {
  nodes: [
    { id: "root", path: "entities", parent_id: null, type: "folder", slug: "entities", title: null, tags: [], importance: 1, role: "root", entity_type: null },
    { id: "p", path: "entities.people", parent_id: "root", type: "entity", slug: "people", title: "people", tags: [], importance: 1, role: "class", entity_type: "class" },
    { id: "ds", path: "entities.data_stores", parent_id: "root", type: "entity", slug: "data_stores", title: "data stores", tags: [], importance: 1, role: "class", entity_type: "class" },
    { id: "m", path: "entities.people.avery", parent_id: "p", type: "entity", slug: "avery", title: "Avery", tags: [], importance: 1, role: "instance", entity_type: "people" },
  ],
  links: [],
};

describe("categoryNameForNode", () => {
  it("returns the canonical name (title) for a multi-word class, not the slug", () => {
    expect(categoryNameForNode(data, "ds")).toBe("data stores");
  });
  it("returns the name for a single-word class", () => {
    expect(categoryNameForNode(data, "p")).toBe("people");
  });
  it("returns undefined for an instance node", () => {
    expect(categoryNameForNode(data, "m")).toBeUndefined();
  });
  it("returns undefined when entityData is null or the node is absent", () => {
    expect(categoryNameForNode(null, "p")).toBeUndefined();
    expect(categoryNameForNode(data, "nope")).toBeUndefined();
  });
});
