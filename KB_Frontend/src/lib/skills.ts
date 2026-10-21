import { setParams } from "./url";
import type { SkillFileEntry, UsageDays } from "../types";

export const SKILL_FILE = "SKILL.md";
export const USAGE_DAYS: UsageDays[] = [7, 30, 90];

/** SKILL.md first, then the manifest's own (code-point) order. */
export function orderedFiles(files: SkillFileEntry[]): SkillFileEntry[] {
  return [
    ...files.filter((f) => f.path === SKILL_FILE),
    ...files.filter((f) => f.path !== SKILL_FILE),
  ];
}

export function firstLine(text: string): string {
  return text.split("\n", 1)[0].trim();
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
}

export function fmtWhen(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

/** Compact relative time ("3h ago", "2d ago"); a calendar date past ~5 weeks. The full
 * timestamp belongs in a title attribute (fmtWhen) next to it. */
export function fmtAgo(iso: string | null, now: number = Date.now()): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  if (d < 36) return `${d}d ago`;
  return new Date(t).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** A file's lines for the line-numbered viewer. A trailing newline ends the last line;
 * it does not open an empty one. */
export function splitLines(text: string): string[] {
  const lines = text.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/** What a failed skill fetch means, from asJson's "<status>: <body>" message. The
 * longer codes are checked first: each of them contains "not_found". */
export type SkillMiss = "skill" | "revision" | "file" | "other";
export function skillMiss(err: unknown): SkillMiss {
  const msg = err instanceof Error ? err.message : String(err);
  if (!msg.startsWith("404")) return "other";
  if (msg.includes("revision_not_found")) return "revision";
  if (msg.includes("file_not_found")) return "file";
  return msg.includes("not_found") ? "skill" : "other";
}

/** `?rev=` is a positive integer without a leading zero; anything else means current. */
export function parseRev(raw: string): number | null {
  return /^[1-9][0-9]{0,8}$/.test(raw) ? Number(raw) : null;
}

/** "surfaced in N searches", honest about searches logged before surfacing was
 * recorded (they carry no skill names, so they cannot count). */
export function surfacedLabel(surfaced: number, surfacedSince: string | null, since: string): string {
  if (surfacedSince === null) return "search surfacing not recorded yet";
  const base = `surfaced in ${surfaced} ${surfaced === 1 ? "search" : "searches"}`;
  return new Date(surfacedSince) > new Date(since)
    ? `${base} (recorded since ${new Date(surfacedSince).toLocaleDateString()})`
    : base;
}

/** Jump to one request on the Retrieval page (LiveTraces reads ?turn=). */
export function openTurn(turnId: number): void {
  setParams({ view: "retrieval", turn: String(turnId), skill: null, rev: null });
}
