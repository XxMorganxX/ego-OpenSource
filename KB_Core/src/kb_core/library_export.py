"""The library download: one gzip tar per kind of every active item's APPROVED
revision.

Deterministic bytes (sorted entries, mtime 0, fixed modes, gzip mtime 0), so an unchanged
library is an unchanged archive. The ETag is computed from (name, revision, package_hash)
alone, so the dashboard answers 304 without building anything. kb_core.library_client
is the reader; build_archive is also its test fixture.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile

KINDS = ("skill", "agent")


def _key(item: dict) -> bytes:
    return item["name"].encode("utf-8")


def etag_for(kind: str, items: list[dict]) -> str:
    h = hashlib.sha256(f"kb-library/1\x00{kind}\n".encode("utf-8"))
    for it in sorted(items, key=_key):
        h.update(f"{it['name']}\x00{it['revision']}\x00{it['package_hash']}\n".encode("utf-8"))
    return h.hexdigest()


def _entry(kind: str, name: str, path: str) -> str:
    return f"skills/{name}/{path}" if kind == "skill" else f"agents/{name}.md"


def _add(tf: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = len(data), 0, 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tf.addfile(info, io.BytesIO(data))


def build_archive(kind: str, items: list[dict]) -> tuple[str, bytes]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    ordered = sorted(items, key=_key)
    manifest = {"format": 1, "kind": kind, "items": []}
    entries: list[tuple[str, bytes]] = []
    for it in ordered:
        files = sorted(it["files"].items(), key=lambda kv: kv[0].encode("utf-8"))
        manifest["items"].append({
            "name": it["name"], "revision": it["revision"],
            "package_hash": it["package_hash"], "description": it["description"],
            "files": [{"path": p, "sha256": hashlib.sha256(c.encode("utf-8")).hexdigest()}
                      for p, c in files]})
        entries += [(_entry(kind, it["name"], p), c.encode("utf-8")) for p, c in files]
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=9) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
            _add(tf, "manifest.json",
                 json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"))
            for name, data in entries:
                _add(tf, name, data)
    return etag_for(kind, ordered), raw.getvalue()


def _approved(conn, kind: str):
    return conn.execute(
        "SELECT s.name, r.revision, r.package_hash, r.description FROM skills s "
        "JOIN skill_revisions r ON r.kind = s.kind AND r.name = s.name "
        "  AND r.revision = s.approved_revision "
        "WHERE s.kind = %s AND s.archived_at IS NULL", (kind,)).fetchall()


def etag(conn, kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    return etag_for(kind, [dict(r) for r in _approved(conn, kind)])


def export(conn, kind: str) -> tuple[str, bytes]:
    """Run inside one REPEATABLE READ transaction (KnowledgeBase.export_library) so the
    item rows and their files come from one snapshot."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    rows = _approved(conn, kind)
    files = conn.execute(
        "SELECT f.name, f.path, f.content FROM skill_revision_files f "
        "JOIN skills s ON s.kind = f.kind AND s.name = f.name "
        "  AND s.approved_revision = f.revision "
        "WHERE f.kind = %s AND s.archived_at IS NULL", (kind,)).fetchall()
    by_name: dict[str, dict[str, str]] = {}
    for f in files:
        by_name.setdefault(f["name"], {})[f["path"]] = f["content"]
    return build_archive(kind, [{**dict(r), "files": by_name.get(r["name"], {})} for r in rows])
