"""Commit and push the assembled export tree to the public mirror.

Everything that lands on the public repo is immutable the moment it is
pushed, so this module refuses (PublishRefused) rather than guesses:

- A push needs a `target.Target` that cli.run validated against the pinned
  public repo id. Dry runs may go without one.
- The remote's main must be empty or hold only `sync:` commits authored AND
  committed by the mirror identity in use, with no merges. A web edit,
  a merged PR or a commit by any other identity is listed and refused — the
  next sync would otherwise silently revert it while it stays in history. If
  private state recorded the last public sync commit, history must still
  contain it. Every upstream sha a sync commit names must appear ONCE:
  the identity is a label anyone with write access can type, but only
  kb-publish knows private shas, so a forged sync commit can only repeat an
  upstream that is already public — and building the same upstream twice is
  refused for the same reason.
- A failed clone is never "probably empty" for a push: only when GitHub
  reports the pinned repo has no branches (target.empty) does it start from
  an empty history. (Dry runs keep the old warn-and-continue.)
- The commit is built with plumbing in a scrubbed environment:
  no inherited GIT_* variables, no global/system config, no hooks, no
  signing, TZ=UTC and author/committer dates pinned to 00:00:00 UTC of the
  given date. Blobs are hashed with --no-filters, so in-tree .gitattributes
  or .gitignore can neither rewrite nor drop an exported byte.
- Before pushing, the commit object and the tag object are read back, their
  headers checked exactly (identity, date, no signature, expected parent) and
  their full text deny-scanned.
- A dry run may pass remote=None: the commit is built on an empty history
  and nothing is cloned (the manifest's placeholder remote is never
  contacted). A push always names its validated remote.
- Transport: ssh:// (or any URL) with GIT_SSH_COMMAND honoured, so a push can
  use a dedicated (ideally hardware-backed) deploy key. Global config stays
  in effect for the network steps only (credential helpers), and a url.*.insteadOf /
  pushInsteadOf rewrite of the remote is refused: the URL that was validated
  must be the URL git talks to.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from datetime import date as _date
from datetime import datetime, timezone
from pathlib import Path

from kb_core.publish import deny
from kb_core.publish.target import Target

# Network operations (clone, push) must never sit blocked on a credential
# prompt in a headless run: GIT_TERMINAL_PROMPT=0 makes git fail immediately
# instead of prompting, and the timeout is a backstop against anything else
# that could still hang (a slow/unreachable host).
_NETWORK_TIMEOUT_S = 300

SYNC_PREFIX = "sync: "
# Exactly what cli.run writes as a sync commit's first line (gate.parse_upstream).
SYNC_SUBJECT = re.compile(r"^sync: \d{4}-\d{2}-\d{2} \(upstream ([0-9a-f]{7,64})\)$")

# The only inherited GIT_* variables: how to reach the remote over ssh (the
# deploy key). Everything else — GIT_AUTHOR_*, GIT_COMMITTER_*, GIT_DIR,
# GIT_INDEX_FILE, GIT_CONFIG_PARAMETERS/COUNT/KEY_n — could change what is
# committed or where, so it is dropped.
_INHERITED_GIT_VARS = frozenset({"GIT_SSH_COMMAND", "GIT_SSH", "GIT_SSH_VARIANT"})

# -c flags on every git invocation: no hook runs, nothing gets signed.
_SAFE_CONFIG = ("-c", f"core.hooksPath={os.devnull}", "-c", "commit.gpgsign=false",
                "-c", "tag.gpgsign=false", "-c", "tag.forceSignAnnotated=false")

_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PublishRefused(RuntimeError):
    """kb-publish must stop before (or instead of) writing the public repo."""


def _base_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") or k in _INHERITED_GIT_VARS}
    env.pop("EMAIL", None)          # git's last-resort identity source
    env.update(GIT_TERMINAL_PROMPT="0", TZ="UTC")
    return env


def _network_env() -> dict:
    """Clone/push: global config kept for credential helpers; URL rewrites are
    detected separately (_check_no_rewrite)."""
    return _base_env()


def _local_env() -> dict:
    """Everything that builds objects: no global or system config at all."""
    env = _base_env()
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_ATTR_NOSYSTEM="1")
    return env


def _git(args: list[str], cwd: Path, *, env: dict | None = None,
         timeout: float | None = None, input: str | bytes | None = None,
         text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *_SAFE_CONFIG, *args], cwd=cwd, check=True,
                          capture_output=True, text=text, input=input,
                          env=env if env is not None else _local_env(), timeout=timeout)


def _stderr(exc: subprocess.CalledProcessError) -> str:
    err = exc.stderr or ""
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    return err.strip()[-300:]


def _clean_trailer_part(value: str) -> str:
    """Neutralize a coauthor name/email before it goes into a commit trailer.

    Commit messages have no escaping: an unsanitized "\\n" in a name lets a
    caller forge arbitrary extra trailer lines (e.g. a fake Signed-off-by:)
    that land verbatim and irrevocably on the public mirror. Control chars
    become spaces, whitespace runs collapse to one space, and the ends are
    stripped — the result is always exactly one line.
    """
    cleaned = re.sub(r"[\r\n\x00]", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _check_identity(identity: tuple[str, str]) -> None:
    for part in identity:
        if not part or part != part.strip() or re.search(r"[<>\x00-\x1f\x7f]", part):
            raise PublishRefused(f"publish identity {identity!r} is not a plain one-line "
                                 f"name/email (no <, >, control chars, outer spaces)")


def _check_tag(tag: str) -> None:
    if (not _TAG.match(tag) or ".." in tag or tag.endswith((".", ".lock"))):
        raise PublishRefused(f"tag {tag!r} is not a plain release name "
                             f"([A-Za-z0-9][A-Za-z0-9._-]*, no '..', no .lock)")


def _epoch(day: str | None) -> int:
    """00:00:00 UTC of `day` (YYYY-MM-DD), default today (UTC).

    Pinned to midnight so the public commit reveals nothing finer than the
    date its message already states — not the operator's timezone, not
    the hour a publish ran. The caller passes the upstream commit's UTC date.
    """
    if day is None:
        d = datetime.now(timezone.utc).date()
    else:
        if not _DATE.match(day):
            raise PublishRefused(f"commit date {day!r} is not YYYY-MM-DD")
        try:
            d = _date.fromisoformat(day)
        except ValueError as exc:
            raise PublishRefused(f"commit date {day!r} is not a valid date") from exc
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _clone(remote: str, clone: Path, *, push: bool, target: Target | None) -> None:
    try:
        subprocess.run(["git", *_SAFE_CONFIG, "clone", "-q", "--no-checkout",
                        "--template=", remote, str(clone)],
                       check=True, capture_output=True, env=_network_env(),
                       timeout=_NETWORK_TIMEOUT_S)
        return
    except subprocess.TimeoutExpired as exc:
        raise PublishRefused(
            f"[git_ops] clone of {remote} timed out after {_NETWORK_TIMEOUT_S}s "
            f"(likely a hung credential prompt to a real remote) — refusing rather "
            f"than hang"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = _stderr(exc)
        if ("terminal prompts disabled" in stderr or "could not read Username" in stderr
                or "could not read Password" in stderr):
            raise PublishRefused(
                f"[git_ops] clone of {remote} needs credentials that GIT_TERMINAL_PROMPT=0 "
                f"refused to prompt for — refusing rather than guess whether the remote is "
                f"empty"
            ) from exc
        if push and not (target and target.empty):
            raise PublishRefused(
                f"[git_ops] clone of {remote} failed ({stderr[-200:]}) and GitHub does not "
                f"report the repo empty — refusing to publish over a history it cannot read"
            ) from exc
        if push:
            print(f"[git_ops] clone of {remote} failed ({stderr[-200:]}); GitHub reports "
                  f"{target.full_name} empty (no branches) — starting a fresh history")
        else:
            print(f"[git_ops] clone of {remote} failed ({stderr[-200:]}); assuming empty "
                  f"remote for this dry run — a push refuses unless GitHub reports the "
                  f"repo empty")
    clone.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "--template="], clone)
    _git(["remote", "add", "origin", remote], clone)


def _check_no_rewrite(clone: Path, remote: str) -> None:
    # Compare git's effective URL with the one stored in the clone's own config
    # (what was asked for; clone may have absolutized a relative local path).
    stored = _git(["config", "--get", "remote.origin.url"], clone).stdout.strip()
    for extra in ([], ["--push"]):
        got = _git(["remote", "get-url", *extra, "origin"], clone,
                   env=_network_env()).stdout.strip()
        if got != stored:
            raise PublishRefused(
                f"git config rewrites the remote{' push' if extra else ''} URL {remote} to "
                f"{got} (url.*.{'pushInsteadOf' if extra else 'insteadOf'}) — the validated "
                f"target must be the URL git uses")


def _remote_tip(clone: Path) -> str | None:
    tip = subprocess.run(["git", *_SAFE_CONFIG, "rev-parse", "-q", "--verify",
                          "refs/remotes/origin/main^{commit}"], cwd=clone,
                         capture_output=True, text=True, env=_local_env())
    if tip.returncode == 0:
        return tip.stdout.strip()
    others = [r for r in _git(["for-each-ref", "--format=%(refname)", "refs/remotes/origin"],
                              clone).stdout.split()
              if r != "refs/remotes/origin/HEAD"]
    if others:
        raise PublishRefused(f"the remote has branches but no main ({', '.join(others[:5])}) "
                             f"— not a mirror kb-publish created")
    return None


def _check_history(clone: Path, tip: str | None, identity: tuple[str, str],
                   last_public_sha: str | None, new_subject: str = "") -> None:
    """Remote main must be empty or sync:-only by `identity`, each upstream once."""
    if tip is None:
        if last_public_sha:
            raise PublishRefused(f"the remote is empty, but the last published public commit "
                                 f"was {last_public_sha[:12]} — wrong repo, or its history "
                                 f"was deleted")
        return
    # -z: commits AND fields are NUL-separated, so the output is a flat list
    # of 7 fields per commit; no name or subject can forge a field boundary.
    fmt = "%H%x00%P%x00%an%x00%ae%x00%cn%x00%ce%x00%s"
    fields = _git(["log", "-z", f"--format={fmt}", tip], clone).stdout.split("\x00")
    foreign = []
    upstreams: dict[str, str] = {}      # upstream sha7 -> the sync commit naming it
    for i in range(0, len(fields) - 6, 7):
        sha, parents, an, ae, cn, ce, subject = fields[i:i + 7]
        ok = (subject.startswith(SYNC_PREFIX) and len(parents.split()) <= 1
              and (an, ae) == identity and (cn, ce) == identity)
        if not ok:
            foreign.append(f"{sha[:12]}  {an} <{ae}> / {cn} <{ce}>  {subject}")
            continue
        m = SYNC_SUBJECT.match(subject)
        if m is None:
            continue
        up = m[1][:7]
        if up in upstreams:
            foreign.append(f"{sha[:12]}  names the same upstream {up} as "
                           f"{upstreams[up][:12]} (a forged or repeated sync commit)  "
                           f"{subject}")
        upstreams.setdefault(up, sha)
    if foreign:
        listing = "\n  ".join(foreign[:20])
        raise PublishRefused(
            f"{len(foreign)} commit(s) on public main are not a sync: commit by "
            f"{identity[0]} <{identity[1]}> — publishing would silently revert them while "
            f"they stay in public history. Import or remove them first:\n  {listing}")
    m = SYNC_SUBJECT.match(new_subject)
    if m and m[1][:7] in upstreams:
        raise PublishRefused(f"upstream {m[1][:7]} is already published as "
                             f"{upstreams[m[1][:7]][:12]} — nothing new to publish (a "
                             f"second sync commit for it would look forged)")
    if last_public_sha:
        r = subprocess.run(["git", *_SAFE_CONFIG, "merge-base", "--is-ancestor",
                            last_public_sha, tip], cwd=clone, capture_output=True,
                           env=_local_env())
        if r.returncode != 0:
            raise PublishRefused(f"public main ({tip[:12]}) does not contain the last "
                                 f"published public commit {last_public_sha[:12]} — "
                                 f"history was rewritten, or this is not the same repo")
        if last_public_sha != tip:
            print(f"[git_ops] NOTE — public main moved {last_public_sha[:12]} -> {tip[:12]} "
                  f"since this checkout's last publish (sync commits only)")


def _build_index(clone: Path, tree: Path) -> dict[str, tuple[str, str]]:
    """Index = exactly the export tree, byte for byte; returns path -> (mode, blob)."""
    files = sorted(p for p in tree.rglob("*") if p.is_file() and not p.is_symlink())
    rels = [p.relative_to(tree).as_posix() for p in files]
    for rel in rels:
        if "\n" in rel or "\x00" in rel:
            raise PublishRefused(f"export path {rel!r} contains a newline/NUL")
    shas = _git(["hash-object", "-w", "--no-filters", "--stdin-paths"], clone,
                input="".join(f"{p}\n" for p in files)).stdout.split() if files else []
    entries = {}
    for rel, path, sha in zip(rels, files, shas, strict=True):
        mode = "100755" if os.stat(path).st_mode & 0o111 else "100644"
        entries[rel] = (mode, sha)
    _git(["read-tree", "--empty"], clone)
    if entries:
        _git(["update-index", "-z", "--index-info"], clone,
             input="".join(f"{m} {s}\t{rel}\0" for rel, (m, s) in entries.items()))
    return entries


def _identity_env(identity: tuple[str, str], epoch: int) -> dict:
    stamp = f"@{epoch} +0000"
    env = _local_env()
    env.update(GIT_AUTHOR_NAME=identity[0], GIT_AUTHOR_EMAIL=identity[1],
               GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_NAME=identity[0],
               GIT_COMMITTER_EMAIL=identity[1], GIT_COMMITTER_DATE=stamp)
    return env


def _scan_object(kind: str, text: str, patterns: list[re.Pattern],
                 allow_identity_hit: bool) -> None:
    # Only HEADER identity lines may be excused: they were verified equal to
    # the identity the operator explicitly allowed. Body text never is.
    n_headers = text.split("\n\n", 1)[0].count("\n") + 1
    hits = []
    for h in deny.scan_text(f"{kind} object", text, patterns):
        if (allow_identity_hit and h.line_no <= n_headers
                and h.line.split(" ", 1)[0] in ("author", "committer", "tagger")):
            continue
        hits.append(h)
    if hits:
        shown = "\n  ".join(f"line {h.line_no} [{h.pattern}] {h.line}" for h in hits[:20])
        raise PublishRefused(f"{len(hits)} denylist hit(s) in the {kind} object about to "
                             f"be pushed:\n  {shown}")


def _verify_commit(clone: Path, sha: str, *, parent: str | None, ident_line: str,
                   message: str) -> str:
    raw = _git(["cat-file", "commit", sha], clone, text=False).stdout
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublishRefused("the sync commit object is not UTF-8") from exc
    headers, _, body = text.partition("\n\n")
    tree = _git(["rev-parse", f"{sha}^{{tree}}"], clone).stdout.strip()
    expected = [f"tree {tree}", *([f"parent {parent}"] if parent else []),
                f"author {ident_line}", f"committer {ident_line}"]
    if headers.split("\n") != expected or body != message:
        raise PublishRefused(f"the sync commit is not exactly what kb-publish built (an "
                             f"environment or config override?):\n{text[:600]}")
    return text


def _verify_tree(clone: Path, sha: str, entries: dict[str, tuple[str, str]]) -> None:
    out = _git(["ls-tree", "-r", "-z", "--full-tree", sha], clone).stdout
    got = {}
    for rec in filter(None, out.split("\0")):
        meta, path = rec.split("\t", 1)
        mode, _kind, blob = meta.split()
        got[path] = (mode, blob)
    if got != entries:
        raise PublishRefused("the committed tree differs from the export tree")


def _make_tag(clone: Path, tag: str, sha: str, ident_line: str) -> str:
    # mktag writes exactly these bytes: no tag.gpgsign, no editor, no cleanup.
    body = f"object {sha}\ntype commit\ntag {tag}\ntagger {ident_line}\n\n{tag}\n"
    tag_sha = _git(["mktag"], clone, input=body).stdout.strip()
    _git(["update-ref", f"refs/tags/{tag}", tag_sha, ""], clone)
    if _git(["cat-file", "tag", tag_sha], clone).stdout != body:
        raise PublishRefused("the tag object is not exactly what kb-publish built")
    return body


def publish_tree(tree: Path, remote: str | None, *, message: str, coauthors: list[dict],
                 push: bool, tag: str | None, workdir: Path,
                 deny_patterns: list[re.Pattern],
                 identity: tuple[str, str] = ("ego-OS mirror", "mirror@invalid"),
                 target: Target | None = None, date: str | None = None,
                 last_public_sha: str | None = None,
                 allow_identity_hit: bool = False) -> str:
    """Commit `tree` as one sync commit on the remote's main; push only if `push`.

    `target` is the validated public repo (required for a push), `date` the
    YYYY-MM-DD both commit dates are pinned to (00:00:00 UTC), and
    `last_public_sha` the sync commit the previous push created, if recorded.
    Returns the new commit's sha. Raises PublishRefused on every refusal.
    `remote=None` (dry run only): build against an empty history, no network.
    """
    if push and remote is None:
        raise PublishRefused("a push needs a remote")
    if push and target is None:
        raise PublishRefused("no validated publish target — cli.run must check the remote "
                             "against the pinned public repo id before a push")
    if not deny_patterns:
        raise PublishRefused("no deny patterns — the commit and tag objects cannot be "
                             "scanned (fail-closed)")
    _check_identity(identity)
    if tag is not None:
        _check_tag(tag)
    epoch = _epoch(date)
    ident_line = f"{identity[0]} <{identity[1]}> {epoch} +0000"

    clone = workdir / "clone"
    clone.parent.mkdir(parents=True, exist_ok=True)
    if remote is None:
        clone.mkdir(parents=True, exist_ok=True)
        _git(["init", "-q", "--template="], clone)
        tip = None
    else:
        _clone(remote, clone, push=push, target=target)
        _check_no_rewrite(clone, remote)
        tip = _remote_tip(clone)
        _check_history(clone, tip, identity, last_public_sha,
                       new_subject=message.split("\n", 1)[0])

    trailer_lines = []
    for c in coauthors:
        name = _clean_trailer_part(c["name"])
        email = _clean_trailer_part(c["email"])
        if " " in email or email.count("@") != 1:
            email = "invalid@invalid"
        trailer_lines.append(f"Co-authored-by: {name} <{email}>")
    trailers = "".join(f"\n{line}" for line in trailer_lines)
    full = message + ("\n" + trailers if trailers else "")

    entries = _build_index(clone, tree)
    tree_sha = _git(["write-tree"], clone).stdout.strip()
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".msg",
                                     dir=workdir, delete=False) as fh:
        fh.write(full)
    sha = _git(["commit-tree", "--no-gpg-sign", tree_sha, *(["-p", tip] if tip else []),
                "-F", fh.name], clone, env=_identity_env(identity, epoch)).stdout.strip()
    _git(["update-ref", "refs/heads/main", sha, tip or ""], clone)
    _git(["symbolic-ref", "HEAD", "refs/heads/main"], clone)

    commit_text = _verify_commit(clone, sha, parent=tip, ident_line=ident_line, message=full)
    _verify_tree(clone, sha, entries)
    _scan_object("commit", commit_text, deny_patterns, allow_identity_hit)
    refspecs = ["refs/heads/main:refs/heads/main"]
    if tag is not None:
        tag_text = _make_tag(clone, tag, sha, ident_line)
        _scan_object("tag", tag_text, deny_patterns, allow_identity_hit)
        refspecs.append(f"refs/tags/{tag}:refs/tags/{tag}")

    if push:
        try:
            subprocess.run(["git", *_SAFE_CONFIG, "push", "-q", "--no-verify", "--atomic",
                            "origin", *refspecs], cwd=clone, check=True,
                           capture_output=True, text=True, env=_network_env(),
                           timeout=_NETWORK_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise PublishRefused(
                f"[git_ops] push to {remote} timed out after {_NETWORK_TIMEOUT_S}s "
                f"(likely a hung credential prompt to a real remote) — refusing rather "
                f"than hang"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise PublishRefused(f"[git_ops] push to {remote} failed: {_stderr(exc)}") from exc
    return sha
