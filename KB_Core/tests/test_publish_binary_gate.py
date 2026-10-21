"""Binary content: containers by magic bytes, and no blind approval.

Two holes remained once archive suffixes were refused:

- containers were recognised by SUFFIX only, so a gzip named model.bin or a
  zip named report.dat published after one "binary — not shown" approval;
- hash-pinning a non-UTF-8 file in the lockfile WAS the approval, and the
  lockfile is not a gate file — UTF-16 text or a compressed payload carrying a
  denylisted name shipped with no acknowledgement, while an image at least
  needed an `allow_media` manifest edit.

Now containers are sniffed by magic bytes whatever the name (never
approvable), a non-UTF-8 non-media file must ALSO match `[export]
allow_binary` in the manifest (a gate file: --ack-gate-changes), allowed
binaries are deny-scanned in their UTF-16 readings too, and `approve` names
the detected type and shows the printable strings instead of "not shown".
"""
import bz2
import gzip
import io
import lzma
import pickle
import re
import zipfile

import pytest

from kb_core.publish import lock, manifest, structural

PATS = [re.compile("secretname", re.IGNORECASE)]
SECRET = b"Meeting notes with secretname about the account\n"


def _zip(name="word/document.xml"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, SECRET)
    return buf.getvalue()


def _scan(tmp_path, files, *, pinned=True, allow_binary=()):
    for rel, data in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return structural.scan_files(tmp_path, sorted(files), PATS,
                                 is_pinned=(lambda r, d: True) if pinned else None,
                                 allow_binary=allow_binary)


def _kinds(rep):
    return sorted((h.path, h.pattern) for h in rep.hits)


CONTAINERS = {
    "src/model.bin": gzip.compress(SECRET),
    "src/report.dat": _zip(),
    "src/blob": bz2.compress(SECRET),
    "src/blob.xzz": lzma.compress(SECRET),
    "src/state.data": pickle.dumps({"who": "x"}, protocol=4),
    "src/cache": b"SQLite format 3\x00" + b"\x00" * 84,
    "src/arr.raw": b"\x93NUMPY\x01\x00" + b"x" * 20,
    "src/x.zst2": b"\x28\xb5\x2f\xfd" + b"\x00" * 10,
    "src/mail.olex": bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 20,
}


@pytest.mark.parametrize("rel", sorted(CONTAINERS))
def test_container_by_magic_refused_even_pinned_and_allowed(tmp_path, rel):
    rep = _scan(tmp_path, {rel: CONTAINERS[rel]}, allow_binary=("src/**",))
    assert any(h.pattern.startswith("structural:") and "magic" in h.pattern
               for h in rep.hits), _kinds(rep)


@pytest.mark.parametrize("rel", ["src/d.svgz", "src/m.msg", "src/x.eml", "src/w.pt",
                                 "src/m.safetensors", "src/a.mbox", "src/x.Z", "src/x.br"])
def test_more_container_suffixes_refused(tmp_path, rel):
    assert manifest.forbidden_suffix(rel), rel


def test_text_that_merely_starts_like_a_magic_is_fine(tmp_path):
    rep = _scan(tmp_path, {"src/notes.md": b"PAR1 is a parquet magic, BZh too\n"})
    assert rep.hits == []


def test_pinned_utf16_needs_allow_binary(tmp_path):
    data = "plain words\n".encode("utf-16-le")
    rep = _scan(tmp_path, {"src/notes.txt": data})
    assert any("allow_binary" in h.line for h in rep.hits), _kinds(rep)
    assert _scan(tmp_path, {"src/notes.txt": data},
                 allow_binary=("src/notes.txt",)).hits == []


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be", "utf-16"])
def test_allowed_binary_is_deny_scanned_as_utf16_too(tmp_path, codec):
    rep = _scan(tmp_path, {"src/notes.txt": SECRET.decode().encode(codec)},
                allow_binary=("src/**",))
    assert any(h.pattern == "secretname" for h in rep.hits), _kinds(rep)


def test_unpinned_allowed_binary_still_refused(tmp_path):
    rep = _scan(tmp_path, {"src/f.bin": b"\x00\x01\x02"}, pinned=False,
                allow_binary=("src/f.bin",))
    assert rep.hits


def test_manifest_accepts_and_validates_allow_binary(tmp_path):
    p = tmp_path / "publish.manifest.toml"
    p.write_text('[publish]\nremote = "x"\n[export]\ninclude = ["src/**"]\n'
                 'allow_binary = ["src/fonts/*.woff2"]\n')
    assert manifest.load_manifest(p).allow_binary == ("src/fonts/*.woff2",)
    p.write_text('[publish]\nremote = "x"\n[export]\ninclude = ["src/**"]\n'
                 'allow_binary = ["./src/x"]\n')
    with pytest.raises(manifest.ManifestError):
        manifest.load_manifest(p)


def test_describe_names_the_type_and_shows_strings():
    d = lock.describe("src/model.bin", gzip.compress(SECRET))
    assert "gzip" in d
    d = lock.describe("src/notes.txt", SECRET.decode().encode("utf-16-le"))
    assert "secretname" in d and "not shown" not in d
