import type { Me } from "../types";

/** /api/me could not be read (5xx, 401/403, network error, timeout, malformed answer):
 *  identity unknown. A non-owner, so owner-only chrome fails closed. */
export const UNKNOWN_ME: Me = Object.freeze({ email: "", tenant: "", is_owner: false });

/** A JSON body shaped like GET /api/me's answer. */
export function isMe(body: unknown): body is Me {
  if (typeof body !== "object" || body === null) return false;
  const b = body as Record<string, unknown>;
  return typeof b.email === "string" && typeof b.tenant === "string" && typeof b.is_owner === "boolean";
}

/** null means a backend without /api/me (pre-tenancy), which counts as the owner so the
 *  dashboard renders exactly as before; fetchMe returns null ONLY for that case. Every
 *  other failure is UNKNOWN_ME, a non-owner. */
export function isOwner(me: Me | null): boolean {
  return me === null ? true : me.is_owner;
}

/** Human label for when this tenant's kb-agent last reached the gateway; null = never. */
export function agentLastSeenLabel(iso: string | null, now: Date = new Date()): string | null {
  if (!iso) return null;
  const seen = new Date(iso).getTime();
  if (Number.isNaN(seen)) return null; // unparseable timestamp: say nothing, not "NaN d ago"
  const secs = Math.max(0, (now.getTime() - seen) / 1000);
  if (secs < 90) return "agent seen just now";
  if (secs < 3600) return `agent seen ${Math.round(secs / 60)} min ago`;
  if (secs < 86400) return `agent seen ${Math.round(secs / 3600)} h ago`;
  return `agent seen ${Math.round(secs / 86400)} d ago`;
}
