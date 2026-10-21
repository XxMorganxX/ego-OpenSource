import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { computeTipShift, DEFS, Dt, GLOSSARY_ORDER } from "./defs";

describe("DEFS", () => {
  it("every entry has non-empty label, short, and long copy", () => {
    for (const [term, d] of Object.entries(DEFS)) {
      expect(d.label.trim(), term).not.toBe("");
      expect(d.short.trim(), term).not.toBe("");
      expect(d.long.trim(), term).not.toBe("");
    }
  });

  it("confidence terms match the engine's five levels exactly", () => {
    const levels = ["none", "weak", "strong", "keyword", "scoped"];
    for (const l of levels) {
      expect(DEFS[`conf_${l}` as keyof typeof DEFS]).toBeDefined();
    }
    expect(Object.keys(DEFS).filter((k) => k.startsWith("conf_"))).toHaveLength(5);
  });

  it("glossary order covers every term exactly once", () => {
    expect([...GLOSSARY_ORDER].sort()).toEqual(Object.keys(DEFS).sort());
  });
});

describe("Dt", () => {
  it("ties the tooltip to the label via aria-describedby", () => {
    render(<Dt term="mrr">MRR</Dt>);
    const wrapper = screen.getByText("MRR").closest(".dt")!;
    const tip = wrapper.querySelector('[role="tooltip"]')!;
    expect(wrapper.getAttribute("aria-describedby")).toBe(tip.id);
    expect(tip.textContent).toBe(DEFS.mrr.short);
    expect(wrapper.getAttribute("tabindex")).toBe("0");
  });
});

describe("computeTipShift", () => {
  it("returns 0 when the tip fits centered", () => {
    expect(computeTipShift(500, 260, 1024)).toBe(0);
  });

  it("shifts right when the tip would overflow the left edge", () => {
    // min center = 8 + 130 = 138; label center 40 -> +98
    expect(computeTipShift(40, 260, 1024)).toBe(98);
  });

  it("shifts left when the tip would overflow the right edge", () => {
    // max center = 1024 - 8 - 130 = 886; label center 1000 -> -114
    expect(computeTipShift(1000, 260, 1024)).toBe(-114);
  });

  it("centers in the viewport when the tip is wider than it", () => {
    expect(computeTipShift(350, 500, 400)).toBe(-150);
  });
});

describe("Dt viewport clamping", () => {
  it("clamps the tooltip on show and resets on hide", async () => {
    const spy = vi
      .spyOn(Element.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: Element) {
        const isTip = (this as HTMLElement).classList?.contains("dt-tip");
        return {
          left: isTip ? -90 : 20, width: isTip ? 260 : 40,
          top: 100, right: 0, bottom: 120, height: 20, x: 20, y: 100,
          toJSON: () => ({}),
        } as DOMRect;
      });
    render(<Dt term="mrr">MRR</Dt>);
    const wrapper = screen.getByText("MRR").closest(".dt")!;
    await userEvent.hover(wrapper);
    const tip = wrapper.querySelector<HTMLElement>(".dt-tip")!;
    expect(tip.className).toContain("show");
    // wrapper center = 20 + 40/2 = 40; jsdom viewport 1024 -> shift +98
    expect(tip.style.transform).toBe("translateX(calc(-50% + 98px))");
    await userEvent.unhover(wrapper);
    expect(tip.className).not.toContain("show");
    spy.mockRestore();
  });
});
