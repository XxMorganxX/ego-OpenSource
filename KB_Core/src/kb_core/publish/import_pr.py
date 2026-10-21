"""kb-import-pr: bring a public-mirror PR into the private repo — only what it may touch.

Community PRs are never merged on the public mirror; their patch is applied
here, reviewed, merged to private main, and shipped by the next kb-publish.
That makes this tool the one door from the public internet into the private
repo. Applied as-is, a PR that added `private/**` to the manifest would ride
the next routine publish out together with every private file. So the patch
is checked BEFORE anything is applied:

- every path it touches — both sides of a rename, the source of a copy (which
  `git apply` reads from this checkout), deletions — must be selected by the
  manifest as it is on private origin/main, i.e. BEFORE the patch;
- control files (the publish gate, CI, agent policy, things that run code on
  install/test: conftest.py, *.sh, lockfiles, pyproject/package.json scripts,
  entry points and dependencies, executable bits) are refused unless
  --allow-control-files, which prints their full diff and still needs --yes;
- symlinks, submodules, binary content (no text to scan), unsafe paths and
  anything the parser cannot read unambiguously are refused, always;
- the added lines and file metadata are deny-scanned and structurally scanned.

The patch is then applied with `git am` (no 3-way, no hooks) onto a NEW branch
import/pr-<n> in a NEW worktree made from origin/main — never onto whatever
the shared checkout has out — and the result is re-checked from git's own
diff, so a path the parser missed cannot slip through. Nothing from the patch
is executed: tests run only after a human has reviewed the worktree.

Credit comes from GitHub's record of who opened the PR (`gh pr view --json
author`), never from the patch's spoofable `From:` header. An author
whose identity hits the denylist is not recorded (with a warning) rather than
left to wedge every later publish.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import NamedTuple

from kb_core.publish import deny, gate, manifest, state, structural, target
from kb_core.publish import github as github_api


class ImportRefused(RuntimeError):
    """kb-import-pr must stop; nothing it created is left behind."""


class PatchError(ImportRefused):
    """The patch can't be read unambiguously — so it is not applied."""


class PrAuthor(NamedTuple):
    login: str
    name: str
    user_id: int


# --- parsing ----------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_MODE_LINE = re.compile(r"^(old mode|new mode|deleted file mode|new file mode) (\d{6})$")
_INDEX = re.compile(r"^index [0-9a-f]{7,64}\.\.[0-9a-f]{7,64}(?: (\d{6}))?$")
_SIMILARITY = re.compile(r"^(?:dis)?similarity index \d{1,3}%$")
_MOVES = ("rename from", "rename to", "copy from", "copy to")
_KNOWN_MODES = frozenset({"100644", "100755", "120000", "160000"})
SYMLINK, GITLINK, EXECUTABLE = "120000", "160000", "100755"


@dataclass(frozen=True)
class FileChange:
    """One `diff --git` section. For a copy, old_path is the SOURCE (read, not written)."""
    kind: str                   # add | delete | modify | rename | copy
    old_path: str | None
    new_path: str | None
    old_mode: str | None
    new_mode: str | None
    binary: bool
    meta: tuple[str, ...]       # the section's header lines (paths, modes)
    added: tuple[str, ...]      # added content lines, without the leading '+'

    @property
    def written(self) -> frozenset[str]:
        paths = {self.new_path} if self.kind == "copy" else {self.old_path, self.new_path}
        return frozenset(p for p in paths if p)

    @property
    def touched(self) -> frozenset[str]:
        return frozenset(p for p in (self.old_path, self.new_path) if p)

    @property
    def label(self) -> str:
        return self.new_path or self.old_path or "?"


def _file_header_path(value: str) -> str:
    # git appends a TAB to ---/+++ names that contain a space.
    value = value[:-1] if value.endswith("\t") else value
    if value.startswith('"'):
        raise PatchError(f"quoted path {value!r} (special characters) — not supported")
    return value


def _plain(value: str, what: str) -> str:
    if value.startswith('"'):
        raise PatchError(f"quoted path in {what} ({value!r}) — not supported")
    return value


def _split_git_line(rest: str) -> str:
    """The path X of `diff --git a/X b/X` (no rename/copy), even when X has spaces."""
    n, odd = divmod(len(rest) - 5, 2)
    if rest.startswith('"'):
        raise PatchError(f"quoted path in 'diff --git {rest}' — not supported")
    if odd or n < 1 or rest != f"a/{rest[2:2 + n]} b/{rest[2:2 + n]}":
        raise PatchError(f"cannot read the paths of 'diff --git {rest}'")
    return rest[2:2 + n]


def _parse_section(lines: list[str], i: int) -> tuple[FileChange, int]:
    header = lines[i]
    rest = header[len("diff --git "):]
    meta, added = [header], []
    modes: dict[str, str] = {}
    moves: dict[str, str] = {}
    minus = plus = None
    binary = has_hunks = False
    i += 1
    n = len(lines)
    while i < n:
        line = lines[i]
        if m := _MODE_LINE.match(line):
            modes[m[1]] = m[2]
        elif move := next((k for k in _MOVES if line.startswith(k + " ")), None):
            if move in moves:
                raise PatchError(f"repeated '{move}' in 'diff --git {rest}'")
            moves[move] = _plain(line[len(move) + 1:], move)
        elif _SIMILARITY.match(line):
            pass
        elif m := _INDEX.match(line):
            if m[1]:
                modes["index"] = m[1]
        elif line.startswith("--- ") and minus is None and plus is None:
            minus = _file_header_path(line[4:])
        elif line.startswith("+++ ") and minus is not None and plus is None:
            plus = _file_header_path(line[4:])
        elif line.startswith("Binary files ") and line.endswith(" differ"):
            binary = True
            meta.append(line)
            i += 1
            break
        elif line == "GIT binary patch":
            binary = True
            meta.append(line)
            i += 1
            # base85 payload: never starts with "diff --git " (no space in the alphabet)
            while i < n and not lines[i].startswith("diff --git ") and lines[i] != "-- ":
                i += 1
            break
        elif line.startswith("@@"):
            has_hunks = True
            break
        else:
            break
        meta.append(line)
        i += 1

    while has_hunks and i < n and lines[i].startswith("@@"):
        m = _HUNK.match(lines[i])
        if not m:
            raise PatchError(f"malformed hunk header {lines[i][:80]!r}")
        old = int(m[2]) if m[2] is not None else 1
        new = int(m[4]) if m[4] is not None else 1
        i += 1
        while old > 0 or new > 0:
            if i >= n:
                raise PatchError(f"truncated hunk in 'diff --git {rest}'")
            line = lines[i]
            if line.startswith("+"):
                new -= 1
                added.append(line[1:])
            elif line.startswith("-"):
                old -= 1
            elif line.startswith(" ") or line == "":
                old -= 1
                new -= 1
            elif not line.startswith("\\"):
                raise PatchError(f"malformed hunk line {line[:80]!r} in 'diff --git {rest}'")
            if old < 0 or new < 0:
                raise PatchError(f"hunk longer than its header in 'diff --git {rest}'")
            i += 1
        while i < n and lines[i].startswith("\\"):
            i += 1

    for mode in modes.values():
        if mode not in _KNOWN_MODES:
            raise PatchError(f"unknown file mode {mode} in 'diff --git {rest}'")
    if "rename from" in moves or "rename to" in moves:
        kind, src, dst = "rename", moves.get("rename from"), moves.get("rename to")
    elif "copy from" in moves or "copy to" in moves:
        kind, src, dst = "copy", moves.get("copy from"), moves.get("copy to")
    else:
        kind = src = dst = None
    if kind:
        if not src or not dst or len(moves) != 2 or rest != f"a/{src} b/{dst}":
            raise PatchError(f"inconsistent {kind} header in 'diff --git {rest}'")
        old_path, new_path = src, dst
    else:
        old_path = new_path = _split_git_line(rest)
        if "new file mode" in modes and "deleted file mode" in modes:
            raise PatchError(f"'diff --git {rest}' is both a new and a deleted file")
        if "new file mode" in modes:
            kind, old_path = "add", None
        elif "deleted file mode" in modes:
            kind, new_path = "delete", None
        else:
            kind = "modify"
    if has_hunks and (minus is None or plus is None):
        raise PatchError(f"hunks without ---/+++ lines in 'diff --git {rest}'")
    if minus is not None:
        want_minus = "/dev/null" if old_path is None else f"a/{old_path}"
        want_plus = "/dev/null" if new_path is None else f"b/{new_path}"
        if (minus, plus) != (want_minus, want_plus):
            raise PatchError(f"file header {minus!r} -> {plus!r} disagrees with "
                             f"'diff --git {rest}'")
    old_mode = modes.get("deleted file mode") or modes.get("old mode") or (
        modes.get("index") if old_path else None)
    new_mode = modes.get("new file mode") or modes.get("new mode") or (
        modes.get("index") if new_path else None)
    return FileChange(kind, old_path, new_path, old_mode, new_mode, binary,
                      tuple(meta), tuple(added)), i


def parse_patch(text: str) -> list[FileChange]:
    """Every file a mailbox patch (or a plain `git diff`) would change.

    Fail closed: anything `git apply` might act on that this parser cannot read
    unambiguously — quoted names, a traditional (non-git) diff, a hunk whose
    length disagrees with its header — raises PatchError.
    """
    if "\0" in text:
        raise PatchError("patch contains a NUL byte")
    lines = text.split("\n")
    changes: list[FileChange] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            change, i = _parse_section(lines, i)
            changes.append(change)
            continue
        if ((line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "))
                or (line.startswith("diff ") and not line.startswith("diff --git "))
                or line.startswith(("@@ -", "GIT binary patch"))):
            raise PatchError(f"non-git diff text outside a 'diff --git' section: {line[:80]!r}")
        i += 1
    return changes


# --- policy -----------------------------------------------------------------------

def path_problem(path: str) -> str | None:
    """Why `path` may not appear in an imported patch at all, or None.

    Printable ASCII only: git quotes anything else, and non-ASCII names invite
    look-alikes (macOS ignores some code points, so `.git\\u200c` IS `.git`).
    """
    if not path:
        return "empty path"
    if any(not (" " <= c <= "~") for c in path):
        return "non-printable or non-ASCII character in path"
    if path.startswith("/"):
        return "absolute path"
    if "\\" in path or '"' in path:
        return "backslash or quote in path"
    for seg in path.split("/"):
        if seg in ("", ".", ".."):
            return f"path segment {seg!r} (traversal / empty segment)"
        if seg != seg.strip():
            return "path segment with leading/trailing space"
        if seg.rstrip(". ").casefold() == ".git":
            return "writes into a .git directory"
    return None


_AGENT_POLICY = frozenset({"agents.md", "gemini.md", ".cursorrules", ".windsurfrules",
                           "copilot-instructions.md"})
_TOOL_DIRS = frozenset({".github", ".claude", ".cursor", ".vscode", ".devcontainer",
                        ".husky", ".githooks"})
_RUNS_CODE = {
    "conftest.py": "pytest conftest (runs at test collection)",
    "sitecustomize.py": "runs at interpreter start",
    "usercustomize.py": "runs at interpreter start",
    "setup.py": "runs at install", "setup.cfg": "install config",
    "noxfile.py": "test runner config", "tox.ini": "test runner config",
    "makefile": "build recipe", ".npmrc": "npm registry/scripts config",
    "uv.lock": "dependency lockfile", "poetry.lock": "dependency lockfile",
    "pipfile": "dependency manifest", "pipfile.lock": "dependency lockfile",
    "package-lock.json": "dependency lockfile", "yarn.lock": "dependency lockfile",
    "pnpm-lock.yaml": "dependency lockfile", "bun.lockb": "dependency lockfile",
}
DEPENDENCY_MANIFESTS = frozenset({"pyproject.toml", "package.json"})
_JS_EXTS = frozenset({"js", "ts", "mjs", "cjs", "mts", "cts"})


def control_reason(path: str) -> str | None:
    """Why `path` is a control file (needs --allow-control-files), by name alone."""
    low = path.casefold()
    parts = low.split("/")
    name = parts[-1]
    if gate.is_gate_file(path) or fnmatchcase(name, "publish.*.toml"):
        return "publish gate file"
    if dirs := _TOOL_DIRS.intersection(parts[:-1]):
        return f"CI / agent / tool config ({sorted(dirs)[0]}/)"
    if name in _AGENT_POLICY or (name.startswith("claude") and name.endswith(".md")):
        return "agent-policy file"
    if name in _RUNS_CODE:
        return _RUNS_CODE[name]
    if name.endswith((".sh", ".bash", ".zsh")):
        return "shell script"
    if name.endswith(".pth"):
        return "runs at interpreter start (.pth)"
    if fnmatchcase(name, "requirements*.txt"):
        return "dependency list"
    if name.startswith(("dockerfile", "docker-compose")) or name.endswith(".mk"):
        return "build recipe"
    if fnmatchcase(name, "*.config.*") and name.rsplit(".", 1)[-1] in _JS_EXTS:
        return "JS build/test config (runs code)"
    if low.endswith("src/test/setup.ts") or fnmatchcase(name, "setuptests.*"):
        return "test setup (runs before every test)"
    return None


@dataclass
class Review:
    refusals: list[str] = field(default_factory=list)            # never overridable
    control: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    hits: list[deny.Hit] = field(default_factory=list)            # never overridable
    candidates: set[str] = field(default_factory=set)             # pyproject/package.json

    @property
    def blocked(self) -> bool:
        return bool(self.refusals or self.hits)


def review_changes(changes: list[FileChange], m: manifest.Manifest,
                   patterns: list[re.Pattern],
                   allows: tuple[deny.Allow, ...] = ()) -> Review:
    """Apply the import policy to parsed changes. `m` is the PRE-patch manifest.

    `allows` ([[allow]] literals) excuse a match in added content only when it
    lies entirely inside an allowed occurrence, scoped by the path the content
    lands at; never in paths or diff headers."""
    r = Review()
    for c in changes:
        for p in sorted(c.touched):
            if problem := path_problem(p):
                r.refusals.append(f"{p!r}: {problem}")
                continue
            if not manifest.select([p], m):
                why = manifest.hard_deny_reason(p)
                r.refusals.append(f"{p}: not exported by publish.manifest.toml at origin/main"
                                  + (f" ({why})" if why else "")
                                  + " — a public PR may only touch exported paths")
        modes = {c.old_mode, c.new_mode}
        if SYMLINK in modes:
            r.refusals.append(f"{c.label}: symlink (mode 120000) — never imported")
        if GITLINK in modes:
            r.refusals.append(f"{c.label}: submodule (gitlink, mode 160000) — never imported")
        if c.binary and c.kind != "delete":
            r.refusals.append(f"{c.label}: binary content — it cannot be deny-scanned; add "
                              f"it by hand after review if it is wanted")
        for p in sorted(c.touched):
            if (why := control_reason(p)) and (p, why) not in r.control:
                r.control.append((p, why))
        if (c.new_path and c.new_mode == EXECUTABLE and c.old_mode != EXECUTABLE
                and not control_reason(c.new_path)):
            r.control.append((c.new_path, "executable bit set"))
        for p in c.written:
            if p.rsplit("/", 1)[-1].casefold() in DEPENDENCY_MANIFESTS:
                r.candidates.add(p)
        r.hits.extend(deny.scan_text(c.label, "\n".join(c.meta), patterns))
        for p in sorted(c.touched):
            r.hits.extend(deny.scan_text(p, p, patterns))
        if c.added:
            added = "\n".join(c.added)
            r.hits.extend(deny.scan_allowed(c.label, added, patterns,
                                            allows if c.new_path else (),
                                            scope=c.new_path))
            r.hits.extend(structural.scan_content(c.label, added))
    return r


# --- dependency manifests ---------------------------------------------------------

_PYPROJECT_SAFE = {
    "project": frozenset({"description", "version", "readme", "keywords", "classifiers",
                          "urls", "authors", "maintainers", "license", "license-files"}),
    "tool": frozenset({"ruff"}),
}
_PACKAGE_JSON_SAFE = frozenset({"description", "version", "keywords", "homepage", "bugs",
                                "license", "author", "contributors", "repository", "funding"})


def _changed(a: dict, b: dict, safe: dict | frozenset, prefix: str = "") -> list[str]:
    out = []
    for key in sorted(set(a) | set(b)):
        if isinstance(safe, frozenset):
            if key not in safe and a.get(key) != b.get(key):
                out.append(prefix + key)
        elif key in safe and isinstance(a.get(key), dict) and isinstance(b.get(key), dict):
            out.extend(_changed(a[key], b[key], safe[key], f"{prefix}{key}."))
        elif a.get(key) != b.get(key):
            out.append(prefix + key)
    return out


def dependency_manifest_reason(rel: str, before: bytes | None,
                               after: bytes | None) -> str | None:
    """Why a pyproject.toml / package.json change is a control change, or None.

    Allowlist, not blocklist: only descriptive keys may change without
    --allow-control-files. Scripts, entry points, dependencies, build backends,
    pytest addopts — anything else that could run code on install or test — is
    control, and so is anything that doesn't parse.
    """
    name = rel.rsplit("/", 1)[-1].casefold()
    if before is None or after is None:
        return f"{name} {'added' if before is None else 'deleted'}"
    try:
        if name == "pyproject.toml":
            a, b = tomllib.loads(before.decode()), tomllib.loads(after.decode())
            changed = _changed(a, b, _PYPROJECT_SAFE)
        else:
            a, b = json.loads(before), json.loads(after)
            if not isinstance(a, dict) or not isinstance(b, dict):
                return f"{name} is not a JSON object"
            changed = _changed(a, b, _PACKAGE_JSON_SAFE)
    except (ValueError, UnicodeDecodeError) as e:
        return f"{name} does not parse ({type(e).__name__})"
    return f"changes {', '.join(changed)}" if changed else None



# --- GitHub seams -----------------------------------------------------------------

_GH_TIMEOUT_S = 120
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def gh_pr_patch(repo: str, pr: int, *, run=subprocess.run) -> str:
    """Default seam: the PR's mailbox patch (`gh pr diff N --patch`, read-only)."""
    try:
        r = run(["gh", "pr", "diff", str(pr), "--patch", "--repo", repo],
                capture_output=True, env=github_api._gh_env(), timeout=_GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise github_api.GitHubError(f"gh pr diff {pr} could not run: {exc}") from exc
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else r.stderr
        raise github_api.GitHubError(f"gh pr diff {pr} failed: {(err or '').strip()[-200:]}")
    try:
        return r.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImportRefused(f"PR #{pr}'s patch is not UTF-8 — it cannot be scanned") from exc


def _gh_json(cmd: list[str], run) -> object:
    try:
        r = run(cmd, capture_output=True, text=True, env=github_api._gh_env(),
                timeout=_GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise github_api.GitHubError(f"{' '.join(cmd[:3])} could not run: {exc}") from exc
    if r.returncode != 0:
        raise github_api.GitHubError(f"{' '.join(cmd[:4])} failed: {(r.stderr or '').strip()[-200:]}")
    try:
        return json.loads(r.stdout)
    except ValueError as exc:
        raise github_api.GitHubError(f"{' '.join(cmd[:4])} returned non-JSON output") from exc


def gh_pr_author(repo: str, pr: int, *, run=subprocess.run) -> PrAuthor:
    """Default seam: who GitHub says opened the PR, with their numeric user id.

    `gh pr view --json author` gives login and display name (its `id` is a
    GraphQL node id); the numeric id for the noreply address comes from
    `gh api users/<login>`. Both GET-only. Bots and odd answers raise.
    """
    data = _gh_json(["gh", "pr", "view", str(pr), "--repo", repo, "--json", "author"], run)
    author = data.get("author") if isinstance(data, dict) else None
    if not isinstance(author, dict) or author.get("is_bot"):
        raise github_api.GitHubError(f"PR #{pr} has no human author on record")
    login, name = author.get("login"), author.get("name") or ""
    if not isinstance(login, str) or not _LOGIN.match(login) or not isinstance(name, str):
        raise github_api.GitHubError(f"PR #{pr}: unexpected author login {login!r}")
    user = _gh_json(["gh", "api", f"users/{login}"], run)
    if (not isinstance(user, dict) or type(user.get("id")) is not int
            or str(user.get("login", "")).casefold() != login.casefold()):
        raise github_api.GitHubError(f"users/{login}: response lacks a matching integer id")
    return PrAuthor(login=login, name=name, user_id=user["id"])


@dataclass
class Seams:
    """Everything kb-import-pr asks the outside world, swappable in tests.

    fetch_patch   (repo, pr) -> mailbox patch text
    pr_author     (repo, pr) -> PrAuthor
    fetch_origin  refresh origin/main (the base of the import branch)
    confirm       ask a human to approve control-file changes; None = a tty prompt
    """
    fetch_patch: Callable[[str, int], str] = lambda repo, pr: gh_pr_patch(repo, pr)
    pr_author: Callable[[str, int], PrAuthor] = lambda repo, pr: gh_pr_author(repo, pr)
    fetch_origin: Callable[[Path], None] = lambda root: gate.fetch_origin(root)
    confirm: Callable[[str], bool] | None = None


# --- credit -----------------------------------------------------------------------

def coauthor(author: PrAuthor) -> dict | None:
    """The Co-authored-by entry for a PR author, or None when it can't be trusted.

    The email is GitHub's noreply form `<id>+<login>@users.noreply.github.com`,
    which GitHub attributes to the account and which reveals no address.
    """
    if not _LOGIN.match(author.login) or type(author.user_id) is not int or author.user_id <= 0:
        return None
    name = re.sub(r"[<>\x00-\x1f\x7f]", " ", author.name or "")
    name = re.sub(r"\s+", " ", name).strip() or author.login
    return {"name": name, "email": f"{author.user_id}+{author.login}@users.noreply.github.com"}


def record_authors(root: Path, authors: list[dict], *, pr: int | None = None) -> None:
    """Queue credit for the next sync commit, one entry per email. `pr` is kept
    on the entry (`prs`) so the push can print the PRs to close."""
    st = state.load(root)
    by_email = {c["email"]: c for c in st["pending_coauthors"]}
    for a in authors:
        entry = by_email.get(a["email"])
        if entry is None:
            entry = by_email[a["email"]] = dict(a)
            st["pending_coauthors"].append(entry)
        if pr is not None and pr not in entry.setdefault("prs", []):
            entry["prs"].append(pr)
    state.save(root, st)


def _credit(root: Path, repo: str, pr: int, seams: Seams,
            patterns: list[re.Pattern]) -> str:
    try:
        author = seams.pr_author(repo, pr)
    except (github_api.GitHubError, ImportRefused) as e:
        return (f"WARNING — could not read PR #{pr}'s author ({e}); the contributor will NOT "
                f"be credited unless you add them to private/publish-state.json by hand")
    entry = coauthor(author)
    if entry is None:
        return (f"WARNING — PR #{pr}'s author ({author.login!r}) is not a usable GitHub user; "
                f"the contributor will NOT be credited")
    hits = [h for v in (entry["name"], entry["email"], author.login)
            for h in deny.scan_text("author", v, patterns)]
    if hits:
        # kb-publish refuses any pending coauthor that hits the denylist, so
        # recording this one would block every later publish.
        return (f"WARNING — PR #{pr}'s author identity matches the denylist; NOT recording a "
                f"Co-authored-by trailer for them (thank them in the PR thread instead)")
    record_authors(root, [entry], pr=pr)
    return f"credit: {entry['name']} <{entry['email']}> (from GitHub's PR record)"


# --- git --------------------------------------------------------------------------

_HOOKLESS = ("-c", f"core.hooksPath={os.devnull}", "-c", "am.threeWay=false",
             "-c", "core.quotePath=true", "-c", "diff.noprefix=false",
             "-c", "diff.mnemonicPrefix=false", "-c", "diff.external=")


def _env() -> dict:
    # Inherited GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE / config overrides
    # could point the import at another checkout or turn hooks back on.
    env = {k: v for k, v in os.environ.items()
           if not (k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                         "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                         "GIT_NAMESPACE", "GIT_EXTERNAL_DIFF", "GIT_CONFIG_PARAMETERS",
                         "GIT_CONFIG_COUNT")
                   or k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")))}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(cwd: Path, *args: str, input: bytes | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *_HOOKLESS, *args], cwd=cwd, input=input, check=check,
                          capture_output=True, env=_env())


def _out(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.decode("utf-8", "replace")


def _err(exc: subprocess.CalledProcessError) -> str:
    return (exc.stderr or b"").decode("utf-8", "replace").strip()[-500:]


def _toplevel() -> Path:
    return Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                               check=True).stdout.decode().strip())


def _main_root(root: Path) -> Path:
    """The primary checkout (parent of the shared .git), where .claude/worktrees lives."""
    common = Path(_out(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    if common.name != ".git":
        raise ImportRefused(f"cannot place the import worktree: git dir {common} is not a "
                            f"checkout's .git")
    return common.parent


def _conflict_advice(root: Path, base: str, changes: list[FileChange]) -> str:
    """What will actually resolve a failed apply.

    The contributor's base is PUBLIC main. If private main changed the paths
    the PR touches since the last publish, a rebase cannot help until those
    changes are published.
    """
    last = state.load(root)["last_published_ref"]
    touched = sorted({p for c in changes for p in c.touched})
    if last and touched:
        r = _git(root, "diff", "--name-only", "-z", last, base, "--", *touched, check=False)
        moved = [p for p in r.stdout.decode().split("\0") if p] if r.returncode == 0 else []
        if moved:
            return (f"private main changed {', '.join(moved[:5])} since the last publish "
                    f"({last[:7]}) — the contributor's base is public main, so a rebase "
                    f"cannot help: publish first (or resolve by hand), then ask them to "
                    f"rebase on public main")
    return "ask the contributor to rebase on public main"


def _show(cwd: Path, rev: str, rel: str) -> bytes | None:
    r = _git(cwd, "cat-file", "blob", f"{rev}:{rel}", check=False)
    return r.stdout if r.returncode == 0 else None


def _gates_at(root: Path, base: str) -> tuple[manifest.Manifest, list[re.Pattern],
                                              tuple[deny.Allow, ...]]:
    """The manifest and denylist as committed on origin/main — the PRE-patch gate."""
    m_bytes = _show(root, base, "publish.manifest.toml")
    d_bytes = _show(root, base, "publish.deny.toml")
    if d_bytes is None:
        raise ImportRefused("no publish.deny.toml on origin/main — kb-import-pr is a "
                            "private-repo operation")
    if m_bytes is None:
        raise ImportRefused("no publish.manifest.toml on origin/main")
    with tempfile.TemporaryDirectory(prefix="kb-import-gate-") as td:
        mp, dp = Path(td) / "m.toml", Path(td) / "d.toml"
        mp.write_bytes(m_bytes)
        dp.write_bytes(d_bytes)
        try:
            m = manifest.load_manifest(mp)
            patterns = deny.load_patterns(dp)
            allows = deny.load_allows(dp, patterns)
        except (ValueError, KeyError, re.error) as e:
            raise ImportRefused(f"the gate files on origin/main do not load: {e}") from e
    if not patterns:
        raise ImportRefused("publish.deny.toml on origin/main has zero patterns")
    return m, patterns, allows


def _source_repo(root: Path) -> str:
    """owner/name of the PUBLIC mirror whose PRs may be imported."""
    env = os.environ.get("KB_PUBLISH_REMOTE")
    pinned = state.load(root)["public_repo_full_name"]
    if env:
        repo = github_api.parse_remote(env)
        if repo is None:
            raise ImportRefused(f"KB_PUBLISH_REMOTE ({env}) is not a github.com owner/name "
                                f"URL")
        if pinned and repo.casefold() != pinned.casefold():
            raise ImportRefused(f"KB_PUBLISH_REMOTE names {repo}, but the pinned public repo "
                                f"is {pinned}")
    elif pinned:
        repo = pinned
    else:
        raise ImportRefused("no KB_PUBLISH_REMOTE set and no pinned public repo "
                            "(`kb-publish pin`); the manifest remote is a fork-facing "
                            "placeholder, not a trusted source of patches")
    try:
        origin = target.origin_full_name(root)
    except target.TargetError as e:
        raise ImportRefused(str(e)) from e
    if repo.casefold() == origin.casefold():
        raise ImportRefused(f"{repo} is the private origin — kb-import-pr imports PRs from "
                            f"the public mirror only")
    private = state.load(root)["private_repo_full_name"]
    if private and origin.casefold() != private.casefold():
        # The patch is applied onto origin/main — a swapped origin would
        # base the import (and its control-file review) on someone else's tree.
        raise ImportRefused(f"origin names {origin}, not the pinned private repo {private}")
    return repo


def _remove(root: Path, wt: Path, branch: str) -> None:
    """Undo a refused import: the worktree and the branch, nothing else."""
    _git(root, "worktree", "remove", "--force", "--force", str(wt), check=False)
    if wt.exists() and wt.parent.name == "worktrees" and wt.name.startswith("import-pr-"):
        shutil.rmtree(wt, ignore_errors=True)
    _git(root, "worktree", "prune", check=False)
    _git(root, "branch", "-D", branch, check=False)


def verify_applied(wt: Path, base: str, expected: list[FileChange], m: manifest.Manifest,
                   patterns: list[re.Pattern],
                   allows: tuple[deny.Allow, ...] = ()) -> list[tuple[str, str]]:
    """Re-check what `git am` actually committed; return its control-file list.

    The backstop for the parser: git's own diff of base..HEAD must change
    exactly the paths the patch parse saw, and passes the same policy. Raises
    ImportRefused otherwise.
    """
    if _out(wt, "rev-list", "--merges", f"{base}..HEAD").strip():
        raise ImportRefused("the applied history contains a merge commit")
    text = _out(wt, "diff", "--no-color", "--no-renames", "--no-ext-diff", "--no-textconv",
                "--src-prefix=a/", "--dst-prefix=b/", base, "HEAD")
    try:
        actual = parse_patch(text)
    except PatchError as e:
        raise ImportRefused(f"git's diff of the applied commits is unreadable: {e}") from e
    want = frozenset().union(*(c.written for c in expected))
    got = frozenset().union(*(c.written for c in actual))
    if want != got:
        extra, missing = sorted(got - want), sorted(want - got)
        raise ImportRefused("the applied commits do not match the reviewed patch"
                            + (f"; unexpected: {', '.join(extra)}" if extra else "")
                            + (f"; missing: {', '.join(missing)}" if missing else ""))
    review = review_changes(actual, m, patterns, allows)
    if review.blocked:
        raise ImportRefused("; ".join(review.refusals
                                      + [f"denylist/structural hit in {h.path}"
                                         for h in review.hits]))
    control = list(review.control)
    for rel in sorted(review.candidates):
        if why := dependency_manifest_reason(rel, _show(wt, base, rel), _show(wt, "HEAD", rel)):
            control.append((rel, why))
    return control


def _printable(line: str) -> str:
    # Control-file diffs go straight to a terminal: a patch must not be able to
    # repaint the screen (or hide its own lines) while it is being reviewed.
    return "".join(c if c.isprintable() or c == "\t" else repr(c)[1:-1] for c in line)


def _print_hits(hits: list[deny.Hit]) -> None:
    for h in hits[:20]:
        print(f"  {h.path}: [{h.pattern}] {_printable(h.line)}")
    if len(hits) > 20:
        print(f"  ... and {len(hits) - 20} more")


# --- the command ------------------------------------------------------------------

def _refused(msg: str) -> int:
    print(f"kb-import-pr: REFUSED — {msg}")
    return 1


def run(root: Path, pr: int, *, allow_control_files: bool = False, yes: bool = False,
        seams: Seams | None = None) -> int:
    seams = seams or Seams()
    if pr <= 0:
        return _refused(f"{pr} is not a PR number")
    try:
        repo = _source_repo(root)
    except ImportRefused as e:
        return _refused(str(e))
    print(f"kb-import-pr: source = {repo} PR #{pr}")
    branch = f"import/pr-{pr}"
    try:
        wt = _main_root(root) / ".claude" / "worktrees" / f"import-pr-{pr}"
    except ImportRefused as e:
        return _refused(str(e))
    if os.path.lexists(wt):
        return _refused(f"{wt} already exists — remove it (git worktree remove) first")
    if _git(root, "rev-parse", "-q", "--verify", f"refs/heads/{branch}",
            check=False).returncode == 0:
        return _refused(f"branch {branch} already exists — delete it first")

    try:
        seams.fetch_origin(root)
    except (gate.GateError, OSError, subprocess.SubprocessError) as e:
        return _refused(f"could not fetch origin/main: {e}")
    r = _git(root, "rev-parse", "-q", "--verify", f"{gate.ORIGIN_MAIN}^{{commit}}", check=False)
    if r.returncode != 0:
        return _refused(f"{gate.ORIGIN_MAIN} does not exist")
    base = r.stdout.decode().strip()
    try:
        m, patterns, allows = _gates_at(root, base)
        text = seams.fetch_patch(repo, pr)
        changes = parse_patch(text)
    except (ImportRefused, github_api.GitHubError) as e:
        return _refused(str(e))
    if not changes:
        return _refused(f"PR #{pr}'s patch changes no files")

    review = review_changes(changes, m, patterns, allows)
    if review.blocked:
        print(f"kb-import-pr: REFUSED — PR #{pr} touches what a public PR may not "
              f"({len(review.refusals)} path problem(s), {len(review.hits)} "
              f"denylist/structural hit(s); never overridable):")
        for msg in review.refusals:
            print(f"  {msg}")
        _print_hits(review.hits)
        return 1
    if review.control and not allow_control_files:
        return _control_refusal(pr, review.control)

    print(f"kb-import-pr: applying {len(changes)} file change(s) onto {branch} at "
          f"origin/main {base[:7]} in {wt}")
    try:
        _git(root, "worktree", "add", "-q", "-b", branch, str(wt), base)
    except subprocess.CalledProcessError as e:
        _remove(root, wt, branch)
        return _refused(f"git worktree add failed: {_err(e)}")
    try:
        try:
            _git(wt, "am", "--no-3way", "--quiet", input=text.encode("utf-8"))
        except subprocess.CalledProcessError as e:
            raise ImportRefused(f"the patch did not apply cleanly onto origin/main "
                                f"{base[:7]} ({_conflict_advice(root, base, changes)}):\n"
                                f"{_err(e)}")
        control = verify_applied(wt, base, changes, m, patterns, allows)
        if control and not allow_control_files:
            raise ImportRefused(_control_text(pr, control))
        if control:
            _show_control_diffs(wt, base, control)
            if not yes:
                confirm = seams.confirm
                if confirm is None:
                    if not sys.stdin.isatty():
                        raise ImportRefused("control-file changes need --yes (or a terminal "
                                            "to confirm) after reading the diffs above")
                    confirm = _ask
                if not confirm(f"Import these {len(control)} control-file change(s)? [y/N] "):
                    raise ImportRefused("not confirmed")
    except ImportRefused as e:
        _remove(root, wt, branch)
        return _refused(f"{e} — removed {wt} and branch {branch}")
    commits = _out(wt, "rev-list", "--count", f"{base}..HEAD").strip()
    credit = _credit(root, repo, pr, seams, patterns)
    _report(pr, branch, wt, base, commits, credit, bool(control))
    return 0


def _control_text(pr: int, control: list[tuple[str, str]]) -> str:
    lines = [f"PR #{pr} changes control file(s):"]
    lines += [f"  {p}: {why}" for p, why in control]
    lines.append("Re-run with --allow-control-files to see their full diffs and confirm "
                 "(--yes); review them before any test runs.")
    return "\n".join(lines)


def _control_refusal(pr: int, control: list[tuple[str, str]]) -> int:
    return _refused(_control_text(pr, control))


def _show_control_diffs(wt: Path, base: str, control: list[tuple[str, str]]) -> None:
    paths = sorted({p for p, _ in control})
    print(f"kb-import-pr: --allow-control-files — full diff of {len(paths)} control file(s):")
    for p, why in control:
        print(f"  {p}: {why}")
    text = _out(wt, "diff", "--no-color", "--no-renames", "--no-ext-diff", "--no-textconv",
                base, "HEAD", "--", *paths)
    for line in text.splitlines():
        print(f"    {_printable(line)}")


def _ask(prompt: str) -> bool:
    return input(prompt).strip().lower() in ("y", "yes")


def _report(pr: int, branch: str, wt: Path, base: str, commits: str, credit: str,
            control: bool) -> None:
    print(f"kb-import-pr: applied PR #{pr} ({commits} commit(s)) onto {branch}")
    print(f"  worktree: {wt}")
    print(f"  base:     origin/main {base[:7]}")
    print(f"  {credit}")
    print("Next steps — nothing from this patch has run:")
    print(f"  1. Review it: git -C {wt} log -p --stat {base[:12]}..HEAD"
          + ("  (control files especially)" if control else ""))
    print("  2. Only after that review, run the tests: in the PR's public CI run, or in a"
          f" sandbox (VM/container) with no access to your data; never in {wt} on a live"
          " machine.")
    print(f"  3. Merge {branch} into main and push origin; new exported paths need "
          f"`kb-publish approve`.")
    print(f"  4. Publish as usual, then close PR #{pr} with a link to the sync commit.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="kb-import-pr",
        description="Import a public-mirror PR onto a new import/pr-N branch in a new "
                    "worktree, after path, control-file and deny checks.",
        epilog="The whole PR flow is described in docs/publishing.md.")
    ap.add_argument("pr_number", type=int)
    ap.add_argument("--allow-control-files", action="store_true",
                    help="permit control-file changes: prints their full diffs and still "
                         "needs --yes (or a terminal confirmation)")
    ap.add_argument("--yes", action="store_true",
                    help="confirm --allow-control-files without prompting")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    return run(_toplevel(), args.pr_number, allow_control_files=args.allow_control_files,
               yes=args.yes)


if __name__ == "__main__":
    sys.exit(main())
