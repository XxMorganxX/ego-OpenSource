import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const styles = readFileSync("src/index.css", "utf8");

function balancedBody(source, openBrace) {
  let depth = 0;
  for (let index = openBrace; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) {
      return { body: source.slice(openBrace + 1, index), end: index + 1 };
    }
  }
  throw new Error("Unbalanced CSS block");
}

function mediaBlocks(source, query) {
  const marker = `@media ${query}`;
  const blocks = [];
  let cursor = 0;
  while (cursor < source.length) {
    const start = source.indexOf(marker, cursor);
    if (start === -1) break;
    const openBrace = source.indexOf("{", start + marker.length);
    const block = balancedBody(source, openBrace);
    blocks.push(block.body);
    cursor = block.end;
  }
  return blocks;
}

function declarations(body) {
  return Object.fromEntries(
    body.split(";").flatMap((entry) => {
      const colon = entry.indexOf(":");
      if (colon === -1) return [];
      return [[entry.slice(0, colon).trim(), entry.slice(colon + 1).trim()]];
    }),
  );
}

function rulesIn(block) {
  const rules = new Map();
  const source = block.replace(/\/\*[\s\S]*?\*\//g, "");
  let cursor = 0;
  while (cursor < source.length) {
    const openBrace = source.indexOf("{", cursor);
    if (openBrace === -1) break;
    const selector = source.slice(cursor, openBrace).trim();
    const rule = balancedBody(source, openBrace);
    rules.set(selector, declarations(rule.body));
    cursor = rule.end;
  }
  return rules;
}

describe("mobile layout", () => {
  const mobileRules = mediaBlocks(styles, "(max-width: 760px)")
    .map(rulesIn)
    .find((rules) => rules.has(".explorer"));

  it("keeps the explorer wrapper layout-transparent on desktop", () => {
    expect(rulesIn(styles).get(".explorer")).toMatchObject({ display: "contents" });
  });

  it("turns the explorer into a closed-by-default drawer over the graph", () => {
    expect(mobileRules, "mobile 760px media block").toBeDefined();
    expect([...mobileRules.keys()].some((selector) => selector.includes(":has("))).toBe(false);

    const workspace = rulesIn(styles).get(".workspace");
    expect(workspace?.position).toBe("relative");

    const drawer = mobileRules.get(".explorer");
    const closed = mobileRules.get(".explorer.closed");
    const scrim = mobileRules.get(".explorer-scrim");
    expect(drawer).toMatchObject({ position: "absolute", left: "0" });
    expect(closed).toMatchObject({ transform: "translateX(-100%)", visibility: "hidden" });
    // The drawer paints above its scrim, which paints above the graph overlays.
    expect(Number(drawer["z-index"])).toBeGreaterThan(Number(scrim["z-index"]));
    expect(Number(scrim["z-index"])).toBeGreaterThan(3);
  });

  it("overlays the whole workspace with the detail panel", () => {
    const detail = mobileRules.get(".workspace > .detail");
    expect(detail).toMatchObject({
      position: "absolute",
      inset: "0",
      width: "auto",
      "max-width": "none",
    });
    // Above the drawer, so a drawer selection reveals the opened detail.
    expect(Number(detail["z-index"])).toBeGreaterThan(
      Number(mobileRules.get(".explorer")["z-index"]),
    );

    const head = mobileRules.get(".detail .concept-head");
    const actions = mobileRules.get(".detail .concept-head .icon-btn");
    expect(head).toMatchObject({ "flex-wrap": "wrap", gap: "6px" });
    expect(actions).toMatchObject({ width: "26px", height: "26px", flex: "0 0 26px" });
  });
});
