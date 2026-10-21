import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { LABELS } from "../test/replayFixtures";
import { ReplayFusion, type FusionLists } from "./ReplayFusion";

// The CRAWL fixture's fusion: x was admitted by the crawl, a/b/c came from the flat pool.
const LISTS: FusionLists = { flat: ["b", "a", "c"], sim: ["b", "x", "c"], admit: [], corrob: [] };
const FINAL = ["b", "x", "a", "c"];

const setup = (over: Partial<Parameters<typeof ReplayFusion>[0]> = {}) => {
  const onHover = vi.fn();
  const utils = render(
    <ReplayFusion lists={LISTS} final={FINAL} limit={2} labels={LABELS} hover={null}
                  motion={false} onHover={onHover} {...over} />);
  const column = (name: string) => utils.getByRole("list", { name });
  return { ...utils, onHover, column };
};

describe("ReplayFusion", () => {
  it("shows the four fused lists and the final order, in that order", () => {
    setup();
    const region = screen.getByRole("region", { name: "final merge lists" });
    expect(within(region).getAllByRole("list").map((l) => l.getAttribute("aria-label")))
      .toEqual(["flat", "sim", "admit", "corrob", "final"]);
  });

  it("ranks each list's rows and names them", () => {
    const { column } = setup();
    const rows = within(column("sim")).getAllByRole("listitem");
    expect(rows.map((r) => r.textContent)).toEqual(["1Beta", "2Crawled", "3Gamma"]);
  });

  it("says why a list is empty", () => {
    const { column } = setup();
    expect(column("admit")).toHaveTextContent("empty: deterministic judge");
    expect(column("corrob")).toHaveTextContent("empty: no bridge parents");
  });

  it("marks the rows that made the final page in every column", () => {
    const { container } = setup();
    const page = (id: string) => [...container.querySelectorAll(`[data-id="${id}"]`)]
      .map((el) => el.classList.contains("page"));
    expect(page("b")).toEqual([true, true, true]);   // flat, sim, final
    expect(page("x")).toEqual([true, true]);         // sim, final
    expect(page("a")).toEqual([false, false]);       // flat, final -- rank 3 of limit 2
  });

  it("shows the top ten of a long list and counts the rest", () => {
    const long = Array.from({ length: 15 }, (_, i) => `m${i}`);
    const { column } = setup({ lists: { ...LISTS, sim: long } });
    const items = within(column("sim")).getAllByRole("listitem");
    expect(items).toHaveLength(11);
    expect(items[10]).toHaveTextContent("+5 more");
  });

  it("reports hover by memory id and highlights that memory in every column", () => {
    const { container, onHover, column, rerender } = setup();
    fireEvent.mouseEnter(within(column("sim")).getByText("Crawled"));
    expect(onHover).toHaveBeenLastCalledWith("x");
    rerender(<ReplayFusion lists={LISTS} final={FINAL} limit={2} labels={LABELS} hover="b"
                           motion={false} onHover={onHover} />);
    expect([...container.querySelectorAll('[data-id="b"]')]
      .every((el) => el.classList.contains("hover"))).toBe(true);
  });
});
