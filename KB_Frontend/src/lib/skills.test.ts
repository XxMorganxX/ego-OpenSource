import { describe, expect, it } from "vitest";

import {
  firstLine, fmtAgo, fmtBytes, orderedFiles, parseRev, skillMiss, splitLines, surfacedLabel,
} from "./skills";

describe("skills helpers", () => {
  it("puts SKILL.md first and keeps the manifest order otherwise", () => {
    const files = [
      { path: "AGENTS.md", bytes: 1, content_hash: "a" },
      { path: "SKILL.md", bytes: 1, content_hash: "b" },
      { path: "references/x.md", bytes: 1, content_hash: "c" },
    ];
    expect(orderedFiles(files).map((f) => f.path)).toEqual(
      ["SKILL.md", "AGENTS.md", "references/x.md"]);
  });

  it("reads ?rev= as a positive integer or current", () => {
    expect(parseRev("3")).toBe(3);
    for (const bad of ["", "0", "-1", "abc", "1.5", "07"]) expect(parseRev(bad)).toBeNull();
  });

  it("classifies a failed fetch by its stable code", () => {
    expect(skillMiss(new Error('404: {"detail":"not_found: x"}'))).toBe("skill");
    expect(skillMiss(new Error('404: {"detail":"revision_not_found: x"}'))).toBe("revision");
    expect(skillMiss(new Error('404: {"detail":"file_not_found: x"}'))).toBe("file");
    expect(skillMiss(new Error("500: boom"))).toBe("other");
  });

  it("says when search surfacing was not recorded for the whole window", () => {
    const since = "2026-09-19T00:00:00+00:00";
    expect(surfacedLabel(0, null, since)).toBe("search surfacing not recorded yet");
    expect(surfacedLabel(4, "2026-09-01T00:00:00+00:00", since)).toBe("surfaced in 4 searches");
    expect(surfacedLabel(1, "2026-10-01T00:00:00+00:00", since)).toMatch(
      /^surfaced in 1 search \(recorded since .+\)$/);
  });

  it("formats small helpers", () => {
    expect(firstLine("Steps before a deploy.\nMore.")).toBe("Steps before a deploy.");
    expect(fmtBytes(512)).toBe("512 B");
    expect(fmtBytes(2048)).toBe("2.0 KB");
    expect(fmtBytes(200 * 1024)).toBe("200 KB");
  });

  it("formats a compact relative time", () => {
    const now = Date.parse("2026-10-19T12:00:00Z");
    expect(fmtAgo(null, now)).toBe("—");
    expect(fmtAgo("2026-10-19T11:59:30Z", now)).toBe("just now");
    expect(fmtAgo("2026-10-19T11:15:00Z", now)).toBe("45m ago");
    expect(fmtAgo("2026-10-19T07:00:00Z", now)).toBe("5h ago");
    expect(fmtAgo("2026-10-16T12:00:00Z", now)).toBe("3d ago");
    expect(fmtAgo("2026-07-01T12:00:00Z", now)).not.toMatch(/ago/);
  });

  it("splits a file into lines without a phantom last line", () => {
    expect(splitLines("a\nb\n")).toEqual(["a", "b"]);
    expect(splitLines("a\n\nb")).toEqual(["a", "", "b"]);
    expect(splitLines("")).toEqual([""]);
  });
});
