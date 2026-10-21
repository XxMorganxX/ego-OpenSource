"""kb-library: the device sync client. No database: a local HTTP server serves
archives built by kb_core.library_export.build_archive (the server's own builder)."""
import ast
import hashlib
import http.server
import io
import json
import tarfile
import threading
from pathlib import Path

import pytest

from kb_core import library_client as C
from kb_core.library_export import build_archive


def _md(name, body="v1\n"):
    return f"---\nname: {name}\ndescription: Example for tests.\n---\n\n{body}"


def _item(name, rev=1, files=None, kind="skill"):
    files = files or ({"SKILL.md": _md(name)} if kind == "skill"
                      else {"AGENT.md": _md(name, "You audit.\n")})
    return {"name": name, "revision": rev, "package_hash": hashlib.sha256(repr(files).encode()).hexdigest(),
            "description": "d", "files": files}


class _Lib:
    """What the fake dashboard serves: kind -> (etag, body), plus a log of request headers."""
    def __init__(self):
        self.archives, self.requests, self.status = {}, [], None

    def set(self, kind, items):
        self.archives[kind] = build_archive(kind, items)


@pytest.fixture
def lib():
    state = _Lib()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            # the Message itself: header lookups are case-insensitive (urllib sends
            # "Cf-access-client-id", which a plain dict would not find)
            state.requests.append((self.path, self.headers))
            if state.status:
                self.send_response(state.status)
                self.send_header("Location", "https://example.cloudflareaccess.com/login")
                self.end_headers()
                return
            kind = {"/api/library/skills": "skill", "/api/library/agents": "agent"}.get(self.path)
            etag, body = state.archives.get(kind, build_archive(kind, []))
            if self.headers.get("If-None-Match") == f'"{etag}"':
                self.send_response(304)
                self.send_header("ETag", f'"{etag}"')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("ETag", f'"{etag}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield state
    srv.shutdown()


@pytest.fixture
def env(tmp_path, lib):
    return {"KB_LIBRARY_HOME": str(tmp_path / "home"), "KB_LIBRARY_CLAUDE_DIR": str(tmp_path / "claude"),
            "KB_LIBRARY_CACHE": str(tmp_path / "cache"), "KB_LIBRARY_URL": lib.url,
            "KB_LIBRARY_CLIENT_ID": "dev-a.access", "KB_LIBRARY_CLIENT_SECRET": "s3cret"}


def test_the_client_imports_only_the_standard_library():
    tree = ast.parse(Path(C.__file__).read_text())
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not mods & {"kb_core", "psycopg", "frontmatter", "yaml", "requests", "httpx"}


def test_sync_installs_both_kinds_and_sends_the_service_token(env, lib, tmp_path):
    lib.set("skill", [_item("deploy-checklist", files={"SKILL.md": _md("deploy-checklist"), "ref/n.md": "n\n"})])
    lib.set("agent", [_item("code-auditor", kind="agent")])
    report = C.sync(env)
    claude = tmp_path / "claude"
    assert (claude / "skills/deploy-checklist/ref/n.md").read_text() == "n\n"
    assert (claude / "agents/code-auditor.md").read_text().startswith("---\nname: code-auditor")
    assert report == ["installed skill deploy-checklist (revision 1)", "installed agent code-auditor (revision 1)"]
    _, headers = lib.requests[0]
    assert headers["CF-Access-Client-Id"] == "dev-a.access" and headers["CF-Access-Client-Secret"] == "s3cret"
    assert headers["Accept-Encoding"] == "identity"


def test_unchanged_library_is_a_304_and_writes_nothing(env, lib, tmp_path):
    lib.set("skill", [_item("deploy-checklist")])
    C.sync(env)
    f = tmp_path / "claude/skills/deploy-checklist/SKILL.md"
    f.write_text("local edit\n")
    assert C.sync(env) == []
    assert f.read_text() == "local edit\n"                        # 304: nothing fetched
    assert lib.requests[-1][1]["If-None-Match"].startswith('"')


def test_a_change_overwrites_local_edits_and_removes_only_managed_items(env, lib, tmp_path):
    lib.set("skill", [_item("deploy-checklist"), _item("review-infra")])
    C.sync(env)
    skills = tmp_path / "claude/skills"
    (skills / "hand-made").mkdir()
    (skills / "hand-made/SKILL.md").write_text(_md("hand-made"))
    (skills / "deploy-checklist/SKILL.md").write_text("local edit\n")
    lib.set("skill", [_item("deploy-checklist", rev=2, files={"SKILL.md": _md("deploy-checklist", "v2\n")})])
    report = C.sync(env)
    assert (skills / "deploy-checklist/SKILL.md").read_text().endswith("v2\n")
    assert not (skills / "review-infra").exists()
    assert (skills / "hand-made/SKILL.md").exists()
    assert report == ["updated skill deploy-checklist (revision 2)", "removed skill review-infra"]


def test_adopting_a_symlinked_item_replaces_the_link_not_its_target(env, lib, tmp_path):
    """The link itself is replaced; the folder it points at is left alone."""
    target = tmp_path / "elsewhere/deploy-checklist"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("theirs\n")
    skills = tmp_path / "claude/skills"
    skills.mkdir(parents=True)
    (skills / "deploy-checklist").symlink_to(target)
    lib.set("skill", [_item("deploy-checklist")])
    assert C.sync(env) == ["adopted skill deploy-checklist (revision 1)"]
    assert not (skills / "deploy-checklist").is_symlink()
    assert (skills / "deploy-checklist/SKILL.md").read_text() == _md("deploy-checklist")
    assert (target / "SKILL.md").read_text() == "theirs\n"         # never followed


def test_access_login_redirect_is_a_clear_error_and_writes_nothing(env, lib, tmp_path, capsys):
    """An Access login redirect is reported clearly and nothing is written."""
    lib.status = 302
    with pytest.raises(C.LibraryError, match="service token"):
        C.sync(env)
    assert not (tmp_path / "claude/skills").exists()
    assert C.main(["sync", "--hook"], env=env) == 0
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("kb-library: ") and "302" in err[0]


def test_hook_mode_is_silent_when_nothing_changed_and_exits_0_offline(env, lib, capsys):
    lib.set("skill", [_item("deploy-checklist")])
    assert C.main(["sync", "--hook"], env=env) == 0
    assert "deploy-checklist" in capsys.readouterr().out
    assert C.main(["sync", "--hook"], env=env) == 0
    assert capsys.readouterr().out == ""
    offline = dict(env, KB_LIBRARY_URL="http://127.0.0.1:9")    # nothing listens on discard
    assert C.main(["sync", "--hook"], env=offline) == 0
    assert C.main(["sync"], env=offline) == 1


def _tamper(body, *, replace=None, extra=None):
    """Rebuild an archive with one entry's bytes replaced and/or one unlisted entry added."""
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as src, \
            tarfile.open(fileobj=out, mode="w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            if replace and m.name == replace[0]:
                data = replace[1]
                m.size = len(data)
            dst.addfile(m, io.BytesIO(data))
        if extra:
            info = tarfile.TarInfo(extra)
            info.size = 1
            dst.addfile(info, io.BytesIO(b"x"))
    return out.getvalue()


def test_one_bad_entry_refuses_the_whole_kind(env, lib, tmp_path):
    """One malformed entry refuses the whole kind; nothing is half-installed."""
    etag, body = build_archive("skill", [_item("deploy-checklist"), _item("review-infra")])
    for bad in (_tamper(body, replace=("skills/review-infra/SKILL.md", b"evil\n")),
                _tamper(body, extra="skills/sneaky/SKILL.md")):
        lib.archives["skill"] = (etag, bad)
        with pytest.raises(C.LibraryError):
            C.sync(env)
        assert not (tmp_path / "claude/skills/deploy-checklist").exists()


@pytest.mark.parametrize("name", ["../escape", "/abs/path", "skills/x/../../y"])
def test_parse_archive_refuses_unsafe_paths(name):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        info = tarfile.TarInfo(name)
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(C.LibraryError, match="unsafe path"):
        C.parse_archive("skill", out.getvalue())


def test_parse_archive_refuses_a_symlink_entry():
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        info = tarfile.TarInfo("skills/x/SKILL.md")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(C.LibraryError, match="not a regular file"):
        C.parse_archive("skill", out.getvalue())


def test_a_crashed_runs_leftovers_are_cleaned_and_never_inside_skills(env, lib, tmp_path):
    """Scratch lives in ~/.claude/.kb-library-work, never skills/."""
    work = tmp_path / "claude/.kb-library-work/skill-deploy-checklist.kb-library-tmp"
    work.mkdir(parents=True)
    (work / "SKILL.md").write_text("half\n")
    lib.set("skill", [_item("deploy-checklist")])
    C.sync(env)
    assert not (tmp_path / "claude/.kb-library-work").exists()
    assert sorted(p.name for p in (tmp_path / "claude/skills").iterdir()) == ["deploy-checklist"]


def test_a_second_concurrent_sync_skips(env, lib, tmp_path):
    import fcntl
    home = tmp_path / "home"
    home.mkdir()
    with open(home / "sync.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert C.sync(env) == ["another kb-library sync is running; skipped"]


def test_login_saves_a_0600_config_and_status_lists_managed_items(env, lib, tmp_path, capsys):
    env2 = {k: v for k, v in env.items() if not k.startswith("KB_LIBRARY_CLIENT")}
    env2.pop("KB_LIBRARY_URL")
    env2["KB_LIBRARY_CLIENT_SECRET"] = "s3cret"
    assert C.main(["login", "--url", lib.url + "/", "--client-id", "dev-a.access"], env=env2) == 0
    cfg_path = tmp_path / "home/config.json"
    assert oct(cfg_path.stat().st_mode & 0o777) == "0o600"
    assert json.loads(cfg_path.read_text()) == {"url": lib.url, "client_id": "dev-a.access",
                                                "client_secret": "s3cret"}
    lib.set("skill", [_item("deploy-checklist", rev=3)])
    C.sync(env2)
    lines = C.status_lines(env2)
    assert lines[0] == f"library: {lib.url}" and "  deploy-checklist (revision 3)" in lines


def test_install_hook_is_idempotent_and_keeps_other_settings(env, tmp_path):
    settings = tmp_path / "claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "x", "hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": "echo hi"}]}]}}))
    script = Path(C.__file__)
    assert C.install_hook(env, script) is True
    assert C.install_hook(env, script) is False
    data = json.loads(settings.read_text())
    assert data["model"] == "x"
    cmds = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    assert cmds[0] == "echo hi" and cmds[1].endswith(f"{script} sync --hook") and " -I " in cmds[1]


def test_install_hook_refuses_a_settings_file_that_is_not_json(env, tmp_path):
    settings = tmp_path / "claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json")
    with pytest.raises(C.LibraryError, match="not touching it"):
        C.install_hook(env, Path(C.__file__))
    assert settings.read_text() == "{not json"


def test_hook_mode_has_a_total_time_budget_against_a_dripping_server(env, tmp_path, monkeypatch, capsys):
    import socket
    import time
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def drip():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
        while not stop.is_set():
            try:
                conn.sendall(b"x")
            except OSError:
                break
            time.sleep(0.2)
    threading.Thread(target=drip, daemon=True).start()
    monkeypatch.setattr(C, "HOOK_BUDGET_S", 1.0)
    e = dict(env, KB_LIBRARY_URL=f"http://127.0.0.1:{srv.getsockname()[1]}")
    t0 = time.monotonic()
    assert C.main(["sync", "--hook"], env=e) == 0
    stop.set()
    assert time.monotonic() - t0 < 3
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and ("gave up" in err[0] or "timed out" in err[0])
    assert not (tmp_path / "claude/skills").exists()


def test_an_oversized_member_refuses_the_whole_kind(env, lib, tmp_path, monkeypatch):
    lib.set("skill", [_item("deploy-checklist", files={"SKILL.md": _md("deploy-checklist", "x" * 500)})])
    monkeypatch.setattr(C, "MAX_FILE_BYTES", 100)
    with pytest.raises(C.LibraryError, match="too large"):
        C.sync(env)
    assert not (tmp_path / "claude/skills/deploy-checklist").exists()


def test_a_skipped_sync_is_silent_in_hook_mode(env, tmp_path, capsys):
    import fcntl
    home = tmp_path / "home"
    home.mkdir()
    with open(home / "sync.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert C.main(["sync", "--hook"], env=env) == 0
    assert capsys.readouterr().out == ""


def test_plain_sync_prints_lines_then_a_summary(env, lib, capsys):
    lib.set("skill", [_item("deploy-checklist")])
    assert C.main(["sync"], env=env) == 0
    assert capsys.readouterr().out.splitlines() == [
        "installed skill deploy-checklist (revision 1)", "kb-library: 1 changed"]
    assert C.main(["sync"], env=env) == 0
    assert capsys.readouterr().out == "up to date\n"


def test_an_oserror_during_install_is_a_one_line_error(env, lib, tmp_path, capsys):
    lib.set("skill", [_item("deploy-checklist")])
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude/skills").write_text("a file where the directory should be")
    with pytest.raises(C.LibraryError, match="cannot install"):
        C.sync(env)
    assert C.main(["sync", "--hook"], env=env) == 0
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_save_config_refuses_a_symlink_and_tightens_the_directory(env, tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    target = tmp_path / "victim"
    (home / "config.json").symlink_to(target)
    with pytest.raises(C.LibraryError):
        C.save_config(env, {"url": "x"})
    assert not target.exists()
    (home / "config.json").unlink()
    C.save_config(env, {"url": "x"})
    assert oct(home.stat().st_mode & 0o777) == "0o700"


def test_state_names_that_are_not_valid_item_names_are_dropped(env, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "state.json").write_text(json.dumps({"version": 1, "url": "u", "kinds": {
        "skill": {"etag": None, "managed": {"../../x": {"revision": 1}, "ok-name": {"revision": 1}},
                  "synced_at": None}}}))
    assert C._load_state(home)["kinds"]["skill"]["managed"] == {"ok-name": {"revision": 1}}


def test_login_refuses_plain_http_off_loopback(env, capsys):
    assert C.main(["login", "--url", "http://kb.example.com", "--client-id", "dev-a.access"], env=env) == 1
    assert "https" in capsys.readouterr().err


@pytest.mark.parametrize("status", [302, 401, 403])
def test_an_access_refusal_points_at_the_service_token(env, lib, tmp_path, status):
    """A 401/403 gets the same hint as the login redirect."""
    lib.status = status
    with pytest.raises(C.LibraryError, match=rf"HTTP {status}.*check the service token "
                                             r"\(KB_LIBRARY_CLIENT_ID / kb-library login\)"):
        C.sync(env)
    assert not (tmp_path / "claude/skills").exists()


def test_install_hook_writes_through_a_symlinked_settings_file(env, tmp_path):
    """A dotfiles-managed ~/.claude/settings.json stays a link."""
    target = tmp_path / "dotfiles/claude-settings.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"model": "x"}))
    target.chmod(0o640)
    link = tmp_path / "claude/settings.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    assert C.install_hook(env, Path(C.__file__)) is True
    assert link.is_symlink() and link.resolve() == target.resolve()
    data = json.loads(target.read_text())
    assert data["model"] == "x" and "sync --hook" in data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert target.stat().st_mode & 0o777 == 0o640
    assert sorted(p.name for p in target.parent.iterdir()) == ["claude-settings.json"]


@pytest.mark.parametrize("settings", [
    [], {"hooks": []}, {"hooks": {"SessionStart": {}}}, {"hooks": {"SessionStart": ["x"]}},
    {"hooks": {"SessionStart": [{"hooks": "echo hi"}]}},
    {"hooks": {"SessionStart": [{"hooks": ["echo hi"]}]}},
])
def test_install_hook_refuses_an_unexpected_hooks_layout(env, tmp_path, settings):
    path = tmp_path / "claude/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(settings))
    with pytest.raises(C.LibraryError, match="unexpected .*not touching it"):
        C.install_hook(env, Path(C.__file__))
    assert json.loads(path.read_text()) == settings


def test_parse_archive_caps_fire_before_the_rest_of_the_stream_is_read(monkeypatch):
    """The member cap must stop a long archive at the cap, not after
    decompressing all of it. The tail here is truncated: reading it would be a tar
    error instead of the cap's refusal."""
    import os as _os
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tf:
        for name, data in (("manifest.json", b"{}"), ("skills/a/SKILL.md", b"a"),
                           ("skills/b/SKILL.md", _os.urandom(200_000))):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    monkeypatch.setattr(C, "MAX_MEMBERS", 1)
    monkeypatch.setattr(C, "MAX_FILE_BYTES", 300_000)
    with pytest.raises(C.LibraryError, match="too large"):
        C.parse_archive("skill", raw.getvalue()[:-5000])
