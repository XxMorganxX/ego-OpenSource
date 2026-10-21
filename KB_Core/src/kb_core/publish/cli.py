"""kb-publish: the layer-3 export gate. Fail-closed at every step.

Default is a dry run. --push publishes to the resolved remote and updates
private/publish-state.json (records the ref, clears pending coauthors).

Pull-side push gate: kb-publish does not rely on server-side branch
protection to keep red commits off main — it obeys CI itself (the whole
flow: docs/publishing.md). Before assembling anything, --push refuses unless
--expect-sha names the exact sha, `git fetch origin` shows it on origin/main, every
REQUIRED_CHECKS check-run on it is green (ci_attest.py), gitleaks is
installed and --skip-verify is absent. Before committing, the last published
upstream sha (public HEAD's "upstream <sha7>", cross-checked with state) must
be an ancestor — no rollbacks — and any change to the gate's own files
(gate.py) since then must be acknowledged with --ack-gate-changes <token> —
a digest of that change the dry run prints beside the diff — and the
exported paths added/removed since then are printed.
Verify runs in a COPY of the export, as the same scripts/ci/*.sh ci.yml runs,
against the scratch database KB_VERIFY_DATABASE_URL names (kb_verify*, never
the shared test DB); the committed tree must still be byte-identical to the
export afterwards. A dry run never contacts the
manifest's placeholder remote. Every outside call goes through
`Seams`, so tests never touch the network.

Subcommands: `export --ref R --out DIR` (assemble + every content gate, no
clone/push — what publish-candidate CI runs) and `diff` (path + gate diff vs
what is public, Markdown for a job summary).

Path lockfile (lock.py): every run prints the new/changed/removed path
diff between the export and private/publish.paths.lock as committed AT THE REF,
and refuses while any path is new or a pinned file changed. `kb-publish
approve [--ref REF]` is the only thing that writes the lockfile: it shows each
new/changed path (size + first lines, or "binary") and records the approval once
a human confirms at a terminal. There is no non-interactive approval (no
--yes), and the lockfile is a gate file, so every approval also needs the push's
--ack-gate-changes. --push never approves anything.

Push target (target.py): --push writes only to the public repo pinned
by `kb-publish pin --repo owner/name` (GitHub's numeric id, in private state).
Before anything is cloned or committed the remote URL must parse to a
github.com owner/name that is not the private origin, and GitHub must report
it public, unredirected, with the pinned id. git_ops then refuses a remote
whose main holds anything but sync: commits by the mirror identity, builds
the commit in a scrubbed environment dated 00:00 UTC of the upstream commit's
UTC day, and deny-scans the commit and tag objects before pushing. Dry runs
need no pin and never call GitHub.

Remote resolution (highest precedence first): --remote flag > KB_PUBLISH_REMOTE
env var > the manifest's [publish] remote. The manifest value is a fallback for
forks who edit their own manifest — the real upstream remote URL contains a
denylisted string and cannot live in an exported file, so this repo's own
publish relies on --remote/KB_PUBLISH_REMOTE rather than the manifest. Because
the manifest remote is a fork-facing placeholder, --push refuses to use it
(dry runs may still use it, with a note).

Remote for --push without --remote/KB_PUBLISH_REMOTE: the pinned public repo
over ssh (the deploy-key transport); the manifest placeholder only when
nothing is pinned (and then refused).

Identity resolution: KB_PUBLISH_IDENTITY_NAME/EMAIL env vars, else the
neutral "ego-OS mirror <mirror@invalid>" — never `git config user.*`, which is
the operator's real name and address. The email must be reserved or
GitHub noreply (structural, not overridable). The identity, the commit
message and any --tag are scanned against the denylist before anything is
committed — content-tree scanning alone would miss an identifier concealed in
immutable commit metadata; a pending coauthor that hits is dropped with a
warning. A deny hit in the identity fields alone can be overridden with
--allow-identity-hit (never a message/tag hit).
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from kb_core.publish import (accept as accept_mod, ci_attest, deny, export, gate, git_ops,
                              hooks, lock, manifest, state, structural, target, verify)
from kb_core.publish import github as github_api


@dataclass
class Seams:
    """Everything kb-publish asks the outside world, swappable in tests.

    fetch_origin  refresh origin/main (git fetch, read-only)
    check_runs    GitHub check-runs for (private repo, sha) — ci_attest.Lookup
    workflow_runs the workflow runs behind them (provenance) — ci_attest.RunsLookup
    public_head   public main's tip for a remote URL (None = empty)
    which         locate an executable (gitleaks)
    verify        verify(tree, require_gitleaks=, which=); None = verify.verify
    clone_public  read-only clone of the public repo's branches + tags (`kb-publish accept`)
    gate_code     why the running gate code is not the code at a rev, or None
    hook_problem  why the pre-push hook (source read at the published sha) is not
                  doing its job in this clone, or None — hooks.problem
    """
    # Defaults look the module function up at CALL time, so a test that
    # patches gate.read_public_head (say) cannot be bypassed by a stale binding.
    fetch_origin: Callable[[Path], None] = lambda root: gate.fetch_origin(root)
    check_runs: ci_attest.Lookup = lambda repo, sha: ci_attest.gh_check_runs(repo, sha)
    workflow_runs: ci_attest.RunsLookup = (
        lambda repo, sha: ci_attest.gh_workflow_runs(repo, sha))
    public_head: Callable[[str], gate.PublicHead | None] = (
        lambda remote: gate.read_public_head(remote))
    which: Callable[[str], str | None] = lambda name: shutil.which(name)
    verify: Callable[..., None] | None = None
    clone_public: accept_mod.Clone = lambda remote, dest: accept_mod.clone(remote, dest)
    gate_code: Callable[[Path, str], str | None] = (
        lambda root, rev: gate.running_gate_problem(root, rev))
    hook_problem: Callable[[Path, str, str | None], str | None] = (
        lambda root, rev, private: hooks.problem(root, rev=rev, private_full_name=private))


# The check-runs a push needs green on the exact sha (ci_attest.py).
REQUIRED_CHECKS = ci_attest.REQUIRED_CHECKS

_MANIFEST_SOURCE = "manifest [publish] remote"
_PINNED_SOURCE = "pinned public repo"
# The publish identity when KB_PUBLISH_IDENTITY_* is unset. Never `git config
# user.*`: that is the operator's real name and address.
_NEUTRAL_IDENTITY = ("ego-OS mirror", "mirror@invalid")


def _publish_identity() -> tuple[str, str]:
    """(name, email) the sync commits are written with — and the only identity
    `accept --allow-identity-hit` may excuse."""
    return (os.environ.get("KB_PUBLISH_IDENTITY_NAME") or _NEUTRAL_IDENTITY[0],
            os.environ.get("KB_PUBLISH_IDENTITY_EMAIL") or _NEUTRAL_IDENTITY[1])


def _rev(root: Path, ref: str) -> str:
    return subprocess.run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=root,
                          check=True, capture_output=True, text=True).stdout.strip()


def _refused(prog: str, msg: str, rc: int = 1) -> int:
    print(f"{prog}: REFUSED — {msg}")
    return rc


class _Prepared(NamedTuple):
    kept: list[str]
    approved: lock.Entries | None
    patterns: list


def _prepare(root: Path, sha: str, ref: str, tree: Path, prog: str) -> _Prepared | int:
    """Assemble `sha` into `tree` and run every content gate. An int is a refusal rc.

    Shared by kb-publish (dry run and push) and `kb-publish export`: the export
    CI uploads is the export a push would build, checked the same way.
    """
    try:
        kept = export.assemble(root, sha, tree)
    except export.ExportError as e:
        return _refused(prog, str(e))
    print(f"{prog}: assembled {len(kept)} files from {sha[:7]}")

    # Path lockfile: the approval that counts is the one COMMITTED at
    # the ref being published — a working-tree approval of some other state
    # proves nothing about this tree. Printed every run; new or changed-
    # pinned paths refuse below, after the scans, so one dry run shows all.
    try:
        approved = lock.load_at_ref(root, sha)
    except lock.LockError as e:
        return _refused(prog, f"{lock.LOCK_REL} at {ref} is invalid: {e}")
    lock_diff = lock.diff(approved, lock.snapshot(tree, kept))
    for line in lock.report_lines(lock_diff, len(kept), have_lock=approved is not None):
        print(f"{prog}: {line}" if not line.startswith(" ") else line)

    deny_path = root / "publish.deny.toml"
    if not deny_path.exists():
        print(f"{prog}: publish.deny.toml missing — refusing (layer 3 needs it)")
        return 1

    # The deny file is tracked, so `ref` should carry the same copy the
    # export was built from. A working-tree edit that hasn't been
    # committed would silently scan with different (possibly weaker)
    # patterns than what the published tree was actually screened
    # against — refuse rather than trust an uncommitted denylist.
    ref_deny = subprocess.run(["git", "show", f"{sha}:publish.deny.toml"],
                              cwd=root, capture_output=True)
    if ref_deny.returncode == 0 and ref_deny.stdout != deny_path.read_bytes():
        print(f"{prog}: publish.deny.toml differs between working tree and "
              f"{ref} — commit the denylist change first.")
        return 1
    # ref_deny.returncode != 0: the file isn't present at this ref (an old
    # ref predating the denylist) — tolerate, nothing to compare against.

    patterns = deny.load_patterns(deny_path)
    print(f"{prog}: {len(patterns)} deny patterns loaded")
    if not patterns:
        return _refused(prog, "publish.deny.toml has zero patterns (fail-closed)")
    # [[allow]]: exact literals masked in file CONTENT only (deny.py). Commit
    # message, tag, identity and coauthor scans below never take them.
    try:
        allows = deny.load_allows(deny_path, patterns)
    except deny.DenyConfigError as e:
        return _refused(prog, f"invalid publish.deny.toml: {e}")
    if allows:
        print(f"{prog}: {len(allows)} exact [[allow]] literal(s) loaded")

    exported_manifest = manifest.load_manifest(tree / "publish.manifest.toml")
    media = manifest.unapproved_media(kept, exported_manifest)
    if media:
        print(f"{prog}: REFUSED — {len(media)} media file(s) not in "
              f"[export] allow_media (images are never denylist-scanned):")
        for rel in media[:20]:
            print(f"  {rel}")
        return 1

    # Denylist content AND path names, plus the structural detectors
    # (encoding, archives, email domains, home paths, IPs, TOML markers).
    # A non-UTF-8 file passes only with its exact bytes hash-pinned.
    scan = structural.scan_files(tree, kept, patterns,
                                 is_pinned=lock.pinned_hook(approved),
                                 allow_binary=exported_manifest.allow_binary,
                                 allows=allows)
    if scan.warnings:
        print(f"{prog}: {len(scan.warnings)} lint warning(s) (non-blocking):")
        for w in scan.warnings[:20]:
            print(f"  {w.path}:{w.line_no}  [{w.pattern}]  {w.line}")
    if scan.hits:
        print(f"{prog}: REFUSED — {len(scan.hits)} denylist/structural hit(s) "
              f"in the export tree:")
        for h in scan.hits[:20]:
            print(f"  {h.path}:{h.line_no}  [{h.pattern}]  {h.line}")
    else:
        print(f"{prog}: scan clean")
    if lock_diff.blocking:
        print(f"{prog}: REFUSED — {_unapproved(lock_diff, approved)} not approved "
              f"in {lock.LOCK_REL} at {ref}. Review with `kb-publish approve --ref "
              f"{ref}`, commit the lockfile, then publish the NEW sha.")
        if (root / lock.LOCK_REL).is_file() and approved != _load_quiet(root):
            print(f"{prog}: NOTE — the working-tree {lock.LOCK_REL} differs from "
                  f"{ref}'s: an approval only counts once committed.")
    if scan.hits or lock_diff.blocking:
        return 1
    return _Prepared(kept, approved, patterns)


def _push_preflight(root: Path, sha: str, ref: str, *, expect_sha: str | None,
                    skip_verify: bool, seams: Seams, st: dict) -> str | None:
    """Push checks that need no export (exact sha, hook, origin/main, CI,
    gitleaks) — the reason to refuse, or None.

    Runs before anything is assembled: a push publishes exactly the commit the
    operator named, that private main contains and that CI passed.
    """
    if skip_verify:
        return ("--skip-verify is not allowed with --push: verify (the export's own "
                "tests + gitleaks) is part of the gate")
    if not expect_sha:
        return (f"--push needs --expect-sha <full sha> naming the commit CI passed "
                f"({ref} is {sha})")
    if expect_sha.lower() != sha:
        return (f"--expect-sha {expect_sha} does not match {ref} = {sha} (give the full "
                f"40-char sha of the commit you mean to publish)")
    if seams.which("gitleaks") is None:
        return "gitleaks is not installed — a push requires the export-tree secret scan"
    # Publish only from a clone whose allowlist hook is doing its job.
    hook_problem = seams.hook_problem(root, sha, st["private_repo_full_name"])
    if hook_problem:
        return hook_problem
    try:
        # CI and origin/main come from the PINNED private repo — never from
        # whatever origin happens to point at (a fork's Actions can paint it green).
        # The id is re-checked against GitHub with the public target, before commit.
        repo = target.check_private_name(target.origin_full_name(root),
                                         pinned_id=st["private_repo_id"],
                                         pinned_full_name=st["private_repo_full_name"])
        seams.fetch_origin(root)
        if not gate.on_origin_main(root, sha):
            return (f"{sha[:12]} is not on origin/main — push it to the private repo and "
                    f"let CI pass first (local, unpushed and side/worktree branches have "
                    f"had no main CI)")
    except (target.TargetError, gate.GateError) as e:
        return str(e)
    print(f"kb-publish: {sha[:12]} is on origin/main ({repo})")
    try:
        names = ci_attest.attest(repo, sha, seams.check_runs, runs=seams.workflow_runs)
    except ci_attest.CIAttestError as e:
        return f"CI: {e}"
    print(f"kb-publish: CI green on {sha[:12]} — {len(names)} required check-run(s) from "
          f"ci.yml / publish-candidate.yml on main: " + ", ".join(names))
    return None


def _gate_lines(base: gate.Base, sha: str, changes: list[tuple[str, str]],
                removed: list[str], allow_changes: list[str] | tuple = ()) -> list[str]:
    since = base.sha[:7] if base.sha else "nothing (first publish)"
    lines = [f"gate: {len(changes)} gate file(s) changed since {since}"]
    lines += [f"  {status} {path}" for status, path in changes]
    if removed:
        # Count only: the patterns ARE the concealed identifiers.
        lines.append(f"gate: {len(removed)} deny pattern(s) removed since {since} — "
                     f"`git diff {base.sha[:12]} {sha[:12]} -- publish.deny.toml`")
    if allow_changes:
        # Kinds and counts only, as above: each literal contains a concealed identifier.
        kinds = {k: allow_changes.count(k) for k in dict.fromkeys(allow_changes)}
        summary = ", ".join(f"{n} {k}" + ("(s)" if k == "added literal" else "")
                            for k, n in kinds.items())
        lines.append(f"gate: {len(allow_changes)} [[allow]] change(s) since {since}: "
                     f"{summary} (exact-match exceptions to the denylist) — `git diff "
                     f"{(base.sha or '')[:12]} {sha[:12]} -- publish.deny.toml`")
    return lines


class GateReview(NamedTuple):
    base: gate.Base
    head: gate.PublicHead | None
    changes: list[tuple[str, str]]
    removed: list[str]
    token: str | None           # what --ack-gate-changes must be; None = nothing to ack
    allow_changes: list[str] | tuple = ()  # [[allow]] widenings since base (kinds, no literal)


def gate_review(root: Path, sha: str, *, read_remote: str | None, st: dict, seams: Seams,
                push: bool) -> GateReview:
    """The review against what is public, as data: base, rollback check, gate
    diff, ack token.

    Reads public HEAD from `read_remote` (None = don't); a dry run falls back
    to private state when that read fails, a push refuses. Raises GateError.
    """
    head, public_read, notes = None, False, []
    if read_remote is not None:
        try:
            head, public_read = seams.public_head(read_remote), True
        except gate.GateError as e:
            if push:
                raise gate.GateError(f"cannot read public HEAD: {e}") from e
            notes.append(f"could not read public HEAD ({e}); using private state only")
    base = gate.resolve_base(root, head=head, public_read=public_read,
                             state_ref=st["last_published_ref"])
    base.notes[:0] = notes
    gate.check_no_rollback(root, base.sha, sha)
    changes = gate.gate_changes(root, base.sha, sha)
    removed = gate.removed_deny_patterns(root, base.sha, sha)
    return GateReview(base, head, changes, removed,
                      gate.ack_token(root, base.sha, sha, changes, removed),
                      gate.allow_changes(root, base.sha, sha))


def _path_diff(root: Path, review: GateReview, now: list[str]) -> tuple[list[str], list[str],
                                                                      str]:
    """Exported paths added/removed since the last publish."""
    if review.head is not None:
        before, against = review.head.paths, f"public HEAD {review.head.sha[:12]}"
    elif review.base.sha:
        with tempfile.TemporaryDirectory(prefix="kb-publish-base-") as td, \
                contextlib.redirect_stdout(sys.stderr):
            before = export.assemble(root, review.base.sha, Path(td) / "base")
        against = f"the export of {review.base.sha[:7]}"
    else:
        before, against = [], "nothing (first publish)"
    return sorted(set(now) - set(before)), sorted(set(before) - set(now)), against


def _check_gate(root: Path, sha: str, *, push: bool, read_remote: str | None,
                st: dict, ack: str | None, seams: Seams, kept: list[str]) -> int | None:
    """Push gate against what is public: no rollback; gate changes acknowledged;
    paths shown.

    Returns a refusal rc, or None to continue. A push reads public HEAD from
    the validated target; a dry run only from an explicit --remote/env remote
    (never the placeholder), falling back to private state if that read fails.
    """
    prog = "kb-publish"
    try:
        review = gate_review(root, sha, read_remote=read_remote, st=st, seams=seams,
                             push=push)
        added, gone, against = _path_diff(root, review, kept)
    except (gate.GateError, export.ExportError) as e:
        return _refused(prog, str(e))
    print(f"{prog}: last published upstream: {review.base.source}")
    for note in review.base.notes:
        print(f"{prog}: NOTE — {note}")
    if read_remote is None:
        # Without a read, "first publish" may just mean "unknown here".
        print(f"{prog}: NOTE — public HEAD not read: the path and gate diff below are "
              f"against private state only; `kb-publish diff` reads the pinned public repo "
              f"for the authoritative one (and its ack token)")
    print(f"{prog}: paths vs {against}: {len(added)} added, {len(gone)} removed "
          f"(of {len(kept)} exported)")
    for rel in added:
        print(f"  + {rel}")
    for rel in gone:
        print(f"  - {rel}")
    for line in _gate_lines(review.base, sha, review.changes, review.removed,
                            review.allow_changes):
        print(f"{prog}: {line}" if not line.startswith(" ") else line)

    # The gate judging this change must be the one already public — code
    # in the commit under review runs on import and could switch checks off.
    judge = review.base.sha or sha
    problem = seams.gate_code(root, judge)
    if problem:
        what = "the last published upstream" if review.base.sha else "this sha (first publish)"
        msg = (f"{problem} — kb-publish must run the gate code of {judge[:12]} ({what}): "
               f"build a non-editable venv from that commit (`git worktree add <dir> "
               f"{judge[:12]}`, then install KB_Core there) and run kb-publish from it")
        if push:
            return _refused(prog, msg)
        print(f"{prog}: NOTE — {msg}")

    given = ack.strip().lower() if ack is not None else None
    if review.token is None:
        if given is not None:
            return _refused(prog, "--ack-gate-changes was given but nothing in the gate "
                                  "changed since the last publish — drop it (an "
                                  "acknowledgement must never be a reflex)")
        return None
    if given == review.token:
        print(f"{prog}: gate changes acknowledged ({review.token})")
        return None
    if given is not None:
        return _refused(prog, "--ack-gate-changes does not match this gate change (stale, "
                              "or acknowledges a different change / base) — re-read the "
                              "diff in a dry run")
    if push:
        # The token is deliberately NOT printed here: it comes with the dry run /
        # `kb-publish diff`, next to the diff it acknowledges.
        return _refused(prog, f"the gate itself changed since the last publish — read the "
                              f"diff in a dry run (`kb-publish --ref {sha[:12]} --expect-sha "
                              f"{sha}`), which prints the --ack-gate-changes token for "
                              f"exactly this change")
    print(f"{prog}: NOTE — read the gate diff above; --push will need "
          f"--ack-gate-changes {review.token}")
    return None


def _digest(tree: Path) -> dict[str, str]:
    return {p.relative_to(tree).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(tree.rglob("*")) if p.is_file() or p.is_symlink()}


def run(root: Path, *, ref: str, push: bool, tag: str | None,
        skip_verify: bool, remote: str | None = None,
        allow_identity_hit: bool = False,
        github: github_api.Lookup | None = None,
        expect_sha: str | None = None, ack_gate_changes: str | None = None,
        seams: Seams | None = None) -> int:
    seams = seams or Seams()
    sha = _rev(root, ref)
    if push:
        reason = _push_preflight(root, sha, ref, expect_sha=expect_sha,
                                 skip_verify=skip_verify, seams=seams, st=state.load(root))
        if reason:
            return _refused("kb-publish", reason)
    elif expect_sha is not None and expect_sha.lower() != sha:
        return _refused("kb-publish", f"--expect-sha {expect_sha} does not match {ref} = {sha}")
    # The upstream commit's calendar day in UTC: it names the sync commit and
    # pins its author/committer dates to 00:00:00 UTC, so the public
    # commit carries neither the operator's timezone nor the hour of the run.
    date = subprocess.run(["git", "log", "-1", "--date=format-local:%Y-%m-%d",
                           "--format=%cd", sha], cwd=root, check=True,
                          capture_output=True, text=True,
                          env={**os.environ, "TZ": "UTC"}).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="kb-publish-") as td:
        tree = Path(td) / "export"
        prepared = _prepare(root, sha, ref, tree, "kb-publish")
        if isinstance(prepared, int):
            return prepared
        kept, patterns = prepared.kept, prepared.patterns

        m = manifest.load_manifest(root / "publish.manifest.toml")
        st = state.load(root)
        message = f"sync: {date} (upstream {sha[:7]})"

        if remote:
            resolved_remote, remote_source = remote, "--remote flag"
        elif os.environ.get("KB_PUBLISH_REMOTE"):
            resolved_remote, remote_source = os.environ["KB_PUBLISH_REMOTE"], "KB_PUBLISH_REMOTE env"
        elif push and st["public_repo_full_name"]:
            # The pin already names the one repo a push may write; the
            # deploy-key transport is ssh. (A dry run stays offline: no clone.)
            resolved_remote = f"ssh://git@github.com/{st['public_repo_full_name']}.git"
            remote_source = _PINNED_SOURCE
        else:
            resolved_remote, remote_source = m.remote, _MANIFEST_SOURCE
        print(f"kb-publish: remote = {resolved_remote} (source: {remote_source})")

        offline = False
        if remote_source == _MANIFEST_SOURCE:
            if push:
                print("kb-publish: REFUSED — no --remote/KB_PUBLISH_REMOTE set; the "
                      "manifest remote is a fork-facing placeholder — refusing to push there.")
                return 1
            # The placeholder is never contacted, not even read.
            offline = True
            print("kb-publish: NOTE — dry run only; the manifest remote is a fork-facing "
                  "placeholder and is not contacted. Set --remote or KB_PUBLISH_REMOTE "
                  "before --push.")

        identity_name, identity_email = _publish_identity()
        print(f"kb-publish: identity = {identity_name} <{identity_email}>")
        # Structural, not just the denylist: an address that isn't reserved or
        # GitHub noreply is personal whether or not anyone listed it. Not
        # overridable — the identity is pinned into every later publish.
        if structural.scan_content("identity", f'"{identity_email}"'):
            return _refused("kb-publish", "KB_PUBLISH_IDENTITY_EMAIL must be a reserved "
                                          "(*.invalid, *.example, ...) or "
                                          "users.noreply.github.com address")

        # Commit author/committer identity, coauthor trailers, and the commit
        # message land verbatim in immutable public history — scan them
        # separately from the export tree. Identity hits are the one class an
        # operator can knowingly override (--allow-identity-hit); coauthor and
        # message hits are never overridable — those values come from another
        # party (pending_coauthors) or embed the export sha, not something a
        # flag on this invocation should be able to wave through.
        identity_hits = []
        identity_hits.extend(deny.scan_text("identity.name", identity_name, patterns))
        identity_hits.extend(deny.scan_text("identity.email", identity_email, patterns))

        # A pending coauthor that hits the denylist (it may have grown since
        # kb-import-pr recorded the credit) or whose email is not a reserved /
        # GitHub noreply address is DROPPED with a warning: omitting a trailer
        # leaks nothing, while refusing wedged every publish until someone
        # hand-edited the state file. Printed by index only — the value
        # is what must not be shown.
        coauthors = []
        for i, c in enumerate(st["pending_coauthors"]):
            bad = (deny.scan_text("n", c["name"], patterns)
                   or deny.scan_text("e", c["email"], patterns)
                   or structural.scan_content("coauthor", f'"{c["email"]}"'))
            if bad:
                print(f"kb-publish: WARNING — pending coauthor[{i}] dropped (denylist hit or "
                      f"an email that is not reserved/noreply); not credited")
            else:
                coauthors.append(c)

        metadata_hits = []
        metadata_hits.extend(deny.scan_text("commit message", message, patterns))
        if tag:
            # Tags land in immutable public metadata the same as the commit
            # message/coauthors — scan them the same way, not overridable.
            metadata_hits.extend(deny.scan_text("tag", tag, patterns))

        if metadata_hits:
            print(f"kb-publish: REFUSED — {len(metadata_hits)} denylist hit(s) in commit "
                  f"metadata (message/tag — not overridable):")
            for h in metadata_hits[:20]:
                print(f"  {h.path}: [{h.pattern}] {h.line}")
            return 1

        if identity_hits:
            if not allow_identity_hit:
                print(f"kb-publish: REFUSED — {len(identity_hits)} denylist hit(s) in the "
                      f"publish identity:")
                for h in identity_hits[:20]:
                    print(f"  {h.path}: [{h.pattern}] {h.line}")
                print("kb-publish: set KB_PUBLISH_IDENTITY_NAME/KB_PUBLISH_IDENTITY_EMAIL to "
                      "a neutral publish identity (or unset them for the default "
                      f"{_NEUTRAL_IDENTITY[0]} <{_NEUTRAL_IDENTITY[1]}>).")
                return 1
            print("kb-publish: WARNING — publishing with an identity that matches the "
                  "denylist (explicitly allowed)")

        # Target: a push goes only to the pinned public repo, checked
        # against GitHub BEFORE anything is cloned or committed. A dry run
        # neither needs a pin nor calls GitHub.
        publish_target = None
        if push:
            try:
                target.check_private(target.origin_full_name(root),
                                     pinned_id=st["private_repo_id"],
                                     pinned_full_name=st["private_repo_full_name"],
                                     github=github or github_api.gh_repo)
                publish_target = target.validate(
                    resolved_remote, pinned_id=st["public_repo_id"],
                    pinned_full_name=st["public_repo_full_name"],
                    origin_full_name=target.origin_full_name(root),
                    github=github or github_api.gh_repo)
            except target.TargetError as e:
                print(f"kb-publish: REFUSED — {e}")
                return 1
            print(f"kb-publish: target = {publish_target.full_name} (id "
                  f"{publish_target.repo_id}, public, matches the pin)")

        # No rollback, gate changes acknowledged.
        rc = _check_gate(root, sha, push=push, read_remote=None if offline else resolved_remote,
                         st=st, ack=ack_gate_changes, seams=seams, kept=kept)
        if rc is not None:
            return rc

        # Verify runs in a COPY: whatever builds and tests write stays
        # there. The tree that gets committed must still be exactly the export.
        pristine = _digest(tree)
        if sorted(pristine) != kept:
            return _refused("kb-publish", "the export tree does not match the selection")
        if skip_verify:
            print("kb-publish: verify SKIPPED (--skip-verify, dry run only)")
        else:
            work = Path(td) / "verify"
            shutil.copytree(tree, work, symlinks=True)
            try:
                (seams.verify or verify.verify)(work, require_gitleaks=push,
                                                which=seams.which)
            except verify.VerifyError as e:
                return _refused("kb-publish", str(e), rc=2)
            if _digest(tree) != pristine:
                return _refused("kb-publish", "the export tree changed during verify — "
                                              "nothing written outside its copy may be "
                                              "published")

        try:
            out_sha = git_ops.publish_tree(
                tree, None if offline else resolved_remote, message=message,
                coauthors=coauthors, push=push, tag=tag,
                workdir=Path(td) / "gitwork",
                identity=(identity_name, identity_email),
                deny_patterns=patterns, target=publish_target, date=date,
                last_public_sha=st["last_published_public_sha"] if push else None,
                allow_identity_hit=allow_identity_hit)
        except git_ops.PublishRefused as e:
            print(f"kb-publish: REFUSED — {e}")
            return 1
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else e.stderr
            print(f"kb-publish: REFUSED — git {' '.join(map(str, e.cmd[1:4]))} failed: "
                  f"{(err or '').strip()[-300:]}")
            return 1
        if push:
            prs = sorted({n for c in st["pending_coauthors"] for n in c.get("prs", [])
                          if isinstance(n, int) and not isinstance(n, bool) and n > 0})
            st.update(last_published_ref=sha, pending_coauthors=[],
                      last_published_public_sha=out_sha)
            state.save(root, st)
            print(f"kb-publish: pushed {out_sha[:7]} to {resolved_remote}"
                  + (f" (tag {tag})" if tag else ""))
            _print_pr_closes(prs, publish_target.full_name, out_sha, tag)
        elif offline:
            print(f"kb-publish: dry run ok — built sync commit {out_sha[:7]} on an empty "
                  f"history (placeholder remote not contacted).")
        else:
            print(f"kb-publish: dry run ok — would push {out_sha[:7]} to {resolved_remote}. "
                  "Re-run with --push to publish.")
    return 0


def _print_pr_closes(prs: list[int], repo: str, out_sha: str, tag: str | None) -> None:
    """The imported PRs this sync shipped, as commands for the operator to run:
    kb-publish itself never writes to the public repo outside the push."""
    if not prs:
        return
    shipped = f"{out_sha[:12]}" + (f" ({tag})" if tag else "")
    print(f"kb-publish: this sync ships {len(prs)} imported PR(s); close each with a link "
          f"(run by hand):")
    for n in prs:
        print(f'  gh pr close {n} -R {repo} --comment "Shipped in {shipped}. Thank you!"')


def export_tree(root: Path, *, ref: str, out: Path) -> int:
    """`kb-publish export --ref R --out DIR`: the export a push would build, gated.

    Assemble + lockfile + media/binary gate + deny and structural scans — no
    clone, no push, no network. publish-candidate CI runs it on every main sha,
    then gitleaks and the export's own CI over DIR. On any refusal DIR is
    removed, so a refused tree can never be uploaded as if it passed.
    """
    prog = "kb-publish export"
    sha = _rev(root, ref)
    if out.exists() and any(out.iterdir()):
        return _refused(prog, f"{out} is not empty")
    prepared = _prepare(root, sha, ref, out, prog)
    if isinstance(prepared, int):
        shutil.rmtree(out, ignore_errors=True)
        return prepared
    print(f"{prog}: ok — {len(prepared.kept)} files from {sha[:7]} at {out}; every path "
          f"approved, scans clean")
    return 0


def _public_remote_for_diff(remote: str | None, st: dict) -> tuple[str | None, str]:
    if remote:
        return remote, "--remote flag"
    if os.environ.get("KB_PUBLISH_REMOTE"):
        return os.environ["KB_PUBLISH_REMOTE"], "KB_PUBLISH_REMOTE env"
    if st["public_repo_full_name"]:
        # Anonymous read of the pinned PUBLIC repo — no credential needed.
        return f"https://github.com/{st['public_repo_full_name']}.git", "pinned public repo"
    return None, "none (no --remote, KB_PUBLISH_REMOTE or pin)"


# Exported files whose EDITS get a full diff in `kb-publish diff`: the lockfile
# only gates new paths, and test fixtures, evals and docs are where corpus
# text has been pasted into an already-approved file before.
_FULL_DIFF_MAX_LINES = 1500


def _wants_full_diff(rel: str) -> bool:
    low = rel.lower().split("/")
    return (low[0] == "evals" or "docs" in low[:-1] or low[-1].endswith(".md")
            or any("fixture" in seg for seg in low))


def _content_git(root: Path, *args: str) -> str:
    """git with no global/system config or attributes, and every diff knob that
    config could turn passed explicitly: the same output on every machine."""
    return subprocess.run(
        ["git", "--literal-pathspecs", "-c", f"core.attributesFile={os.devnull}",
         "-c", "core.quotePath=false",
         *args], cwd=root, check=True, capture_output=True, text=True,
        errors="replace", env=export._git_env()).stdout


_DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
               "--diff-algorithm=myers", "--indent-heuristic", "--src-prefix=a/",
               "--dst-prefix=b/")


def _content_lines(root: Path, base: str | None, sha: str, common: list[str]) -> list[str]:
    """Markdown: per-file +/- line counts for exported files that were already
    public and changed since `base`, then the full diff of those under
    tests fixtures, evals/ and docs (capped). Sorted by path: deterministic."""
    if not base:
        return ["- content: first publish — every exported file is new (each one was "
                "shown by `kb-publish approve`)"]
    try:
        raw = _content_git(root, "diff", "--numstat", "-z", *_DIFF_FLAGS, base, sha)
    except subprocess.CalledProcessError:
        return [f"- NOTE: content diff unavailable — {base[:12]} is not in this clone"]
    keep, rows = set(common), []
    for rec in filter(None, raw.split("\0")):
        added, deleted, rel = rec.split("\t", 2)
        if rel in keep:
            rows.append((rel, added, deleted))
    rows.sort()
    if not rows:
        return [f"- content vs `{base[:12]}`: no already-exported file changed"]
    plus = sum(int(a) for _, a, _ in rows if a != "-")
    minus = sum(int(d) for _, _, d in rows if d != "-")
    width = max(len(rel) for rel, _, _ in rows)
    out = [f"- content vs `{base[:12]}`: **{len(rows)} already-exported file(s) changed, "
           f"+{plus} -{minus}** — `git diff {base[:12]} {sha[:12]} -- <path>` for any of them",
           "", "```text"]
    out += [f"{rel.ljust(width)} | " + ("binary" if a == "-" else f"+{a} -{d}")
            for rel, a, d in rows]
    out += ["```", ""]
    full = [rel for rel, _, _ in rows if _wants_full_diff(rel)]
    if full:
        text = _content_git(root, "diff", "-U3", *_DIFF_FLAGS, base, sha, "--", *full).splitlines()
        shown = text[:_FULL_DIFF_MAX_LINES]
        out += [f"<details><summary>full diff: {len(full)} fixture/evals/docs file(s)"
                f"</summary>", "", "````diff", *shown, "````"]
        if len(text) > len(shown):
            out.append(f"(truncated: {len(text) - len(shown)} more line(s) — run the git "
                       f"diff above)")
        out += ["", "</details>", ""]
    return out


def diff(root: Path, *, ref: str, remote: str | None = None,
         seams: Seams | None = None) -> int:
    """`kb-publish diff`: what a publish of `ref` would add, remove and change in the gate.

    Also the CONTENT of already-exported files that changed (+/- per file, and
    the full diff of fixtures, evals and docs): the lockfile gates new paths
    only, so this is where a human sees an edit to an approved file.

    Compares with the public HEAD's upstream sha (read from --remote,
    KB_PUBLISH_REMOTE or the pinned repo), else private state, else "first
    publish". Markdown on stdout, for $GITHUB_STEP_SUMMARY. Never writes,
    never pushes. An inconsistent public record or a rollback is a refusal.
    """
    prog = "kb-publish diff"
    seams = seams or Seams()
    sha = _rev(root, ref)
    st = state.load(root)
    read_remote, source = _public_remote_for_diff(remote, st)
    try:
        review = gate_review(root, sha, read_remote=read_remote, st=st, seams=seams,
                             push=False)
    except gate.GateError as e:
        return _refused(prog, str(e))
    base, notes = review.base, [n.replace("could not read public HEAD",
                                          f"could not read public HEAD from {source}")
                                for n in review.base.notes]
    changes, removed = review.changes, review.removed

    with tempfile.TemporaryDirectory(prefix="kb-publish-diff-") as td, \
            contextlib.redirect_stdout(sys.stderr):     # stdout is the summary only
        try:
            now = export.assemble(root, sha, Path(td) / "now")
        except export.ExportError as e:
            return _refused(prog, str(e))
    try:
        added, gone, against = _path_diff(root, review, now)
    except export.ExportError as e:
        return _refused(prog, f"cannot rebuild the export of {base.sha[:12]}: {e}")

    print(f"### kb-publish diff — `{sha[:12]}`")
    print()
    print(f"- last published upstream: {base.source}")
    print(f"- public read: {source}")
    for note in notes:
        print(f"- NOTE: {note}")
    print(f"- paths vs {against}: **{len(added)} added, {len(gone)} removed** "
          f"(of {len(now)} exported)")
    print()
    if added or gone:
        print("```diff")
        for rel in added:
            print(f"+ {rel}")
        for rel in gone:
            print(f"- {rel}")
        print("```")
        print()
    try:
        for line in _content_lines(root, base.sha, sha, sorted(set(now) - set(added))):
            print(line)
    except subprocess.CalledProcessError as e:
        return _refused(prog, f"content diff failed: {(e.stderr or '').strip()[-300:]}")
    for line in _gate_lines(base, sha, changes, removed, review.allow_changes):
        print(f"  - `{line.strip()}`" if line.startswith(" ") else f"- {line}")
    if review.token:
        print(f"- **--push needs `--ack-gate-changes {review.token}`** (acknowledges exactly "
              f"the gate change listed above)")
    return 0


def accept(root: Path, *, remote: str | None = None, allow_identity_hit: bool = False,
           seams: Seams | None = None) -> int:
    """`kb-publish accept [--remote URL]`: scan everything the public repo holds.

    The acceptance scan that closes every publish: a fresh read-only clone of
    the public repo (default: the pinned one, anonymously), scanned with the REAL deny file
    and the structural detectors — every file at HEAD, every blob in history,
    every commit and tag object (accept.py). Any hit refuses.

    --allow-identity-hit excuses deny hits on commit/tag author, committer
    and tagger header lines equal to the publish identity
    (KB_PUBLISH_IDENTITY_*) — the identity a push with the same flag wrote.
    Nothing else is excused.
    """
    prog = "kb-publish accept"
    seams = seams or Seams()
    deny_path = root / "publish.deny.toml"
    if not deny_path.exists():
        return _refused(prog, "no publish.deny.toml — acceptance is a private-repo operation")
    patterns = deny.load_patterns(deny_path)
    if not patterns:
        return _refused(prog, "publish.deny.toml has zero patterns (fail-closed)")
    try:
        allows = deny.load_allows(deny_path, patterns)
    except deny.DenyConfigError as e:
        return _refused(prog, f"invalid publish.deny.toml: {e}")
    st = state.load(root)
    if remote is None:
        if not st["public_repo_full_name"]:
            return _refused(prog, "no --remote and no pinned public repo")
        remote = f"https://github.com/{st['public_repo_full_name']}.git"
    print(f"{prog}: cloning {remote} (read-only)")
    with tempfile.TemporaryDirectory(prefix="kb-accept-") as td:
        repo = Path(td) / "public.git"
        try:
            seams.clone_public(remote, repo)
            rep, n = accept_mod.scan(root, repo, patterns,
                                     identity=_publish_identity() if allow_identity_hit
                                     else None, allows=allows)
        except (accept_mod.AcceptError, subprocess.CalledProcessError, lock.LockError,
                manifest.ManifestError) as e:
            return _refused(prog, f"cannot scan {remote}: {e}")
    summary = (f"{n['files']} file(s) at HEAD, {n['history']} other blob(s) in history, "
               f"{n['commits']} commit(s), {n['tags']} tag(s)")
    if n["excused"]:
        name, email = _publish_identity()
        print(f"{prog}: WARNING — {n['excused']} denylist hit(s) excused on commit/tag "
              f"header lines naming the publish identity {name} <{email}> "
              f"(--allow-identity-hit)")
    if rep.hits:
        print(f"{prog}: REFUSED — {len(rep.hits)} hit(s) in what is public ({summary}):")
        for h in rep.hits[:50]:
            print(f"  {h.path}:{h.line_no}  [{h.pattern}]  {h.line}")
        return 1
    print(f"{prog}: ok — {summary} scanned clean with the real deny file "
          f"({len(patterns)} patterns) and the structural detectors")
    return 0


def _main_accept(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="kb-publish accept",
                                 description="Clone the public repo (read-only) and scan "
                                             "everything in it: HEAD, history, commit and "
                                             "tag objects.")
    ap.add_argument("--remote", help="public repo URL (default: the pinned repo, https)")
    ap.add_argument("--allow-identity-hit", action="store_true",
                    help="excuse denylist hits on commit/tag author/committer/tagger header "
                         "lines that exactly equal KB_PUBLISH_IDENTITY_NAME/EMAIL (never a "
                         "message, trailer or file) — the identity --push "
                         "--allow-identity-hit published with")
    args = ap.parse_args(argv)
    return accept(_toplevel(), remote=args.remote, allow_identity_hit=args.allow_identity_hit)


def _unapproved(d: lock.LockDiff, approved: lock.Entries | None) -> str:
    if approved is None:
        return f"no lockfile — all {len(d.new)} path(s) are"
    return f"{len(d.new)} new and {len(d.changed)} changed-pinned path(s) are"


def _load_quiet(root: Path) -> lock.Entries | None:
    try:
        return lock.load(root)
    except lock.LockError:
        return None


def _ask(prompt: str) -> bool:
    return input(prompt).strip().lower() in ("y", "yes")


def approve(root: Path, *, ref: str,
            confirm: Callable[[str], bool] | None = None) -> int:
    """`kb-publish approve`: show every new/changed path, then record the lockfile.

    The ONLY writer of private/publish.paths.lock. Builds the same export
    kb-publish would from `ref`, prints each new or changed-pinned path with its
    size and first lines (or that it is binary), lists removals, and — once a
    human says yes — rewrites the working-tree lockfile to exactly that export.
    Committing it is left to the human: the commit is the reviewable record.

    Never runs as part of --push, and never without a human: with no terminal
    on stdin it refuses (there is no --yes). `confirm` is the prompt, injectable
    for tests only.
    """
    if not (root / "publish.deny.toml").exists():
        print("kb-publish approve: REFUSED — no publish.deny.toml; approving exports "
              "is a private-repo operation")
        return 1
    sha = subprocess.run(["git", "rev-parse", ref], cwd=root, check=True,
                         capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="kb-approve-") as td:
        tree = Path(td) / "export"
        try:
            kept = export.assemble(root, sha, tree)
        except export.ExportError as e:
            print(f"kb-publish approve: REFUSED — {e}")
            return 1
        try:
            approved = lock.load(root)
        except lock.LockError as e:
            print(f"kb-publish approve: REFUSED — the working-tree {lock.LOCK_REL} is "
                  f"invalid ({e}); restore it from git rather than hand-editing it")
            return 1
        current = lock.snapshot(tree, kept)
        d = lock.diff(approved, current)
        if approved is not None and not (d.new or d.changed or d.removed):
            print(f"kb-publish approve: {lock.LOCK_REL} is up to date with "
                  f"{sha[:7]} ({len(current)} paths)")
            return 0
        try:
            text = lock.render(current)
        except lock.LockError as e:
            print(f"kb-publish approve: REFUSED — {e}; rename or exclude that path")
            return 1

        print(f"kb-publish approve: export of {sha[:7]} — {len(d.new)} new, "
              f"{len(d.changed)} changed-pinned, {len(d.removed)} removed "
              f"(of {len(current)})")
        for tag, paths in (("+", d.new), ("~", d.changed)):
            for rel in paths:
                print(f"{tag} {lock.describe(rel, (tree / rel).read_bytes())}")
        for rel in d.removed:
            print(f"- {rel}")

        if confirm is None:
            if not sys.stdin.isatty():
                print("kb-publish approve: REFUSED — not a terminal. Approving paths for "
                      "export is a human step: run it yourself in a terminal, read the "
                      "list above, and answer the prompt")
                return 1
            confirm = _ask
        if not confirm(f"Approve these {len(d.new) + len(d.changed)} path(s) for "
                       f"export? [y/N] "):
            print("kb-publish approve: REFUSED — not confirmed; lockfile unchanged")
            return 1

        out = root / lock.LOCK_REL
        out.parent.mkdir(exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(text)
        tmp.replace(out)
    print(f"kb-publish approve: wrote {lock.LOCK_REL} ({len(current)} paths). Commit it "
          f"(`git add {lock.LOCK_REL}`), then publish the NEW sha.")
    return 0


def pin(root: Path, *, repo: str, replace: bool = False, reset_history: bool = False,
        github: github_api.Lookup | None = None) -> int:
    """`kb-publish pin --repo owner/name`: record WHICH repo is the public mirror
    and which is the private repo (origin).

    Stores GitHub's numeric id and canonical name in private state, after
    checking the repo is public, is not the private origin (by name and by id)
    and was not reached through a rename redirect. --push refuses without a pin
    and refuses any target whose id differs, so a URL that later points
    somewhere else — the private repo, a re-registered name — cannot be pushed.
    Re-pinning to a different id needs --replace: it is the one moment the
    target changes, so it should be deliberate. The same goes for the private
    pin: origin's id and canonical name, recorded so --push reads CI and
    origin/main from that repo only.

    --reset-history (with --replace) is the incident path: the public repo was
    deleted and recreated, so the record of the last publish names a commit
    the new repo never had and every later push would refuse ("history was
    deleted"). It clears last_published_ref and last_published_public_sha —
    printing what it drops — only when GitHub reports the repo being pinned
    empty (no branches: nothing a first publish could clobber or hide); the
    next push is then a first publish. Pending coauthors are kept (their
    credit is still owed).
    """
    if reset_history and not replace:
        print("kb-publish pin: REFUSED — --reset-history only goes with --replace (the "
              "incident path: a recreated public repo)")
        return 1
    if not (root / "publish.deny.toml").exists():
        print("kb-publish pin: REFUSED — no publish.deny.toml; pinning the publish "
              "target is a private-repo operation")
        return 1
    lookup = github or github_api.gh_repo
    try:
        origin = target.origin_full_name(root)
        info = target.check_public(repo, origin_full_name=origin, github=lookup)
        private = target.check_origin_private(origin, github=lookup)
    except target.TargetError as e:
        print(f"kb-publish pin: REFUSED — {e}")
        return 1
    try:
        allowed = hooks.allowed_slug(root)
    except hooks.HookError as e:
        print(f"kb-publish pin: REFUSED — {e}")
        return 1
    if allowed and info.full_name.casefold() == allowed:
        print(f"kb-publish pin: REFUSED — the pre-push hook allows git pushes to "
              f"{info.full_name}; a repo every dev clone may push to can never be the public "
              f"mirror (give the public repo a name the private repo has never had)")
        return 1
    st = state.load(root)
    if st["public_repo_id"] not in (None, info.id) and not replace:
        print(f"kb-publish pin: REFUSED — already pinned to {st['public_repo_full_name']} "
              f"(id {st['public_repo_id']}); pass --replace to switch the publish target "
              f"to {info.full_name} (id {info.id})")
        return 1
    if st["private_repo_id"] not in (None, private.id) and not replace:
        print(f"kb-publish pin: REFUSED — the pinned private repo is "
              f"{st['private_repo_full_name']} (id {st['private_repo_id']}), but origin is "
              f"{private.full_name} (id {private.id}); restore origin, or pass --replace to "
              f"move the private pin")
        return 1
    recorded = {k: st[k] for k in ("last_published_ref", "last_published_public_sha")
                if st[k] is not None}
    if reset_history and not info.empty:
        print(f"kb-publish pin: REFUSED — --reset-history: GitHub reports "
              f"{info.full_name} is not empty; a recreated repo must have no branches "
              f"before the next first publish")
        return 1
    st.update(public_repo_id=info.id, public_repo_full_name=info.full_name,
              private_repo_id=private.id, private_repo_full_name=private.full_name)
    if reset_history:
        st.update(last_published_ref=None, last_published_public_sha=None)
    state.save(root, st)
    print(f"kb-publish pin: pinned {info.full_name} (id {info.id}, public) as the "
          f"publish target and {private.full_name} (id {private.id}, origin) as the private "
          f"repo whose CI a push obeys, in {state.path(root)}")
    if reset_history:
        for k, v in recorded.items():
            print(f"kb-publish pin: reset-history — dropped {k} = {v}")
        if not recorded:
            print("kb-publish pin: reset-history — no publish was recorded; nothing dropped")
        print("kb-publish pin: the next --push is a first publish (gate changes are listed "
              "against nothing and need --ack-gate-changes)")
    elif recorded and replace and info.empty:
        print(f"kb-publish pin: NOTE — {info.full_name} is empty but this checkout recorded "
              f"publishing {recorded.get('last_published_public_sha') or '(a sync commit)'}; "
              f"if the public repo was recreated, re-run with --replace --reset-history or "
              f"every push will refuse")
    return 0


def _main_pin(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="kb-publish pin",
                                 description="Pin the public mirror's GitHub repo id; "
                                             "--push refuses any other target.")
    ap.add_argument("--repo", required=True, help="owner/name of the PUBLIC mirror")
    ap.add_argument("--replace", action="store_true",
                    help="switch an existing pin to a different repo")
    ap.add_argument("--reset-history", action="store_true",
                    help="with --replace, after the public repo was deleted and recreated: "
                         "forget the last published commit (prints what it drops). Refused "
                         "unless GitHub reports the repo empty")
    args = ap.parse_args(argv)
    return pin(_toplevel(), repo=args.repo, replace=args.replace,
               reset_history=args.reset_history)


def _main_approve(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="kb-publish approve",
                                 description="Review and record new/changed export paths "
                                             f"in {lock.LOCK_REL}.")
    ap.add_argument("--ref", default="main")
    args = ap.parse_args(argv)
    return approve(_toplevel(), ref=args.ref)


def _toplevel() -> Path:
    return Path(subprocess.run(["git", "rev-parse", "--show-toplevel"],
                               capture_output=True, check=True).stdout.decode().strip())


def _main_export(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="kb-publish export",
                                 description="Assemble and gate the export of REF into DIR "
                                             "(no clone, no push). Used by publish-candidate CI.")
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    return export_tree(_toplevel(), ref=args.ref, out=args.out)


def _main_diff(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="kb-publish diff",
                                 description="Path, content and gate-file diff of REF against "
                                             "what is public (Markdown, for a job summary).")
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--remote", help="public repo to read HEAD from (else KB_PUBLISH_REMOTE, "
                                     "else the pinned repo over anonymous https)")
    args = ap.parse_args(argv)
    return diff(_toplevel(), ref=args.ref, remote=args.remote)


_SUBCOMMANDS = {"approve": _main_approve, "pin": _main_pin, "export": _main_export,
                "diff": _main_diff, "accept": _main_accept}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] and argv[0] in _SUBCOMMANDS:
        return _SUBCOMMANDS[argv[0]](argv[1:])
    ap = argparse.ArgumentParser(
        prog="kb-publish",
        description="Dry run by default. --push needs --expect-sha (on origin/main, green "
                    "CI), --ack-gate-changes when the gate changed, gitleaks, and verify.",
        epilog="Subcommands: `approve [--ref REF]` (interactive) records new export paths; "
               "`pin --repo OWNER/NAME` pins the push target; `export --ref REF --out DIR` "
               "builds and gates the export only; `diff [--ref REF]` prints the path and "
               "gate diff against what is public; `accept [--remote URL]` scans everything "
               "the public repo holds (`--allow-identity-hit` as for a push). How the "
               "whole flow fits together: docs/publishing.md.")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--tag")
    ap.add_argument("--skip-verify", action="store_true",
                    help="dry run only — refused with --push. Verify needs "
                         "KB_VERIFY_DATABASE_URL naming a kb_verify* scratch database")
    ap.add_argument("--expect-sha",
                    help="the full sha CI passed; required with --push, must equal --ref")
    ap.add_argument("--ack-gate-changes", metavar="TOKEN",
                    help="acknowledge gate-file / denylist changes since the last publish "
                         "with the token the dry run prints next to that diff")
    ap.add_argument("--remote",
                    help="Push remote override (else KB_PUBLISH_REMOTE env, "
                         "else the manifest's [publish] remote — refused on --push)")
    ap.add_argument("--allow-identity-hit", action="store_true",
                    help="Permit a denylist hit in the publish identity name/email only "
                         "(never a coauthor/message/tag hit) — prints a loud warning")
    args = ap.parse_args(argv)
    return run(_toplevel(), ref=args.ref, push=args.push, tag=args.tag,
               skip_verify=args.skip_verify, remote=args.remote,
               allow_identity_hit=args.allow_identity_hit,
               expect_sha=args.expect_sha, ack_gate_changes=args.ack_gate_changes)


if __name__ == "__main__":
    sys.exit(main())
