"""Personal skill registry: portable Agent Skills packages with immutable revisions.

Usage: docs/setup/recipes/skill-registry.md.

Pure validation lives at the top (no database, testable without Docker); the SQL
functions below it take an open psycopg connection, and `KnowledgeBase` wraps each in a
pooled connection/transaction. Skills are NEVER `nodes` rows: nothing in the ingest,
enrichment, concept, decay or mirror machinery may treat an instruction package as a
memory, and dedicated tables make that true by construction rather than by carve-out.

Error categories are stable: every SkillError's str() starts with its `code`
("not_found: ..."), which is exactly what reaches an agent inside FastMCP's ToolError
text, so an agent can branch on the prefix without parsing prose.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass

import frontmatter  # python-frontmatter: already a kb-core dependency (Obsidian parser)
import psycopg

from kb_core.retrieval_heat import validate_tz

SKILL_FILE = "SKILL.md"
AGENT_FILE = "AGENT.md"
KINDS = ("skill", "agent")
# The one instructions file each kind's discovery metadata is derived from.
MAIN_FILE = {"skill": SKILL_FILE, "agent": AGENT_FILE}
MAX_NOTES_LEN = 2000
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# Agent Skills spec: lowercase letters, digits, single hyphens; must equal the
# frontmatter `name` and would be the directory name of a local install.
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME_LEN = 64
MAX_DESCRIPTION_LEN = 1024
MAX_PATH_LEN = 256
MAX_FILES = 64
MAX_FILE_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024
MAX_SUMMARY_LEN = 2000
HISTORY_LIMIT = 20
PAGE_SIZE = 50


class SkillError(Exception):
    """Base: `code` is the stable category an agent can branch on."""
    code = "skill_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class SkillNotFound(SkillError):
    code = "not_found"


class RevisionNotFound(SkillError):
    code = "revision_not_found"


class FileNotFound(SkillError):
    code = "file_not_found"


class InvalidPackage(SkillError):
    code = "invalid_package"


class PackageTooLarge(SkillError):
    code = "size_limit"


class InvalidArgument(SkillError):
    code = "invalid_argument"


class RevisionConflict(SkillError):
    """The caller's expected revision is not current. Carries the current one so the
    caller can reread and reconcile (spec: never a silent overwrite)."""
    code = "revision_conflict"

    def __init__(self, message: str, current_revision: int) -> None:
        super().__init__(f"{message} (current_revision={current_revision})")
        self.current_revision = current_revision


class NotApproved(SkillError):
    """No approved revision to serve by default; the message names the one to review."""
    code = "not_approved"


class ReviewHashMismatch(SkillError):
    """The reviewer's hash is not this revision's: it read something else, or nothing."""
    code = "review_hash_mismatch"


class NotPending(SkillError):
    code = "not_pending"


@dataclass(frozen=True)
class Package:
    name: str
    description: str
    files: dict[str, str]          # path -> text, sorted by path
    file_hashes: dict[str, str]    # path -> sha256 hex of the UTF-8 text
    package_hash: str              # sha256 over (path, file hash) pairs in path order


def validate_kind(kind) -> str:
    if kind not in KINDS:
        raise InvalidArgument(f"kind must be one of {', '.join(KINDS)} (got {kind!r})")
    return kind


def validate_name(name, kind: str = "skill") -> str:
    if (not isinstance(name, str) or len(name) > MAX_NAME_LEN
            or not NAME_RE.fullmatch(name)):  # fullmatch: `$` would admit a trailing newline
        raise InvalidPackage(
            f"{kind} name {name!r} must be 1-{MAX_NAME_LEN} characters of lowercase "
            "letters, digits and single hyphens (e.g. 'review-infrastructure')")
    return name


def normalize_path(path) -> str:
    """A package-relative path exactly as it will be stored: forward slashes, no '.',
    '..' or empty segment, no backslash, NUL or leading slash. A path that is not
    already in this form is rejected rather than rewritten, so two spellings of one
    file can never land in one revision and nothing is ever resolved against the
    server's filesystem."""
    if not isinstance(path, str) or not path:
        raise InvalidPackage("a file path is empty")
    if len(path) > MAX_PATH_LEN:
        raise InvalidPackage(f"path {path[:40]!r}... is longer than {MAX_PATH_LEN} characters")
    if "\\" in path or "\x00" in path:
        raise InvalidPackage(f"path {path!r} contains a backslash or NUL")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidPackage(f"path {path!r} is not valid UTF-8") from exc
    if path.startswith("/"):
        raise InvalidPackage(f"path {path!r} is absolute; paths are package-relative")
    if any(seg in ("", ".", "..") for seg in path.split("/")):
        raise InvalidPackage(f"path {path!r} must not contain '.', '..' or empty segments")
    return path


def parse_skill_md(name: str, text: str, filename: str = SKILL_FILE) -> str:
    """The description from the main file's frontmatter (SKILL.md, or AGENT.md for an
    agent). Discovery metadata is DERIVED from the instructions so it cannot drift."""
    try:
        post = frontmatter.loads(text)
    except Exception as exc:  # yaml.YAMLError and friends
        raise InvalidPackage(f"{filename} frontmatter is not valid YAML: {exc}") from exc
    meta = dict(post.metadata)
    if not meta:
        raise InvalidPackage(f"{filename} must start with YAML frontmatter (name, description)")
    fm_name = meta.get("name")
    if fm_name != name:
        raise InvalidPackage(
            f"{filename} frontmatter name {fm_name!r} must equal the name {name!r}")
    desc = meta.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise InvalidPackage(f"{filename} frontmatter needs a non-empty description")
    desc = " ".join(desc.split())
    if len(desc) > MAX_DESCRIPTION_LEN:
        raise InvalidPackage(f"description is longer than {MAX_DESCRIPTION_LEN} characters")
    return desc


def validate_package(name, files, kind: str = "skill") -> Package:
    """Validate one complete package or raise; nothing is persisted here. An agent is a
    one-file package: Claude Code reads a subagent from a single <name>.md."""
    validate_kind(kind)
    validate_name(name, kind)
    if not isinstance(files, dict) or not files:
        raise InvalidPackage("files must be a non-empty object of {path: text}")
    if len(files) > MAX_FILES:
        raise PackageTooLarge(f"{len(files)} files; the limit is {MAX_FILES}")
    clean: dict[str, str] = {}
    total = 0
    for raw_path, content in files.items():
        path = normalize_path(raw_path)
        if path in clean:
            raise InvalidPackage(f"duplicate path {path!r}")
        if not isinstance(content, str):
            raise InvalidPackage(f"{path}: content must be text")
        if "\x00" in content:
            raise InvalidPackage(f"{path}: NUL bytes are not allowed")
        try:
            size = len(content.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise InvalidPackage(f"{path}: content is not valid UTF-8") from exc
        if size > MAX_FILE_BYTES:
            raise PackageTooLarge(f"{path}: {size} bytes; the per-file limit is {MAX_FILE_BYTES}")
        total += size
        clean[path] = content
    if total > MAX_PACKAGE_BYTES:
        raise PackageTooLarge(f"package is {total} bytes; the limit is {MAX_PACKAGE_BYTES}")
    main = MAIN_FILE[kind]
    if kind == "agent" and set(clean) != {AGENT_FILE}:
        raise InvalidPackage(f"an agent package is exactly one file, {AGENT_FILE} "
                             f"(got {', '.join(sorted(clean))})")
    if main not in clean:
        raise InvalidPackage(f"package must contain {main}")
    description = parse_skill_md(name, clean[main], main)
    ordered = dict(sorted(clean.items()))
    hashes = {p: hashlib.sha256(c.encode("utf-8")).hexdigest() for p, c in ordered.items()}
    h = hashlib.sha256()
    for p, fh in hashes.items():
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
        h.update(fh.encode("ascii"))
        h.update(b"\n")
    return Package(name=name, description=description, files=ordered,
                   file_hashes=hashes, package_hash=h.hexdigest())


def encode_cursor(offset: int) -> str:
    """Opaque paging token: the offset, base64 so callers never build one by hand."""
    return base64.urlsafe_b64encode(f"o:{offset}".encode("ascii")).decode("ascii")


def decode_cursor(cursor) -> int:
    if cursor is None or cursor == "":
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
        kind, n = raw.split(":", 1)
        if kind != "o" or int(n) < 0:
            raise ValueError(raw)
        return int(n)
    except (ValueError, binascii.Error, UnicodeDecodeError, UnicodeEncodeError,
            AttributeError) as exc:
        raise InvalidArgument("cursor is not one this server issued") from exc


def _is_utf8(text: str) -> bool:
    """False for a str holding a lone surrogate ("\\ud800", which JSON over MCP can carry):
    the driver would raise a raw UnicodeEncodeError instead of a stable category."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# --------------------------------------------------------------------------- SQL
# Every function takes an OPEN psycopg connection (dict rows). Writers run inside the
# caller's transaction; KnowledgeBase supplies it.

def _lock(conn, kind: str, name: str) -> None:
    """Serialise every writer and reviewer of one item -- create included -- for the
    rest of the transaction. Behind this lock a concurrent create sees the winner's row,
    and a review can never interleave with a write to the same item."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"library:{kind}:{name}",))


def _item_row(conn, kind: str, name: str):
    return conn.execute(
        "SELECT name, current_revision, approved_revision, archived_at, archived_by, "
        "created_at, updated_at FROM skills WHERE kind = %s AND name = %s",
        (kind, name)).fetchone()


def _pending_revision(conn, kind: str, name: str):
    return conn.execute(
        "SELECT max(revision) AS r FROM skill_revisions "
        "WHERE kind = %s AND name = %s AND review_status = 'pending'",
        (kind, name)).fetchone()["r"]


def _check_revision_arg(value, *, allow_null: bool, what: str) -> None:
    if value is None and allow_null:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidArgument(what)


def _check_label(value, what: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value) > 128:
        raise InvalidArgument(f"{what} must be a string of at most 128 characters")
    if "\x00" in value:
        raise InvalidArgument(f"{what} must not contain NUL bytes")
    if not _is_utf8(value):
        raise InvalidArgument(f"{what} is not valid UTF-8")


def _next_step(kind: str, name: str, revision: int, package_hash: str,
               archived: bool = False) -> str:
    files = (f" and every file with get_skill_file(name={name!r}, path, revision={revision})"
             if kind == "skill" else "")
    restore = (f" The {kind} is archived and stays archived until then: approving it "
               "restores the item." if archived else "")
    return (f"Revision {revision} is pending review and is not served yet.{restore} Read it "
            f"in full with get_{kind}(name={name!r}, revision={revision}){files}, then call "
            f"review_{kind}(name={name!r}, revision={revision}, "
            f"package_hash={package_hash!r}, approve=..., notes=...).")


def upsert(conn, name, expected_revision, files, change_summary, *, kind: str = "skill",
           actor: str | None = None, agent_label: str | None = None) -> dict:
    """Commit one complete package snapshot as a new, PENDING revision.

    expected_revision None -> create the item, or make this its newest revision
                              unconditionally (last write wins; history keeps
                              every revision, so nothing is lost).
    expected_revision N    -> today's compare-and-swap: only if N is current.
    Validation happens before any row is touched, and the caller's transaction makes the
    inserts atomic, so an invalid package leaves history and pointers untouched. A write
    to an archived item leaves it archived -- nothing of it is served -- and approving
    the new revision reactivates it (review), so a fix to a retired item never re-serves
    the old approved text while the fix waits."""
    pkg = validate_package(name, files, kind)
    if not isinstance(change_summary, str) or not change_summary.strip():
        raise InvalidPackage("change_summary is required")
    # Postgres text cannot hold NUL, and JSON over MCP can carry \u0000: without these
    # checks the INSERT would surface a raw psycopg DataError instead of a stable category.
    if "\x00" in change_summary:
        raise InvalidPackage("change_summary must not contain NUL bytes")
    if not _is_utf8(change_summary):
        raise InvalidPackage("change_summary is not valid UTF-8")
    summary = change_summary.strip()
    if len(summary) > MAX_SUMMARY_LEN:
        raise PackageTooLarge(
            f"change_summary is {len(summary)} characters; the limit is {MAX_SUMMARY_LEN}")
    _check_revision_arg(expected_revision, allow_null=True,
                        what="expected_revision must be omitted (write unconditionally) or "
                             "the positive integer revision you read")
    _check_label(agent_label, "agent_label")
    _lock(conn, kind, name)
    row = conn.execute("SELECT current_revision, archived_at FROM skills "
                       "WHERE kind = %s AND name = %s FOR UPDATE", (kind, name)).fetchone()
    if row is None:
        if expected_revision is not None:
            raise SkillNotFound(f"{kind} {name!r} does not exist; omit expected_revision to "
                                "create it")
        revision, archived = 1, False
        conn.execute("INSERT INTO skills (kind, name, current_revision) VALUES (%s, %s, 1)",
                     (kind, name))
    else:
        if expected_revision is not None and row["current_revision"] != expected_revision:
            raise RevisionConflict(
                f"{kind} {name!r} is not at revision {expected_revision}; reread, or omit "
                "expected_revision to overwrite", row["current_revision"])
        revision = row["current_revision"] + 1
        archived = row["archived_at"] is not None
        conn.execute(
            "UPDATE skills SET current_revision = %s, updated_at = now() "
            "WHERE kind = %s AND name = %s", (revision, kind, name))
    conn.execute(
        "INSERT INTO skill_revisions (kind, name, revision, description, actor, agent_label, "
        "change_summary, package_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (kind, name, revision, pkg.description, actor, agent_label, summary, pkg.package_hash))
    conn.cursor().executemany(
        "INSERT INTO skill_revision_files (kind, name, revision, path, content, content_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        [(kind, name, revision, p, c, pkg.file_hashes[p]) for p, c in pkg.files.items()])
    return {"kind": kind, "name": name, "revision": revision,
            "package_hash": pkg.package_hash, "description": pkg.description,
            "archived": archived, "reactivated": False, "review_status": "pending",
            "next_step": _next_step(kind, name, revision, pkg.package_hash, archived)}


def archive(conn, name, expected_revision=None, *, kind: str = "skill",
            actor: str | None = None) -> dict:
    """Archive: the item leaves search and the library download; every revision stays
    readable. expected_revision is optional; a number still compares-and-swaps.
    Idempotent, so a retried request after a lost response does not conflict with itself.
    No new content, so no review."""
    validate_kind(kind)
    validate_name(name, kind)
    _check_revision_arg(expected_revision, allow_null=True,
                        what="expected_revision must be omitted or the positive integer "
                             "revision you read")
    _lock(conn, kind, name)
    row = conn.execute("SELECT current_revision, archived_at FROM skills "
                       "WHERE kind = %s AND name = %s FOR UPDATE", (kind, name)).fetchone()
    if row is None:
        raise SkillNotFound(f"{kind} {name!r} does not exist")
    if expected_revision is not None and row["current_revision"] != expected_revision:
        raise RevisionConflict(
            f"{kind} {name!r} is not at revision {expected_revision}; reread and reconcile",
            row["current_revision"])
    if row["archived_at"] is None:
        conn.execute("UPDATE skills SET archived_at = now(), archived_by = %s, "
                     "updated_at = now() WHERE kind = %s AND name = %s", (actor, kind, name))
    return {"name": name, "revision": row["current_revision"], "archived": True}


def review(conn, name, revision, package_hash, approve, notes, *, kind: str = "skill",
           actor: str | None = None, reviewer_label: str | None = None) -> dict:
    """Record one review of one PENDING revision, bound to its package hash.

    Approving serves it (approved_revision moves forward, never back) and supersedes
    every older pending revision of the item, which therefore can never be approved
    later, and reactivates an archived item (`reactivated`). Rejecting leaves the served
    revision -- and an archive -- alone. The hash proves which bytes the
    verdict covers; it cannot prove the reviewer read them -- the policy requires that."""
    validate_kind(kind)
    validate_name(name, kind)
    _check_revision_arg(revision, allow_null=False,
                        what="revision is required: the revision you read in full")
    if not isinstance(package_hash, str) or not _HASH_RE.fullmatch(package_hash):
        raise InvalidArgument("package_hash must be the 64-character hex hash get_"
                              f"{kind} returned for that revision")
    if not isinstance(approve, bool):
        raise InvalidArgument("approve must be true or false")
    if not isinstance(notes, str) or not notes.strip():
        raise InvalidArgument("notes are required: say what you checked")
    if "\x00" in notes or not _is_utf8(notes):
        raise InvalidArgument("notes must be text without NUL bytes or lone surrogates")
    notes = notes.strip()
    if len(notes) > MAX_NOTES_LEN:
        raise InvalidArgument(f"notes are {len(notes)} characters; the limit is {MAX_NOTES_LEN}")
    _check_label(reviewer_label, "agent_label")
    _lock(conn, kind, name)
    item = conn.execute("SELECT approved_revision, archived_at FROM skills "
                        "WHERE kind = %s AND name = %s FOR UPDATE", (kind, name)).fetchone()
    if item is None:
        raise SkillNotFound(f"{kind} {name!r} does not exist")
    r = conn.execute("SELECT review_status, package_hash FROM skill_revisions "
                     "WHERE kind = %s AND name = %s AND revision = %s FOR UPDATE",
                     (kind, name, revision)).fetchone()
    if r is None:
        raise RevisionNotFound(f"{kind} {name!r} has no revision {revision}")
    if r["review_status"] != "pending":
        raise NotPending(f"{kind} {name!r} revision {revision} is {r['review_status']}, "
                         "not pending; only a pending revision can be reviewed")
    if r["package_hash"] != package_hash:
        raise ReviewHashMismatch(
            f"package_hash does not match {kind} {name!r} revision {revision}; reread it with "
            f"get_{kind}(name, revision={revision}) and review what you read")
    status = "approved" if approve else "rejected"
    conn.execute(
        "UPDATE skill_revisions SET review_status = %s, reviewed_at = now(), reviewer = %s, "
        "reviewer_label = %s, review_notes = %s, reviewed_hash = %s "
        "WHERE kind = %s AND name = %s AND revision = %s",
        (status, actor, reviewer_label, notes, package_hash, kind, name, revision))
    approved = item["approved_revision"]
    reactivated = approve and item["archived_at"] is not None
    if approve:
        conn.execute("UPDATE skill_revisions SET review_status = 'superseded' "
                     "WHERE kind = %s AND name = %s AND revision < %s "
                     "AND review_status = 'pending'", (kind, name, revision))
        if approved is None or revision > approved:     # defensive: approved only moves forward
            approved = revision
    # Always touch the item row: the 0052 trigger then refreshes open dashboard pages.
    conn.execute("UPDATE skills SET approved_revision = %s, updated_at = now(), "
                 "archived_at = CASE WHEN %s THEN NULL ELSE archived_at END, "
                 "archived_by = CASE WHEN %s THEN NULL ELSE archived_by END "
                 "WHERE kind = %s AND name = %s",
                 (approved, reactivated, reactivated, kind, name))
    return {"kind": kind, "name": name, "revision": revision, "review_status": status,
            "approved_revision": approved, "reactivated": reactivated}


def get(conn, name, revision=None, *, kind: str = "skill", default: str = "approved",
        history_limit: int = HISTORY_LIMIT) -> dict:
    """One revision with its main file inline (skill_md / agent_md), a manifest of every
    file at that exact revision, its review state, and the newest `history_limit`
    revisions. An explicit `revision` reads any version (a reviewer reads a pending one
    this way). Without one, default="approved" serves the approved revision -- what
    agents get -- and "latest" the newest (the dashboard). Archived items are readable."""
    validate_kind(kind)
    validate_name(name, kind)
    _check_revision_arg(revision, allow_null=True, what="revision must be a positive integer")
    if default not in ("approved", "latest"):
        raise InvalidArgument("default must be 'approved' or 'latest'")
    item = _item_row(conn, kind, name)
    if item is None:
        raise SkillNotFound(f"{kind} {name!r} does not exist")
    pending = _pending_revision(conn, kind, name)
    if revision is not None:
        rev = revision
    elif default == "latest":
        rev = item["current_revision"]
    else:
        rev = item["approved_revision"]
        if rev is None:
            why = (f"revision {pending} is pending review" if pending is not None
                   else f"its newest revision {item['current_revision']} was rejected")
            raise NotApproved(f"{kind} {name!r} has no approved revision yet: {why}. "
                              f"Read a specific one with get_{kind}(name, revision=N)")
    r = conn.execute(
        "SELECT revision, description, actor, agent_label, change_summary, package_hash, "
        "created_at, review_status, reviewed_at, reviewer, reviewer_label, review_notes "
        "FROM skill_revisions WHERE kind = %s AND name = %s AND revision = %s",
        (kind, name, rev)).fetchone()
    if r is None:
        raise RevisionNotFound(f"{kind} {name!r} has no revision {rev}")
    # COLLATE "C": the database's en_US collation would sort "references/..." before
    # "SKILL.md"; the manifest must follow the code-point order validate_package sorts
    # by and package_hash is computed over.
    files = conn.execute(
        "SELECT path, content, content_hash FROM skill_revision_files "
        'WHERE kind = %s AND name = %s AND revision = %s ORDER BY path COLLATE "C"',
        (kind, name, rev)).fetchall()
    history = conn.execute(
        "SELECT revision, actor, agent_label, change_summary, package_hash, created_at, "
        "review_status, reviewed_at, reviewer_label, review_notes "
        "FROM skill_revisions WHERE kind = %s AND name = %s ORDER BY revision DESC LIMIT %s",
        (kind, name, history_limit)).fetchall()
    main = MAIN_FILE[kind]
    return {
        "kind": kind,
        "name": name,
        "description": r["description"],
        "revision": rev,
        "current_revision": item["current_revision"],
        "latest_revision": item["current_revision"],
        "approved_revision": item["approved_revision"],
        "pending_revision": pending,
        "review_status": r["review_status"],
        "reviewed_at": r["reviewed_at"],
        "reviewer": r["reviewer"],
        "reviewer_label": r["reviewer_label"],
        "review_notes": r["review_notes"],
        "archived": item["archived_at"] is not None,
        "archived_at": item["archived_at"],
        "archived_by": item["archived_by"],
        "package_hash": r["package_hash"],
        "change_summary": r["change_summary"],
        "actor": r["actor"],
        "agent_label": r["agent_label"],
        "created_at": r["created_at"],
        f"{kind}_md": next(f["content"] for f in files if f["path"] == main),
        "files": [{"path": f["path"], "bytes": len(f["content"].encode("utf-8")),
                   "content_hash": f["content_hash"]} for f in files],
        "history": [dict(h) for h in history],
    }


def get_file(conn, name, path, revision, *, kind: str = "skill") -> dict:
    """One file at an EXPLICIT revision -- the one get returned -- so instructions and
    references can never mix across an intervening update."""
    validate_kind(kind)
    validate_name(name, kind)
    _check_revision_arg(revision, allow_null=False,
                        what=f"revision is required: pass the revision get_{kind} returned")
    path = normalize_path(path)
    row = conn.execute(
        "SELECT content, content_hash FROM skill_revision_files "
        "WHERE kind = %s AND name = %s AND revision = %s AND path = %s",
        (kind, name, revision, path)).fetchone()
    if row is None:
        if _item_row(conn, kind, name) is None:
            raise SkillNotFound(f"{kind} {name!r} does not exist")
        if conn.execute("SELECT 1 FROM skill_revisions WHERE kind = %s AND name = %s "
                        "AND revision = %s", (kind, name, revision)).fetchone() is None:
            raise RevisionNotFound(f"{kind} {name!r} has no revision {revision}")
        raise FileNotFound(f"{kind} {name!r} revision {revision} has no file {path!r}")
    return {"name": name, "revision": revision, "path": path,
            "content": row["content"], "content_hash": row["content_hash"]}


def _like(q: str) -> str:
    """A LIKE pattern that matches the query text literally (default backslash escape)."""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search(conn, query=None, cursor=None, *, kind: str = "skill",
           include_archived: bool = False, include_unapproved: bool = False,
           page_size: int = PAGE_SIZE) -> dict:
    """Deterministic lexical discovery over name + the SERVED revision's description:
    English full-text rank, trigram similarity on the name, and a literal substring
    match, whichever is highest; ties break on name. No query = browse by name. No
    embedding or model call, so the inference map is untouched.

    The served revision is the approved one; an item with none is left out unless
    include_unapproved (the dashboard), which falls back to the newest. Each row says
    what waits: `pending_revision`, and the newest revision's `review_status`.

    websearch_to_tsquery never raises on operator-looking text, the LIKE pattern is
    escaped, and ts_rank of an empty query (all stopwords) is 0, which is why odd query
    text can only ever narrow the result, never error. The one exception is text Postgres
    cannot hold at all (NUL, a lone surrogate): that is an InvalidArgument, not a DB error."""
    validate_kind(kind)
    offset = decode_cursor(cursor)
    page_size = max(1, min(int(page_size), PAGE_SIZE))
    q = " ".join((query or "").split())
    if "\x00" in q or not _is_utf8(q):
        raise InvalidArgument("query must be text without NUL bytes or lone surrogates")
    params: dict = {"limit": page_size + 1, "offset": offset, "kind": kind}
    where = ["s.kind = %(kind)s"]
    if not include_archived:
        where.append("s.archived_at IS NULL")
    if q:
        params.update(q=q, like=_like(q))
        # The @@ gate is load-bearing: for a multi-term query ('unit tests' is
        # 'unit' & 'test') ts_rank returns a 1e-20 floor, not 0, on a document that
        # does not match, so without it "rank > 0" would admit every item.
        doc = "to_tsvector('english', s.name || ' ' || r.description)"
        tsq = "websearch_to_tsquery('english', %(q)s)"
        rank = ("GREATEST("
                f"CASE WHEN {doc} @@ {tsq} THEN ts_rank({doc}, {tsq}) ELSE 0 END, "
                "similarity(s.name, %(q)s), "
                "CASE WHEN s.name ILIKE %(like)s OR r.description ILIKE %(like)s "
                "     THEN 0.5 ELSE 0 END)")
        where.append(f"{rank} > 0")
        order = f"{rank} DESC, s.name COLLATE \"C\""
    else:
        order = 's.name COLLATE "C"'      # bytewise, like the manifest order (see get)
    served = ("COALESCE(s.approved_revision, s.current_revision)" if include_unapproved
              else "s.approved_revision")   # an inner join on NULL drops the item
    sql = ("SELECT s.name, r.description, r.revision, s.current_revision AS latest_revision, "
           "s.approved_revision, l.review_status, s.updated_at, "
           "(s.archived_at IS NOT NULL) AS archived, "
           "(SELECT max(p.revision) FROM skill_revisions p WHERE p.kind = s.kind "
           " AND p.name = s.name AND p.review_status = 'pending') AS pending_revision "
           "FROM skills s "
           f"JOIN skill_revisions r ON r.kind = s.kind AND r.name = s.name AND r.revision = {served} "
           "JOIN skill_revisions l ON l.kind = s.kind AND l.name = s.name "
           "  AND l.revision = s.current_revision "
           "WHERE " + " AND ".join(where)
           + f" ORDER BY {order} LIMIT %(limit)s OFFSET %(offset)s")
    rows = conn.execute(sql, params).fetchall()
    more = len(rows) > page_size
    return {f"{kind}s": [dict(r) for r in rows[:page_size]],
            "next_cursor": encode_cursor(offset + page_size) if more else None,
            "query": q or None}


def pending_reviews(conn, kind=None) -> dict:
    """Every revision waiting for review, oldest first (capped at 200)."""
    if kind is not None:
        validate_kind(kind)
    rows = conn.execute(
        "SELECT kind, name, revision, package_hash, actor, agent_label, change_summary, "
        "created_at FROM skill_revisions WHERE review_status = 'pending' "
        "AND (%(kind)s::text IS NULL OR kind = %(kind)s) "
        'ORDER BY created_at, kind DESC, name COLLATE "C", revision LIMIT 200',
        {"kind": kind}).fetchall()
    return {"pending": [dict(r) for r in rows]}


# ---- Usage over the MCP trace log (dashboard Skills page) ---------------------------
# Read-only aggregates over mcp_trace -- no model call, no table of its own. Real
# traffic only (caller IS NULL), the trace_summary / retrieval heatmap rule. One window
# for everything: `days` whole local days in `tz`, today included, so the per-day chart,
# the library counts and the per-skill counts always cover exactly the same instants.

USAGE_DAYS = (7, 30, 90)
USAGE_TOP = 20
RECENT_LOADS = 20

# A successful load / file read / search by real traffic since the window opened.
_REAL = "caller IS NULL AND error IS NULL AND at >= %(since)s"
# A search that found nothing: blank queries are browses, and a later page that ran
# off the end of a matching query's results (`cursor` set) is not a miss either.
_MISSED = ("result_count = 0 AND btrim(coalesce(query, '')) <> '' "
           "AND args->>'cursor' IS NULL")
# The revision a load served: trace.revision (recorded since the Skills page shipped),
# else the one the caller asked for; NULL = "current at the time", not recorded. The
# regex keeps one malformed value from failing the whole query on the ::int cast.
_REVISION = ("CASE WHEN trace->>'revision' ~ '^[0-9]{1,9}$' THEN (trace->>'revision')::int "
             "WHEN args->>'revision' ~ '^[0-9]{1,9}$' THEN (args->>'revision')::int END")


def _window(conn, days, tz) -> dict:
    if isinstance(days, bool) or days not in USAGE_DAYS:
        raise InvalidArgument(f"days must be one of {', '.join(map(str, USAGE_DAYS))}")
    try:
        validate_tz(tz)
        row = conn.execute(
            "SELECT w.start_local::date AS start_day, w.end_local::date AS end_day, "
            "       w.start_local AT TIME ZONE %(tz)s AS since "
            "FROM (SELECT date_trunc('day', now() AT TIME ZONE %(tz)s) "
            "             - make_interval(days => %(days)s::int - 1) AS start_local, "
            "             date_trunc('day', now() AT TIME ZONE %(tz)s) AS end_local) w",
            {"tz": tz, "days": days}).fetchone()
    except (ValueError, psycopg.errors.InvalidParameterValue) as exc:
        # The second case: a zone Python's tzdata knows but this server's does not.
        raise InvalidArgument(f"unknown timezone: {tz!r}") from exc
    return {"days": days, "tz": tz, **row}


def _surfaced_since(conn):
    """When search surfacing started being recorded (the first search_skills row that
    carries trace.skills); None before that, so the page can say so instead of 0."""
    row = conn.execute(
        "SELECT at FROM mcp_trace WHERE tool = 'search_skills' AND caller IS NULL "
        "AND trace ? 'skills' ORDER BY at LIMIT 1").fetchone()
    return row["at"] if row else None


def usage_for(conn, name, days, tz="UTC") -> dict:
    """One skill's usage in the window. A name with no registry row is not an error:
    its calls (e.g. requests for a skill that was never created) are still history."""
    validate_name(name)
    w = _window(conn, days, tz)
    p = {"name": name, "since": w["since"], "limit": RECENT_LOADS}
    counts = conn.execute(
        "SELECT count(*) FILTER (WHERE tool = 'get_skill') AS loads, "
        "       count(*) FILTER (WHERE tool = 'get_skill_file') AS file_reads, "
        "       max(at) FILTER (WHERE tool = 'get_skill') AS last_loaded_at "
        f"FROM mcp_trace WHERE {_REAL} "
        "AND tool IN ('get_skill', 'get_skill_file') AND args->>'name' = %(name)s",
        p).fetchone()
    surfaced = conn.execute(
        f"SELECT count(*) AS n FROM mcp_trace WHERE {_REAL} "
        "AND tool = 'search_skills' AND trace->'skills' ? %(name)s", p).fetchone()["n"]
    recent = conn.execute(
        f"SELECT at, {_REVISION} AS revision, session_id, turn_id FROM mcp_trace "
        f"WHERE {_REAL} AND tool = 'get_skill' AND args->>'name' = %(name)s "
        "ORDER BY at DESC, id DESC LIMIT %(limit)s", p).fetchall()
    return {"name": name, "days": w["days"], "tz": w["tz"], "since": w["since"],
            "loads": counts["loads"], "file_reads": counts["file_reads"],
            "surfaced": surfaced, "last_loaded_at": counts["last_loaded_at"],
            "surfaced_since": _surfaced_since(conn),
            "recent_loads": [dict(r) for r in recent]}


def usage(conn, days, tz="UTC") -> dict:
    """Library-wide usage in the window: searches per local day (zero-filled), the
    searches that found nothing, requests for skills that do not exist, loads per
    skill, and the active skills nobody loaded."""
    w = _window(conn, days, tz)
    p = {"since": w["since"], "tz": tz, "start": w["start_day"], "end": w["end_day"],
         "top": USAGE_TOP}
    per_day = conn.execute(
        "WITH d AS (SELECT %(start)s::date + i AS day "
        "           FROM generate_series(0, %(end)s::date - %(start)s::date) i), "
        "s AS (SELECT (at AT TIME ZONE %(tz)s)::date AS day, count(*) AS searches, "
        f"            count(*) FILTER (WHERE {_MISSED}) AS missed "
        f"     FROM mcp_trace WHERE {_REAL} AND tool = 'search_skills' GROUP BY 1) "
        "SELECT d.day, coalesce(s.searches, 0)::int AS searches, "
        "       coalesce(s.missed, 0)::int AS missed "
        "FROM d LEFT JOIN s USING (day) ORDER BY d.day", p).fetchall()
    missed = conn.execute(
        "SELECT regexp_replace(lower(btrim(query)), '\\s+', ' ', 'g') AS query, "
        "       count(*)::int AS count, max(at) AS last_at "
        f"FROM mcp_trace WHERE {_REAL} AND tool = 'search_skills' AND {_MISSED} "
        "GROUP BY 1 ORDER BY count DESC, last_at DESC, query LIMIT %(top)s", p).fetchall()
    # left(error, 10), not LIKE 'not_found:%': `_` is a LIKE wildcard. The prefix is
    # SkillNotFound's stable code; file_not_found / revision_not_found do not match.
    missing = conn.execute(
        "SELECT args->>'name' AS name, count(*)::int AS count, max(at) AS last_at "
        "FROM mcp_trace WHERE caller IS NULL AND at >= %(since)s "
        "AND tool IN ('get_skill', 'get_skill_file') AND left(error, 10) = 'not_found:' "
        "AND args->>'name' IS NOT NULL "
        "GROUP BY 1 ORDER BY count DESC, last_at DESC, name LIMIT %(top)s", p).fetchall()
    loads = conn.execute(
        "SELECT args->>'name' AS name, count(*)::int AS loads, max(at) AS last_loaded_at "
        f"FROM mcp_trace WHERE {_REAL} AND tool = 'get_skill' "
        "AND args->>'name' IS NOT NULL GROUP BY 1", p).fetchall()
    never = conn.execute(
        "SELECT s.name FROM skills s WHERE s.kind = 'skill' AND s.archived_at IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM mcp_trace t WHERE t.caller IS NULL AND t.error IS NULL "
        "  AND t.at >= %(since)s AND t.tool = 'get_skill' AND t.args->>'name' = s.name) "
        'ORDER BY s.name COLLATE "C"', p).fetchall()
    return {"days": w["days"], "tz": w["tz"], "since": w["since"],
            "searches_per_day": [dict(r) for r in per_day],
            "missed_searches": [dict(r) for r in missed],
            "missing_requests": [dict(r) for r in missing],
            "per_skill": {r["name"]: {"loads": r["loads"],
                                      "last_loaded_at": r["last_loaded_at"]} for r in loads},
            "never_loaded": [r["name"] for r in never],
            "surfaced_since": _surfaced_since(conn)}
