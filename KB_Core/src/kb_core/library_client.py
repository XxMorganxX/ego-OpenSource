#!/usr/bin/env python3
"""kb-library: keep this device's Claude Code skills and agents in step with the KB.

STDLIB ONLY, Python 3.9+, and no kb_core import: the dashboard serves this exact file at
/api/library/client.py, so a fresh device bootstraps with curl and python3. It only
downloads -- nothing here can change the KB. New skills and agents are uploaded by an
agent session through kb-mcp, which is where every version is reviewed.

    kb-library login --url https://kb.example.com --client-id <id>.access
    kb-library sync            # also: sync --hook (session start: 5 s download budget,
                               #   DNS excluded; silent; always exit 0)
    kb-library status
    kb-library install-hook
    kb-library stage owner/repo --skill NAME [--agent NAME ...]
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import gzip
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME_LEN = 64
KINDS = {"skill": "skills", "agent": "agents"}
SKILL_FILE = "SKILL.md"
AGENT_FILE = "AGENT.md"
HOOK_BUDGET_S = 5.0
SYNC_BUDGET_S = 60.0
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 64
MAX_PACKAGE_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 20000
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
WORK_DIR = ".kb-library-work"


class LibraryError(Exception):
    """A refusal with a one-line reason. Whatever raised it has written nothing."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def home_dir(env) -> Path:
    return Path(env.get("KB_LIBRARY_HOME") or Path.home() / ".config" / "kb-library")


def claude_dir(env) -> Path:
    return Path(env.get("KB_LIBRARY_CLAUDE_DIR") or Path.home() / ".claude")


def cache_dir(env) -> Path:
    return Path(env.get("KB_LIBRARY_CACHE") or Path.home() / ".cache" / "kb-library")


def check_name(name) -> str:
    if not isinstance(name, str) or len(name) > MAX_NAME_LEN or not NAME_RE.fullmatch(name):
        raise LibraryError(f"bad item name {name!r}")
    return name


def check_rel(path) -> str:
    """A relative path that can only land inside the directory it is joined to."""
    if (not isinstance(path, str) or not path or len(path) > 512 or "\\" in path
            or "\x00" in path or path.startswith("/")
            or any(seg in ("", ".", "..") for seg in path.split("/"))):
        raise LibraryError(f"unsafe path {path!r}")
    return path


# ---- configuration -------------------------------------------------------------------

def load_config(env) -> dict:
    path = home_dir(env) / "config.json"
    cfg: dict = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise LibraryError(f"{path} is not valid JSON: {exc}") from None
    for key, var in (("url", "KB_LIBRARY_URL"), ("client_id", "KB_LIBRARY_CLIENT_ID"),
                     ("client_secret", "KB_LIBRARY_CLIENT_SECRET")):
        if env.get(var):
            cfg[key] = env[var]
    if not cfg.get("url"):
        raise LibraryError("no library URL configured: run `kb-library login --url "
                           "https://kb.example.com --client-id <id>.access`")
    cfg["url"] = str(cfg["url"]).rstrip("/")
    return cfg


def save_config(env, cfg: dict) -> Path:
    d = home_dir(env)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    path = d / "config.json"
    try:
        # NOFOLLOW: a planted symlink must not redirect the secret; fchmod before writing
        # because O_CREAT does not change the mode of an existing file.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise LibraryError(f"cannot write {path}: {exc}") from None
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)
        json.dump(cfg, fh, indent=2)
    return path


# ---- download ------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Access answers a missing or expired service token with a redirect to its login
    page. Following it would hand an HTML page to the archive parser; refusing turns it
    into an HTTPError the caller can explain."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
_TOKEN_HINT = "check the service token (KB_LIBRARY_CLIENT_ID / kb-library login)"


def _etag_value(raw):
    if not raw:
        return None
    return raw.strip().removeprefix("W/").strip('"') or None


def fetch_library(cfg: dict, kind: str, etag, timeout: float):
    """(status, etag, body); status 304 = unchanged since `etag`, body empty."""
    url = f"{cfg['url']}/api/library/{KINDS[kind]}"
    # identity: the dashboard gzips responses it thinks are compressible, and urllib
    # does not undo a Content-Encoding.
    headers = {"Accept-Encoding": "identity", "User-Agent": "kb-library/1"}
    if cfg.get("client_id"):
        headers["CF-Access-Client-Id"] = cfg["client_id"]
        headers["CF-Access-Client-Secret"] = cfg.get("client_secret") or ""
    if etag:
        headers["If-None-Match"] = f'"{etag}"'
    req = urllib.request.Request(url, headers=headers)
    deadline = time.monotonic() + timeout
    try:
        with _OPENER.open(req, timeout=max(0.1, timeout)) as resp:
            chunks, total = [], 0
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise LibraryError(f"{url}: gave up after {timeout:.0f} s")
                try:   # bound each recv by what is left of the budget (best effort)
                    resp.fp.raw._sock.settimeout(left)
                except AttributeError:
                    pass
                chunk = resp.read1(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise LibraryError(f"{url}: archive is larger than {MAX_ARCHIVE_BYTES} bytes")
                chunks.append(chunk)
            return resp.status, _etag_value(resp.headers.get("ETag")), b"".join(chunks)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, etag, b""
        if 300 <= exc.code < 400:
            hint = (" (a redirect: the Access service token is missing, wrong or expired; "
                    f"{_TOKEN_HINT})")
        elif exc.code in (401, 403):
            hint = f" (refused: {_TOKEN_HINT})"
        else:
            hint = ""
        raise LibraryError(f"{url}: HTTP {exc.code}{hint}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise LibraryError(f"{url}: {getattr(exc, 'reason', exc)}") from None


def parse_archive(kind: str, body: bytes) -> list:
    """Every item in a library archive, each file verified against the manifest, or a
    LibraryError. Writes nothing: the caller installs only what this returns, so one bad
    entry refuses the whole kind."""
    members: dict = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
            # Iterate, never getmembers(): that reads every header of the stream -- the
            # whole decompressed archive -- before the first cap below can fire.
            for m in tf:
                if not m.isfile():
                    raise LibraryError(f"archive entry {m.name!r} is not a regular file")
                check_rel(m.name)
                cap = MAX_MANIFEST_BYTES if m.name == "manifest.json" else MAX_FILE_BYTES
                total += m.size
                if m.size > cap or total > MAX_ARCHIVE_BYTES or len(members) >= MAX_MEMBERS:
                    raise LibraryError(f"archive entry {m.name!r} is too large "
                                       f"({m.size} bytes; limit {cap})")
                if m.name in members:
                    raise LibraryError(f"archive lists {m.name!r} twice")
                members[m.name] = tf.extractfile(m).read()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise LibraryError(f"not a library archive: {exc}") from None
    try:
        manifest = json.loads(members.pop("manifest.json").decode("utf-8"))
        if manifest["format"] != 1 or manifest["kind"] != kind:
            raise LibraryError(f"archive is format {manifest['format']!r} kind "
                               f"{manifest['kind']!r}; expected format 1 kind {kind!r}")
        items, listed, seen = [], set(), set()
        for it in manifest["items"]:
            name = check_name(it["name"])
            if name in seen:
                raise LibraryError(f"manifest lists {name!r} twice")
            seen.add(name)
            files, pkg_bytes = {}, 0
            if len(it["files"]) > MAX_FILES:
                raise LibraryError(f"{kind} {name!r} has more than {MAX_FILES} files")
            for f in it["files"]:
                path = check_rel(f["path"])
                if kind == "agent" and path != AGENT_FILE:
                    raise LibraryError(f"agent {name!r} has a file other than {AGENT_FILE}")
                entry = f"skills/{name}/{path}" if kind == "skill" else f"agents/{name}.md"
                data = members.get(entry)
                if data is None:
                    raise LibraryError(f"archive is missing {entry!r}")
                if hashlib.sha256(data).hexdigest() != f["sha256"]:
                    raise LibraryError(f"{entry!r} does not match its manifest hash")
                pkg_bytes += len(data)
                if pkg_bytes > MAX_PACKAGE_BYTES:
                    raise LibraryError(f"{kind} {name!r} is larger than {MAX_PACKAGE_BYTES} bytes")
                files[path] = data
                listed.add(entry)
            if (SKILL_FILE if kind == "skill" else AGENT_FILE) not in files:
                raise LibraryError(f"{kind} {name!r} has no main file")
            items.append({"name": name, "revision": int(it["revision"]),
                          "package_hash": str(it["package_hash"]), "files": files})
    except LibraryError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise LibraryError(f"malformed library manifest: {exc!r}") from None
    extra = sorted(set(members) - listed)
    if extra:
        raise LibraryError(f"archive has entries its manifest does not list: {extra[:3]}")
    return items


# ---- install -------------------------------------------------------------------------

def _remove(p: Path) -> None:
    """Remove a file, directory or symlink. A symlink is unlinked, never followed."""
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)


def _dest(root: Path, kind: str, name: str) -> Path:
    return root / KINDS[kind] / (name if kind == "skill" else f"{name}.md")


def install_items(kind: str, items: list, root: Path, managed: dict):
    """Write every item over its local copy -- no comparison: the KB is the source of
    truth -- then remove the managed items the KB no longer serves. A name this
    client does not manage is never touched unless the KB serves it (then it is
    adopted). Each item is built in ~/.claude/.kb-library-work and renamed into place,
    so Claude Code never sees half of one. Returns (managed, report lines)."""
    (root / KINDS[kind]).mkdir(parents=True, exist_ok=True)
    work = root / WORK_DIR
    work.mkdir(parents=True, exist_ok=True)
    new, report = {}, []
    for it in items:
        name = it["name"]
        dest = _dest(root, kind, name)
        existed = dest.is_symlink() or dest.exists()
        tmp, old = work / f"{kind}-{name}.tmp", work / f"{kind}-{name}.old"
        _remove(tmp)
        _remove(old)
        if kind == "skill":
            tmp.mkdir()
            for rel, data in it["files"].items():
                out = tmp.joinpath(*rel.split("/"))
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
        else:
            tmp.write_bytes(it["files"][AGENT_FILE])
        if existed:
            os.rename(dest, old)        # renames a symlink itself, never its target
        os.rename(tmp, dest)
        _remove(old)
        prev = managed.get(name)
        if prev is None:
            report.append(f"{'adopted' if existed else 'installed'} {kind} {name} "
                          f"(revision {it['revision']})")
        elif prev.get("package_hash") != it["package_hash"] or not existed:
            report.append(f"updated {kind} {name} (revision {it['revision']})")
        new[name] = {"revision": it["revision"], "package_hash": it["package_hash"]}
    for name in sorted(set(managed) - set(new)):
        _remove(_dest(root, kind, name))
        report.append(f"removed {kind} {name}")
    return new, report


# ---- state + sync --------------------------------------------------------------------

def _blank_state() -> dict:
    return {"version": 1, "url": None,
            "kinds": {k: {"etag": None, "managed": {}, "synced_at": None} for k in KINDS}}


def _load_state(home: Path) -> dict:
    path = home / "state.json"
    if not path.exists():
        return _blank_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        for kind in KINDS:
            ks = state["kinds"].setdefault(kind, _blank_state()["kinds"][kind])
            ks.setdefault("etag", None)
            ks.setdefault("managed", {})
            ks.setdefault("synced_at", None)
        for ks in state["kinds"].values():
            for name in list(ks["managed"]):
                try:
                    check_name(name)
                except LibraryError:
                    del ks["managed"][name]   # never let a tampered name steer _remove
        return state
    except (ValueError, KeyError, TypeError, AttributeError):
        # Unreadable: refetch everything. Items removed from the KB meanwhile cannot be
        # cleaned up (this device no longer knows it managed them); nothing else is lost.
        return _blank_state()


def _save_state(home: Path, state: dict) -> None:
    tmp = home / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, home / "state.json")


def sync(env, *, hook: bool = False) -> list:
    """Pull both libraries. Unchanged kinds cost one 304; a changed kind is verified in
    full before anything is written. State is saved after each kind, so a failure on
    agents keeps the skills progress."""
    cfg = load_config(env)
    root, home = claude_dir(env), home_dir(env)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    budget = HOOK_BUDGET_S if hook else SYNC_BUDGET_S
    deadline = time.monotonic() + budget
    with open(home / "sync.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return [] if hook else ["another kb-library sync is running; skipped"]
        _remove(root / WORK_DIR)                 # a crashed run's leftovers
        state = _load_state(home)
        if state.get("url") != cfg["url"]:
            # A different library: refetch everything, but keep what this device manages
            # so items the new library lacks are still removed.
            for ks in state["kinds"].values():
                ks["etag"] = None
            state["url"] = cfg["url"]
        report: list = []
        for kind in KINDS:
            ks = state["kinds"][kind]
            left = deadline - time.monotonic()
            if left <= 0:
                raise LibraryError(f"gave up after {budget:.0f} s")
            status, etag, body = fetch_library(cfg, kind, ks["etag"], left)
            if status != 304:
                items = parse_archive(kind, body)
                if time.monotonic() > deadline:
                    raise LibraryError(f"gave up after {budget:.0f} s")
                try:
                    ks["managed"], lines = install_items(kind, items, root, ks["managed"])
                except OSError as exc:
                    raise LibraryError(f"cannot install {kind} items: {exc}") from None
                ks["etag"] = etag
                report += lines
            ks["synced_at"] = _now()
            _save_state(home, state)
        _remove(root / WORK_DIR)
        return report


def status_lines(env) -> list:
    state = _load_state(home_dir(env))
    lines = [f"library: {state.get('url') or 'never synced'}"]
    for kind, plural in KINDS.items():
        ks = state["kinds"][kind]
        lines.append(f"{plural}: last synced {ks['synced_at'] or 'never'}, "
                     f"{len(ks['managed'])} managed")
        for name, m in sorted(ks["managed"].items()):
            lines.append(f"  {name} (revision {m.get('revision')})")
    return lines


def install_hook(env, script: Path) -> bool:
    """Add a SessionStart hook that runs `sync --hook`; False if one is already there.
    The interpreter is called directly (-I, no shebang): macOS privacy controls (TCC)
    can deny a shebang script launched from a hook or agent."""
    path = claude_dir(env) / "settings.json"
    # A symlinked settings file (dotfiles) is written THROUGH: os.replace on the link
    # itself would swap it for a regular file and silently fork the user's settings.
    target = path.resolve() if path.is_symlink() else path
    settings: dict = {}
    mode = None
    if target.exists():
        try:
            settings = json.loads(target.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise LibraryError(f"{path} is not valid JSON ({exc}); not touching it") from None
        mode = target.stat().st_mode & 0o7777
    layout = LibraryError(f"{path}: unexpected hooks layout; not touching it")
    if not isinstance(settings, dict):
        raise layout
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict) or not isinstance(hooks.setdefault("SessionStart", []), list):
        raise layout
    entries = hooks["SessionStart"]
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks", []), list):
            raise layout
        for h in entry.get("hooks", []):
            if not isinstance(h, dict):
                raise layout
            cmd = str(h.get("command", ""))
            if "sync --hook" in cmd and "library" in cmd:
                return False
    command = f"{shlex.quote(sys.executable)} -I {shlex.quote(str(script))} sync --hook"
    entries.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".kb-library-tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, target)
    return True


# ---- stage (the add path) ------------------------------------------------------------
# Staging prepares files; it never uploads and never installs into ~/.claude. An agent
# session reads every staged file in full, uploads with upsert_skill / upsert_agent and
# reviews each new revision; sync then installs it on every device.

GITHUB_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})$")
SUBAGENT_RE = re.compile(r"""subagent_type\s*[=:]\s*["']([a-z0-9]+(?:-[a-z0-9]+)*)["']""")


def _http_get(url: str, accept=None, timeout: float = 30.0) -> bytes:
    headers = {"User-Agent": "kb-library/1"}
    if accept:
        headers["Accept"] = accept
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                    timeout=timeout) as resp:
            body = resp.read(MAX_ARCHIVE_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise LibraryError(f"{url}: {getattr(exc, 'reason', exc)}") from None
    if len(body) > MAX_ARCHIVE_BYTES:
        raise LibraryError(f"{url}: reply is larger than the {MAX_ARCHIVE_BYTES}-byte limit")
    return body


def frontmatter_name(text: str):
    """The `name:` in a leading YAML frontmatter block, or None. Deliberately tiny (no
    YAML library on a bare device): it only locates items; the server validates fully."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        m = re.match(r"^name:\s*(.*?)\s*$", line)
        if m:
            return m.group(1).strip("'\"") or None
    return None


def _text(p: Path) -> str:
    """Leading text of a file, for locating items only (bounded; never raises)."""
    try:
        with open(p, "rb") as fh:
            return fh.read(MAX_FILE_BYTES + 1).decode("utf-8", "ignore")
    except OSError:
        return ""


def _checked_bytes(p: Path, rel: str) -> bytes:
    if p.is_symlink():
        raise LibraryError(f"{rel} is a symlink; a package holds regular files only")
    size = p.stat().st_size            # checked before reading: never slurp a huge file
    if size > MAX_FILE_BYTES:
        raise LibraryError(f"{rel} is {size} bytes; the per-file limit is {MAX_FILE_BYTES}")
    data = p.read_bytes()
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise LibraryError(f"{rel} is not UTF-8 text; the library holds text files only") from None
    if len(data) > MAX_FILE_BYTES:
        raise LibraryError(f"{rel} is {len(data)} bytes; the per-file limit is {MAX_FILE_BYTES}")
    return data


def _read_skill_dir(d: Path) -> dict:
    """A skill folder's files. Hidden names (.skillfish.json and other tool metadata)
    are not part of a skill; a symlink is refused rather than followed."""
    files: dict = {}
    for dirpath, dirnames, filenames in os.walk(d, followlinks=False):
        here = Path(dirpath)
        for n in dirnames:
            if not n.startswith(".") and (here / n).is_symlink():
                raise LibraryError(f"{(here / n).relative_to(d)} is a symlink; a package "
                                   "holds regular files only")
        dirnames[:] = sorted(n for n in dirnames if not n.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            p = here / fn
            rel = check_rel(p.relative_to(d).as_posix())
            if p.is_symlink():
                raise LibraryError(f"{rel} is a symlink; a package holds regular files only")
            files[rel] = _checked_bytes(p, rel)
            if len(files) > MAX_FILES:
                raise LibraryError(f"{d} has more than {MAX_FILES} files")
    if SKILL_FILE not in files:
        raise LibraryError(f"{d} has no {SKILL_FILE}")
    if len(files) > MAX_FILES:
        raise LibraryError(f"{d} has {len(files)} files; the limit is {MAX_FILES}")
    total = sum(len(b) for b in files.values())
    if total > MAX_PACKAGE_BYTES:
        raise LibraryError(f"{d} is {total} bytes; the package limit is {MAX_PACKAGE_BYTES}")
    return files


def _find(root: Path, name: str, kind: str, path, oversize=frozenset()) -> Path:
    if path:
        p = root.joinpath(*check_rel(path).split("/"))
        if kind == "skill" and p.name == SKILL_FILE:
            p = p.parent
        main = p / SKILL_FILE if kind == "skill" else p
        if main.relative_to(root).as_posix() in oversize:
            raise LibraryError(f"{main.relative_to(root).as_posix()} is too large; the "
                               f"per-file limit is {MAX_FILE_BYTES} bytes")
        if p.is_symlink() or main.is_symlink():
            raise LibraryError(f"{path} is a symlink; a package holds regular files only")
        if not main.is_file() or frontmatter_name(_text(main)) != name:
            raise LibraryError(f"{path} is not a {kind} named {name!r}")
        return p
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [n for n in dirnames if not n.startswith(".")]
        for fn in filenames:
            if kind == "skill" and fn != SKILL_FILE:
                continue
            if kind == "agent" and (not fn.endswith(".md") or fn == SKILL_FILE):
                continue
            p = Path(dirpath) / fn
            if not p.is_symlink() and frontmatter_name(_text(p)) == name:
                found.append(p.parent if kind == "skill" else p)
    if not found:
        big = sorted(o for o in oversize if o.endswith(".md")
                     and (o.rsplit("/", 1)[-1] == SKILL_FILE) == (kind == "skill"))
        hint = (f" (skipped as too large for the {MAX_FILE_BYTES}-byte per-file limit: "
                + ", ".join(big[:5]) + ")") if big else ""
        raise LibraryError(f"no {kind} named {name!r} in the source{hint}")
    if len(found) > 1:
        rels = ", ".join(sorted(f.relative_to(root).as_posix() for f in found))
        raise LibraryError(f"{len(found)} {kind}s are named {name!r} ({rels}): "
                           "pick one with --path")
    return found[0]


class _Capped:
    """File-like over the decompressed tarball that refuses to yield more than `limit`
    bytes, so a small gzip bomb cannot exhaust memory or disk while tarfile skips over it."""

    def __init__(self, raw, limit: int):
        self._raw, self._left = raw, limit

    def read(self, n: int = -1) -> bytes:
        n = self._left + 1 if n is None or n < 0 else min(n, self._left + 1)
        data = self._raw.read(n)
        self._left -= len(data)
        if self._left < 0:
            raise LibraryError("repository tarball unpacks to more than "
                               f"{MAX_UNPACKED_BYTES} bytes")
        return data


def _extract_repo(body: bytes, dest: Path):
    """GitHub's tarball, treated as hostile: no extractall, no symlinks or special
    entries (skipped, never followed or created), every name through check_rel, declared
    sizes capped before any read. A regular file over MAX_FILE_BYTES is not extracted;
    its path is returned so an item that contains one is refused, not silently
    truncated. Returns (the one top-level directory, oversize paths relative to it)."""
    oversize: set = set()
    count = total = 0
    try:
        gz = gzip.GzipFile(fileobj=io.BytesIO(body))
        with tarfile.open(fileobj=_Capped(gz, MAX_UNPACKED_BYTES), mode="r|") as tf:
            for m in tf:
                if not m.isfile():
                    continue
                count += 1
                if count > MAX_MEMBERS:
                    raise LibraryError(f"repository tarball has more than {MAX_MEMBERS} files")
                rel = check_rel(m.name)
                if m.size > MAX_FILE_BYTES:
                    oversize.add(rel.split("/", 1)[1] if "/" in rel else rel)
                    continue
                total += m.size
                if total > MAX_ARCHIVE_BYTES:
                    raise LibraryError("repository tarball holds more than "
                                       f"{MAX_ARCHIVE_BYTES} bytes of files")
                out = dest.joinpath(*rel.split("/"))
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(tf.extractfile(m).read())
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise LibraryError(f"not a repository tarball: {exc}") from None
    tops = list(dest.iterdir())
    if len(tops) != 1 or not tops[0].is_dir():
        raise LibraryError("unexpected repository tarball layout")
    return tops[0], oversize


def _refuse_oversize(item: Path, root: Path, oversize: set) -> None:
    rel = item.relative_to(root).as_posix()
    for big in sorted(oversize):
        if big == rel or big.startswith(rel + "/"):
            raise LibraryError(f"{big} is over the per-file limit ({MAX_FILE_BYTES} bytes)")


def stage(env, source: str, *, skills=(), agents=(), path=None, ref=None,
          http_get=_http_get) -> Path:
    """Stage skills and agents from a GitHub repository (owner/repo, pinned to one
    commit) or a local skill folder / agent .md file."""
    skills, agents = tuple(skills), tuple(agents)
    if path and len(skills) + len(agents) != 1:
        raise LibraryError("--path needs exactly one --skill or --agent")
    for n in (*skills, *agents):
        check_name(n)
    local = Path(source).expanduser()
    commit, scratch = None, None
    staged_skills: dict = {}
    staged_agents: dict = {}
    try:
        if local.exists() and not skills and not agents:
            if local.is_dir() and (local / SKILL_FILE).is_file():
                files = _read_skill_dir(local)
                name = check_name(frontmatter_name(files[SKILL_FILE].decode("utf-8")) or "")
                staged_skills[name] = files
            elif local.is_file() and local.suffix == ".md":
                data = _checked_bytes(local, local.name)
                staged_agents[check_name(frontmatter_name(data.decode("utf-8")) or "")] = data
            else:
                raise LibraryError(f"{local} is neither a skill folder (with {SKILL_FILE}) "
                                   "nor an agent .md file")
        else:
            oversize: set = set()
            if local.exists():
                root = local
            else:
                m = GITHUB_RE.fullmatch(source)
                if not m:
                    raise LibraryError(f"{source!r} is neither a local path nor owner/repo")
                if not skills and not agents:
                    raise LibraryError("name at least one --skill or --agent to stage "
                                       "from a repository")
                owner, repo = m.groups()
                api = f"https://api.github.com/repos/{owner}/{repo}"
                if ref is None:
                    try:
                        ref = json.loads(http_get(api))["default_branch"]
                    except (ValueError, KeyError, TypeError) as exc:
                        raise LibraryError(f"{api}: unexpected reply ({exc!r})") from None
                commit = http_get(f"{api}/commits/{urllib.parse.quote(ref, safe='')}",
                                  accept="application/vnd.github.sha").decode("ascii", "replace").strip()
                if not re.fullmatch(r"[0-9a-f]{40}", commit):
                    raise LibraryError(f"cannot resolve {ref!r} in {source}")
                scratch = Path(tempfile.mkdtemp(prefix="kb-library-src-"))   # 0700
                root, oversize = _extract_repo(
                    http_get(f"https://codeload.github.com/{owner}/{repo}/tar.gz/{commit}"),
                    scratch)
            for name in skills:
                p = _find(root, name, "skill", path, oversize)
                _refuse_oversize(p, root, oversize)
                staged_skills[name] = _read_skill_dir(p)
            for name in agents:
                p = _find(root, name, "agent", path, oversize)
                _refuse_oversize(p, root, oversize)
                staged_agents[name] = _checked_bytes(p, p.name)
        parent = cache_dir(env) / "stage"
        parent.mkdir(parents=True, exist_ok=True)
        # mkdtemp: every staging gets its own empty directory, so two stagings can never
        # merge (the session uploads EVERY file it finds in one).
        out = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%dT%H%M%S-"), dir=parent))
        for name, files in staged_skills.items():
            for rel, data in files.items():
                dest = out / "skills" / name / Path(*rel.split("/"))
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
        for name, data in staged_agents.items():
            (out / "agents").mkdir(parents=True, exist_ok=True)
            (out / "agents" / f"{name}.md").write_bytes(data)
        referenced: set = set()
        for files in staged_skills.values():
            referenced |= set(SUBAGENT_RE.findall(files[SKILL_FILE].decode("utf-8")))
        meta = {"source": source, "commit": commit, "staged_at": _now(),
                "skills": sorted(staged_skills), "agents": sorted(staged_agents),
                "referenced_agents_not_staged": sorted(referenced - set(staged_agents))}
        out.mkdir(parents=True, exist_ok=True)
        (out / "stage.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        return out
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def print_stage(out: Path) -> None:
    meta = json.loads((out / "stage.json").read_text(encoding="utf-8"))
    print(f"staged in {out}")
    for kind in ("skills", "agents"):
        for name in meta[kind]:
            print(f"  {kind[:-1]} {name}")
    if meta["referenced_agents_not_staged"]:
        print("  the staged skills reference agents that are not staged: "
              + ", ".join(meta["referenced_agents_not_staged"]))
    print("Next, in the agent session: read EVERY staged file in full; upload each skill "
          "with upsert_skill (all of its files) and each agent with upsert_agent; then "
          "review every new revision with review_skill / review_agent.")

# ---- command line --------------------------------------------------------------------

def main(argv=None, env=None) -> int:
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(prog="kb-library",
                                 description="Sync Claude Code skills and agents from the KB.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login", help="save the library URL and this device's service token")
    p.add_argument("--url", required=True)
    p.add_argument("--client-id", required=True)
    p = sub.add_parser("sync", help="download both libraries into ~/.claude")
    p.add_argument("--hook", action="store_true",
                   help="session-start mode: 5 s download budget (DNS lookup not "
                        "included), silent when unchanged, always exit 0")
    sub.add_parser("status", help="show the library URL and the managed items")
    sub.add_parser("install-hook", help="run `sync --hook` at every Claude Code session start")
    p = sub.add_parser("stage", help="prepare a skill or agent for an agent session to upload")
    p.add_argument("source", help="owner/repo on GitHub, or a local folder or .md file")
    p.add_argument("--skill", action="append", default=[], metavar="NAME")
    p.add_argument("--agent", action="append", default=[], metavar="NAME")
    p.add_argument("--path", help="the item's path in the source, when its name is ambiguous")
    p.add_argument("--ref", help="git ref to stage from (default: the default branch)")
    args = ap.parse_args(argv)
    hook = getattr(args, "hook", False)
    try:
        if args.cmd == "login":
            u = urllib.parse.urlsplit(args.url)
            if u.scheme != "https" and not (u.scheme == "http" and u.hostname in
                                            ("127.0.0.1", "::1", "localhost")):
                raise LibraryError("the library URL must be https (http only for loopback)")
            secret = env.get("KB_LIBRARY_CLIENT_SECRET") or getpass.getpass("Access client secret: ")
            path = save_config(env, {"url": args.url.rstrip("/"), "client_id": args.client_id,
                                     "client_secret": secret})
            print(f"saved {path}")
        elif args.cmd == "sync":
            lines = sync(env, hook=hook)
            if hook:
                if lines:
                    print("kb-library: " + "; ".join(lines))
            else:
                if not lines:
                    print("up to date")
                elif lines[0].startswith("another kb-library sync"):
                    print(lines[0])
                else:
                    print("\n".join(lines + [f"kb-library: {len(lines)} changed"]))
        elif args.cmd == "status":
            print("\n".join(status_lines(env)))
        elif args.cmd == "install-hook":
            settings = claude_dir(env) / "settings.json"
            if install_hook(env, Path(__file__).resolve()):
                print(f"added a SessionStart hook to {settings}")
            else:
                print(f"{settings} already runs kb-library at session start")
        elif args.cmd == "stage":
            print_stage(stage(env, args.source, skills=tuple(args.skill),
                              agents=tuple(args.agent), path=args.path, ref=args.ref))
    except LibraryError as exc:
        print(f"kb-library: {exc}", file=sys.stderr)
        return 0 if hook else 1
    except Exception as exc:  # noqa: BLE001 -- a session must never fail on this hook
        if hook:
            print(f"kb-library: unexpected error: {exc!r}", file=sys.stderr)
            return 0
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
