"""kb-library stage: prepare items from GitHub or a local folder for an agent
session to upload. Read-only toward the KB; no network in tests (http_get injected)."""
import io
import json
import tarfile

import pytest

from kb_core import library_client as C

SHA = "a" * 40


def _md(name, body="Body.\n"):
    return f"---\nname: {name}\ndescription: \"Example: for tests.\"\n---\n\n{body}"


def _repo_tarball(files: dict) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        for rel, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"acme-tools-{SHA[:7]}/{rel}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(f"acme-tools-{SHA[:7]}/skills/evil-link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc"
        tf.addfile(link)
    return out.getvalue()


REPO = {
    "skills/release-notes/SKILL.md": _md("release-notes",
                                         'Agent(subagent_type="code-auditor")\nAgent(subagent_type="test-runner")\n'),
    "skills/release-notes/ref/style.md": "Style.\n",
    "skills/release-notes/.tool-meta.json": "{}",
    "docs/ja/skills/release-notes/SKILL.md": _md("release-notes", "translated\n"),
    "agents/code-auditor.md": _md("code-auditor", "You audit.\n"),
    "README.md": "# Acme\n",
}


def _fake_get(calls):
    def get(url, accept=None, timeout=30.0):
        calls.append((url, accept))
        if url == "https://api.github.com/repos/acme/tools":
            return json.dumps({"default_branch": "main"}).encode()
        if url == "https://api.github.com/repos/acme/tools/commits/main":
            assert accept == "application/vnd.github.sha"
            return SHA.encode()
        if url == f"https://codeload.github.com/acme/tools/tar.gz/{SHA}":
            return _repo_tarball(REPO)
        raise AssertionError(url)
    return get


@pytest.fixture
def env(tmp_path):
    return {"KB_LIBRARY_CACHE": str(tmp_path / "cache"), "KB_LIBRARY_HOME": str(tmp_path / "home")}


def test_frontmatter_name_reads_plain_and_quoted_names():
    assert C.frontmatter_name("---\nname: a-b\n---\n") == "a-b"
    assert C.frontmatter_name("---\nname: 'a-b'\ndescription: x\n---\n") == "a-b"
    assert C.frontmatter_name("no frontmatter\nname: a\n") is None
    assert C.frontmatter_name("---\ndescription: x\n---\nname: late\n") is None


def test_ambiguous_names_are_refused_with_the_candidates(env):
    with pytest.raises(C.LibraryError, match=r"2 skills are named 'release-notes' \(docs/ja/skills/release-notes, skills/release-notes\)"):
        C.stage(env, "acme/tools", skills=("release-notes",), http_get=_fake_get([]))


def test_stage_from_github_pins_the_commit_and_names_missing_agents(env, tmp_path):
    calls = []
    out = C.stage(env, "acme/tools", skills=("release-notes",), path="skills/release-notes",
                  http_get=_fake_get(calls))
    meta = json.loads((out / "stage.json").read_text())
    assert meta["commit"] == SHA and meta["skills"] == ["release-notes"] and meta["agents"] == []
    assert meta["referenced_agents_not_staged"] == ["code-auditor", "test-runner"]
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "skills/release-notes/SKILL.md", "skills/release-notes/ref/style.md", "stage.json"]
    assert calls[-1][0].endswith(f"/tar.gz/{SHA}")


def test_stage_an_agent_by_name_from_github(env):
    out = C.stage(env, "acme/tools", agents=("code-auditor",), http_get=_fake_get([]))
    assert (out / "agents/code-auditor.md").read_text().startswith("---\nname: code-auditor")


def test_stage_a_local_skill_folder_and_a_local_agent_file(env, tmp_path):
    skill = tmp_path / "src/my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(_md("alpha-skill"))
    (skill / ".skillfish.json").write_text("{}")
    out = C.stage(env, str(skill))
    assert (out / "skills/alpha-skill/SKILL.md").exists()
    assert not (out / "skills/alpha-skill/.skillfish.json").exists()
    agent = tmp_path / "beta-agent.md"
    agent.write_text(_md("beta-agent"))
    out2 = C.stage(env, str(agent))
    assert (out2 / "agents/beta-agent.md").exists()
    assert out != out2
    assert not (out2 / "skills").exists() and not (out / "agents").exists()


def test_stage_refuses_binary_oversize_symlinks_and_missing_names(env, tmp_path):
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text(_md("alpha-skill"))
    (skill / "logo.png").write_bytes(b"\x89PNG\x00\xff")
    with pytest.raises(C.LibraryError, match="not UTF-8"):
        C.stage(env, str(skill))
    (skill / "logo.png").unlink()
    (skill / "big.md").write_text("x" * (256 * 1024 + 1))
    with pytest.raises(C.LibraryError, match="per-file limit"):
        C.stage(env, str(skill))
    (skill / "big.md").unlink()
    (skill / "link").symlink_to(tmp_path)
    with pytest.raises(C.LibraryError, match="symlink"):
        C.stage(env, str(skill))
    with pytest.raises(C.LibraryError, match="no skill named"):
        C.stage(env, "acme/tools", skills=("nope",), http_get=_fake_get([]))
    with pytest.raises(C.LibraryError, match="at least one"):
        C.stage(env, "acme/tools", http_get=_fake_get([]))
    with pytest.raises(C.LibraryError, match="exactly one"):
        C.stage(env, "acme/tools", skills=("a",), agents=("b",), path="x", http_get=_fake_get([]))


def test_print_stage_tells_the_session_what_to_do_next(env, capsys):
    out = C.stage(env, "acme/tools", skills=("release-notes",), path="skills/release-notes",
                  http_get=_fake_get([]))
    C.print_stage(out)
    text = capsys.readouterr().out
    assert str(out) in text and "upsert_skill" in text and "review_skill" in text
    assert "code-auditor, test-runner" in text


def _raw_tar(entries):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return out.getvalue()


def _get_with(tarball):
    def get(url, accept=None, timeout=30.0):
        if url.endswith("/tools"):
            return b'{"default_branch": "main"}'
        if url.endswith("/commits/main"):
            return SHA.encode()
        return tarball
    return get


def test_hostile_tarballs_are_refused(env):
    for name in ("r/../escape.md", "/abs.md"):
        with pytest.raises(C.LibraryError, match="unsafe path"):
            C.stage(env, "acme/tools", agents=("a",), http_get=_get_with(_raw_tar([(name, b"x")])))


def test_oversize_file_inside_a_staged_skill_is_refused_not_dropped(env):
    tb = _raw_tar([("r/skills/alpha-skill/SKILL.md", _md("alpha-skill").encode()),
                   ("r/skills/alpha-skill/big.md", b"x" * (C.MAX_FILE_BYTES + 1))])
    with pytest.raises(C.LibraryError, match="per-file limit"):
        C.stage(env, "acme/tools", skills=("alpha-skill",), http_get=_get_with(tb))


def test_decompression_bomb_is_capped(env, monkeypatch):
    monkeypatch.setattr(C, "MAX_UNPACKED_BYTES", 50_000)
    tb = _raw_tar([("r/x.bin", b"\0" * 200_000)])
    with pytest.raises(C.LibraryError, match="unpacks to more than"):
        C.stage(env, "acme/tools", agents=("a",), http_get=_get_with(tb))


def test_oversize_main_file_reports_the_real_cause(env):
    big = b"x" * (C.MAX_FILE_BYTES + 1)
    tb = _raw_tar([("r/README.md", b"hi"), ("r/skills/alpha-skill/SKILL.md", big), ("r/agents/beta-agent.md", big)])
    with pytest.raises(C.LibraryError, match="too large"):
        C.stage(env, "acme/tools", skills=("alpha-skill",), path="skills/alpha-skill",
                http_get=_get_with(tb))
    with pytest.raises(C.LibraryError, match="too large"):
        C.stage(env, "acme/tools", agents=("beta-agent",), path="agents/beta-agent.md",
                http_get=_get_with(tb))
    with pytest.raises(C.LibraryError, match="skipped as too large"):
        C.stage(env, "acme/tools", skills=("alpha-skill",), http_get=_get_with(tb))


def test_http_get_refuses_a_reply_over_the_limit(monkeypatch):
    class Resp:
        def __init__(self, n):
            self.n = n

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, limit):
            return b"x" * min(self.n, limit)

    monkeypatch.setattr(C, "MAX_ARCHIVE_BYTES", 10)
    monkeypatch.setattr(C.urllib.request, "urlopen", lambda *a, **k: Resp(50))
    with pytest.raises(C.LibraryError, match="larger than"):
        C._http_get("https://example.com/x")
    monkeypatch.setattr(C.urllib.request, "urlopen", lambda *a, **k: Resp(10))
    assert C._http_get("https://example.com/x") == b"x" * 10


def test_ref_is_quoted_in_the_api_url(env):
    seen = []

    def get(url, accept=None, timeout=30.0):
        seen.append(url)
        return b"not a sha"

    with pytest.raises(C.LibraryError, match="cannot resolve"):
        C.stage(env, "acme/tools", agents=("a",), ref="../x", http_get=get)
    assert seen == ["https://api.github.com/repos/acme/tools/commits/..%2Fx"]
