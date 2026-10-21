import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const fetchPolicy = vi.fn();
vi.mock("../api/client", () => ({ fetchPolicy: () => fetchPolicy() }));

import { PolicyPanel } from "./PolicyPanel";

describe("PolicyPanel", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the source and one line per section for a loaded file", async () => {
    fetchPolicy.mockResolvedValue({
      source: "policy.toml", is_default: false, overlays: ["entities.examples.md"],
      policy: {
        decay: [{ name: "email", match: { tags: ["email"] }, half_life_days: 17, floor: 0.2 }],
        projects: { folder_label: "Projects", max_depth: 1, dormancy_half_life_days: 182.5 },
        importance: { recency_half_life_days: 8, recency_floor: 0.65, access_saturation: 10 },
        concepts: { goal_window_days: 14, data_recency_half_life_days: 14,
                    retrieval_recency_half_life_days: 7, recency_floor: 0.2 },
        taxonomy: { categories: [{ name: "person", kind: "person", description: "x" }], kind_seeds: {} },
        prompts: { overlay_dir: "prompts.local" },
      },
    });
    render(<PolicyPanel />);
    expect(await screen.findByText("policy.toml")).toBeInTheDocument();
    expect(screen.getByText(/1 rule: email/)).toBeInTheDocument();
    expect(screen.getByText(/Projects, depth 1, 182.5 d/)).toBeInTheDocument();
    expect(screen.getByText(/1 categor/)).toBeInTheDocument();
    expect(screen.getByText(/entities.examples.md/)).toBeInTheDocument();
  });

  it("says shipped defaults when there is no file", async () => {
    fetchPolicy.mockResolvedValue({
      source: null, is_default: true, overlays: [],
      policy: { decay: [], projects: null,
                importance: { recency_half_life_days: 8, recency_floor: 0.65, access_saturation: 10 },
                concepts: { goal_window_days: 14, data_recency_half_life_days: 14,
                            retrieval_recency_half_life_days: 7, recency_floor: 0.2 },
                taxonomy: { categories: [], kind_seeds: {} }, prompts: { overlay_dir: null } },
    });
    render(<PolicyPanel />);
    expect(await screen.findByText(/shipped defaults/)).toBeInTheDocument();
    expect(screen.getByText(/no rules/)).toBeInTheDocument();
    expect(screen.getByText(/not configured/)).toBeInTheDocument();
  });
});
