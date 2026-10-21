import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

// The dashboard is local-first: loading it must not contact any third party (fonts
// are bundled via @fontsource, see main.tsx). Any absolute http(s) URL in the shell
// page is an outbound request on every load.
const html = readFileSync("index.html", "utf8");

describe("index.html", () => {
  it("references no external http(s) origin", () => {
    expect(html.match(/https?:\/\/[^\s"'<>)]+/gi) ?? []).toEqual([]);
  });

  it("references no protocol-relative origin", () => {
    expect(html.match(/(?:href|src)\s*=\s*["']\/\//gi) ?? []).toEqual([]);
  });
});
