import { describe, expect, it } from "vitest";
import { agentLastSeenLabel, isOwner, UNKNOWN_ME } from "./identity";

describe("identity helpers", () => {
  it("treats an absent /api/me as the owner (old backends)", () => {
    expect(isOwner(null)).toBe(true);
    expect(isOwner({ email: "b@x", tenant: "b", is_owner: false })).toBe(false);
    expect(isOwner({ email: "m@x", tenant: "owner", is_owner: true })).toBe(true);
  });
  it("labels the agent's last contact", () => {
    const now = new Date("2026-10-09T12:00:00Z");
    expect(agentLastSeenLabel(null, now)).toBeNull();
    expect(agentLastSeenLabel("2026-10-09T11:59:30Z", now)).toBe("agent seen just now");
    expect(agentLastSeenLabel("2026-10-09T10:00:00Z", now)).toBe("agent seen 2 h ago");
    expect(agentLastSeenLabel("2026-10-01T10:00:00Z", now)).toBe("agent seen 8 d ago");
  });
  it("says nothing for an unparseable timestamp rather than 'NaN d ago'", () => {
    expect(agentLastSeenLabel("garbage", new Date("2026-10-09T12:00:00Z"))).toBeNull();
  });
  it("treats an unknown identity as a non-owner", () => {
    expect(isOwner(UNKNOWN_ME)).toBe(false);
  });
});
