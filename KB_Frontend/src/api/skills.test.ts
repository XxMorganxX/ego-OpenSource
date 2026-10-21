import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchSkill, fetchSkillFile, fetchSkillLibraryUsage, fetchSkills, fetchSkillUsage,
} from "./client";

const fetchMock = vi.fn();
const ok = { ok: true, status: 200, json: () => Promise.resolve({}) } as unknown as Response;

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(ok);
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("skills client", () => {
  it("omits empty search params and sends the rest", async () => {
    await fetchSkills();
    expect(fetchMock).toHaveBeenLastCalledWith("/api/skills");
    await fetchSkills({ q: "deploy checklist", cursor: "NTA=", archived: true });
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/skills?q=deploy+checklist&cursor=NTA%3D&archived=true");
  });

  it("asks for the current revision unless one is given", async () => {
    await fetchSkill("deploy-checklist");
    expect(fetchMock).toHaveBeenLastCalledWith("/api/skills/deploy-checklist");
    await fetchSkill("deploy-checklist", 2);
    expect(fetchMock).toHaveBeenLastCalledWith("/api/skills/deploy-checklist?revision=2");
  });

  it("encodes each file path segment but keeps the slashes", async () => {
    await fetchSkillFile("deploy-checklist", "references/deploy notes#1.md", 3);
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/skills/deploy-checklist/files/references/deploy%20notes%231.md?revision=3");
  });

  it("routes library usage to /api/skill-usage, never /api/skills/usage", async () => {
    await fetchSkillLibraryUsage(30, "UTC");
    expect(fetchMock).toHaveBeenLastCalledWith("/api/skill-usage?days=30&tz=UTC");
    await fetchSkillUsage("usage", 7, "Asia/Tokyo");
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/skills/usage/usage?days=7&tz=Asia%2FTokyo");
  });

  it("rejects a 404 with the status and the stable code", async () => {
    fetchMock.mockResolvedValue({
      ok: false, status: 404, text: () => Promise.resolve('{"detail":"not_found: x"}'),
    } as unknown as Response);
    await expect(fetchSkill("x")).rejects.toThrow(/^404: .*not_found/);
  });
});
