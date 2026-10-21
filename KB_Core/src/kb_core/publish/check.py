"""kb-check-publishable: layer-2 guard. Scans manifest-included TRACKED files
for denylist patterns and structural private-data shapes (structural.py).
Runs in private-repo CI on every push; kb-publish (layer 3) runs the same
detectors against the assembled export tree.

Two modes, because the same exported command runs in two places:

- default: the public repo and every fork have no publish.deny.toml (it never
  leaves private), so a missing deny file is a SKIP with exit 0 — printed
  loudly as "not a personal-data scan" so nobody mistakes it for a pass.
- --strict (or KB_PUBLISH_GUARD_REQUIRED=1, set only on the private repo):
  fail CLOSED. Missing deny file, zero patterns, a selection below
  --min-selected, or a denylist that no longer matches every out-of-band
  canary (PUBLISH_DENY_CANARIES) is a refusal: a commit that quietly drops
  patterns turns the guard red. Canaries are reported as
  counts only: they ARE the identifiers being concealed. So is a missing
  private/publish.paths.lock, or any selected path it doesn't approve.

The deny file's `[[allow]]` entries (exact literals, deny.load_allows) are
honoured in file content and validated on load: an unsafe entry is a refusal
in both modes. A canary only counts as matched if no allow literal excuses it.

In either mode a deny file that exists but holds zero patterns, a manifest
that selects nothing, or a lockfile that doesn't parse is refused — none is
ever a meaningful "clean". The lockfile diff is printed in both modes; outside
strict it is a note (the day-to-day job isn't the publish gate — kb-publish
refuses the same paths itself).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kb_core.publish import deny, lock, manifest, structural


def _tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout.decode()
    return [p for p in out.split("\0") if p]


@dataclass
class CheckReport:
    hits: list[deny.Hit] = field(default_factory=list)
    warnings: list[deny.Hit] = field(default_factory=list)
    selected: int = 0
    patterns: list = field(default_factory=list)
    allows: tuple = ()                   # deny.Allow entries ([[allow]])
    lock: lock.LockDiff | None = None    # None: no lockfile in this checkout


def inspect(root: Path) -> CheckReport:
    """Everything the guard knows about the tree. Empty report if no deny file
    (the caller decides whether that is a skip or a refusal).

    Raises lock.LockError if the checkout's lockfile doesn't parse, and
    deny.DenyConfigError if an [[allow]] entry is unsafe."""
    deny_path = root / "publish.deny.toml"
    if not deny_path.exists():
        return CheckReport()
    patterns = deny.load_patterns(deny_path)
    allows = deny.load_allows(deny_path, patterns)
    m = manifest.load_manifest(root / "publish.manifest.toml")
    approved = lock.load(root)
    selected = manifest.select(_tracked_files(root), m)
    rep = CheckReport(selected=len(selected), patterns=patterns, allows=allows)
    if approved is not None:
        rep.lock = lock.diff(approved, lock.snapshot(root, selected))
    rep.hits.extend(
        deny.Hit(rel, 0, "media not in [export] allow_media", "(binary — not scannable)")
        for rel in manifest.unapproved_media(selected, m)
    )
    # A non-UTF-8 file passes only with its exact bytes hash-pinned.
    scan = structural.scan_files(root, selected, patterns,
                                 is_pinned=lock.pinned_hook(approved),
                                 allow_binary=m.allow_binary, allows=allows)
    rep.hits.extend(scan.hits)
    rep.warnings.extend(scan.warnings)
    return rep


def run_check(root: Path) -> list[deny.Hit]:
    return inspect(root).hits


def _print_warnings(warnings: list[deny.Hit]) -> None:
    if not warnings:
        return
    print(f"kb-check-publishable: {len(warnings)} lint warning(s) (non-blocking — "
          "check these aren't copied from the real box):")
    for w in warnings:
        print(f"  {w.path}:{w.line_no}  [{w.pattern}]  {w.line}")


# Today's real export is ~700 paths; a selection under this means the manifest
# was gutted (or a glob typo'd), not that the project shrank. Lowering it is a
# gate-file change like any other.
STRICT_MIN_SELECTED = 400


def parse_canaries(raw: str | None) -> list[str]:
    """PUBLISH_DENY_CANARIES: newline- and/or comma-separated identifiers."""
    if not raw:
        return []
    return [c.strip() for c in re.split(r"[,\n]", raw) if c.strip()]


def canaries_matched(patterns: list[re.Pattern], canaries: list[str],
                     allows: tuple[deny.Allow, ...] = ()) -> int:
    """How many canaries at least one deny pattern catches. Never returns which.

    With `allows`, every [[allow]] entry is honoured whatever its path scope,
    exactly as the content gates honour it (deny.scan_text): a canary counts
    only if some match in it is NOT entirely inside an allowed occurrence. So
    a canary that is (or wraps) an allowed literal counts as unmatched and
    strict CI refuses — an allow can never be what hides a canary. A canary
    that merely appears INSIDE an allowed literal (the owner's handle inside
    the repo URL) still counts: the allow excuses that one exact literal, never
    the canary on its own or next to it.
    """
    return sum(1 for c in canaries if deny.scan_text("canary", c, patterns, allows=allows))


def _refuse(msg: str) -> int:
    print(f"kb-check-publishable: REFUSED — {msg}")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb-check-publishable")
    ap.add_argument("--strict", action="store_true",
                    help="private-repo mode: fail closed on a missing/empty denylist, a "
                         "short selection, or an unmatched canary (also implied by "
                         "KB_PUBLISH_GUARD_REQUIRED=1)")
    ap.add_argument("--min-selected", type=int, default=STRICT_MIN_SELECTED,
                    help=f"strict: refuse if fewer paths are selected "
                         f"(default {STRICT_MIN_SELECTED})")
    args = ap.parse_args(argv)
    strict = args.strict or os.environ.get("KB_PUBLISH_GUARD_REQUIRED") == "1"
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, check=True
        ).stdout.decode().strip()
    )
    if not (root / "publish.deny.toml").exists():
        if strict:
            return _refuse("no publish.deny.toml (strict mode never skips)")
        print("kb-check-publishable: no publish.deny.toml — SKIPPED. This is NOT a "
              "personal-data scan (the denylist is private; public repo and forks "
              "have none). The private repo runs this with --strict.")
        return 0
    try:
        rep = inspect(root)
    except manifest.ManifestError as e:
        return _refuse(f"invalid publish.manifest.toml: {e}")
    except lock.LockError as e:
        return _refuse(f"invalid {lock.LOCK_REL}: {e} — restore it from git, then "
                       "re-run `kb-publish approve`")
    except deny.DenyConfigError as e:
        return _refuse(f"invalid publish.deny.toml: {e}")
    print(f"kb-check-publishable: {len(rep.patterns)} deny pattern(s), "
          f"{len(rep.allows)} exact [[allow]] literal(s), "
          f"{rep.selected} selected path(s){' [strict]' if strict else ''}")
    if not rep.patterns:
        return _refuse("publish.deny.toml has zero patterns")
    if rep.selected == 0:
        return _refuse("the manifest selects zero tracked paths")
    if strict and rep.selected < args.min_selected:
        return _refuse(f"only {rep.selected} path(s) selected, below the floor of "
                       f"{args.min_selected} — is the manifest gutted?")

    canaries = parse_canaries(os.environ.get("PUBLISH_DENY_CANARIES"))
    if canaries:
        matched = canaries_matched(rep.patterns, canaries, rep.allows)
        print(f"kb-check-publishable: canaries {matched}/{len(canaries)} matched by the denylist")
        if matched < len(canaries):
            return _refuse(f"{len(canaries) - matched} canary(ies) not matched by any deny "
                           "pattern (or excused by an [[allow]] literal) — the denylist "
                           "was weakened")
    elif strict:
        return _refuse("PUBLISH_DENY_CANARIES is unset or empty — strict mode needs the "
                       "canaries to prove the denylist wasn't weakened")

    refusals: list[str] = []
    if rep.lock is None:
        print(f"kb-check-publishable: no {lock.LOCK_REL} — none of the {rep.selected} "
              "selected path(s) is approved")
        if strict:
            refusals.append(f"no {lock.LOCK_REL} — strict mode needs every exported path "
                            "approved (`kb-publish approve`, then commit the lockfile)")
    else:
        for line in lock.report_lines(rep.lock, rep.selected):
            print(line if line.startswith(" ") else f"kb-check-publishable: {line}")
        if rep.lock.blocking:
            what = (f"{len(rep.lock.new)} new and {len(rep.lock.changed)} changed-pinned "
                    f"path(s) not approved in {lock.LOCK_REL}")
            if strict:
                refusals.append(f"{what} — review with `kb-publish approve`, then commit "
                                "the lockfile")
            else:
                print(f"kb-check-publishable: NOTE — {what}; kb-publish will refuse "
                      "them until approved")

    _print_warnings(rep.warnings)
    if rep.hits:
        print(f"kb-check-publishable: {len(rep.hits)} denylist/structural hit(s) in "
              "exportable paths:")
        for h in rep.hits:
            print(f"  {h.path}:{h.line_no}  [{h.pattern}]  {h.line}")
        refusals.append(f"{len(rep.hits)} denylist/structural hit(s)")
    if refusals:
        for r in refusals:
            _refuse(r)
        return 1
    print("kb-check-publishable: clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
