import subprocess
from pathlib import Path

import pytest

from kb_core.publish import verify

SCRATCH = "postgresql://kb_test:pw@127.0.0.1:5433/kb_verify_scratch"


class _Rec:
    def __init__(self, fail_label=None):
        self.calls = []
        self.envs = []
        self.fail_label = fail_label

    def __call__(self, argv, cwd=None, **kw):
        self.calls.append((tuple(argv), Path(cwd).name if cwd else None))
        self.envs.append(kw.get("env"))
        rc = 1 if self.fail_label and self.fail_label in " ".join(argv) else 0
        return type("R", (), {"returncode": rc, "stdout": b"", "stderr": b"boom"})()


def test_runs_all_steps_in_order(tmp_path):
    rec = _Rec()
    verify._run_steps(tmp_path, rec, which=lambda name: f"/opt/bin/{name}")
    assert [c[0] for c in rec.calls] == [tuple(argv) for _l, _c, argv in verify.STEPS]


def test_raises_on_first_failure(tmp_path):
    rec = _Rec(fail_label="python.sh")
    with pytest.raises(verify.VerifyError) as e:
        verify._run_steps(tmp_path, rec, which=lambda name: f"/opt/bin/{name}")
    assert e.value.step == "python" and "scripts/ci/python.sh" in str(e.value)
    assert rec.calls[-1][0] == ("bash", "scripts/ci/python.sh")    # nothing after it


def test_steps_content_and_order():
    # Pin the exact ordered label list literally (not derived from verify.STEPS)
    # so a silent deletion, reorder, or rename of a step fails this test.
    # Every step is one scripts/ci/<job>.sh — the same script the ci.yml job of
    # that name runs (tests/test_publish_ci_scripts.py pins the other side).
    labels = [label for label, _rel_cwd, _argv in verify.STEPS]
    assert labels == ["gitleaks", "lint", "python", "kb-inference", "evals-tests", "frontend"]
    for label, rel_cwd, argv in verify.STEPS:
        assert rel_cwd == "." and argv == ["bash", f"scripts/ci/{label}.sh"]


# --- gitleaks is mandatory for a push ------------------------------------------

def test_gitleaks_absent_is_a_failure_when_required(tmp_path):
    rec = _Rec()
    with pytest.raises(verify.VerifyError) as e:
        verify._run_steps(tmp_path, rec, require_gitleaks=True, which=lambda _: None)
    assert e.value.step == "gitleaks" and "not installed" in str(e.value)
    assert rec.calls == []                       # nothing ran after the refusal


def test_gitleaks_absent_is_skipped_only_when_not_required(tmp_path, capsys):
    rec = _Rec()
    verify._run_steps(tmp_path, rec, require_gitleaks=False, which=lambda _: None)
    assert "scripts/ci/gitleaks.sh" not in [c[0][1] for c in rec.calls]
    assert len(rec.calls) == len(verify.STEPS) - 1
    assert "gitleaks not installed" in capsys.readouterr().out


def test_gitleaks_present_runs_first(tmp_path):
    rec = _Rec()
    verify._run_steps(tmp_path, rec, require_gitleaks=True,
                      which=lambda name: f"/opt/bin/{name}")
    assert rec.calls[0][0] == ("bash", "scripts/ci/gitleaks.sh")
    assert len(rec.calls) == len(verify.STEPS)


def test_verify_passes_requirement_through(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_VERIFY_DATABASE_URL", SCRATCH)
    monkeypatch.setattr(verify.subprocess, "run",
                        lambda *a, **kw: type("P", (), {"stdout": ""})())
    with pytest.raises(verify.VerifyError):
        verify.verify(tmp_path, _Rec(), require_gitleaks=True, which=lambda _: None)


# --- verify never touches a shared test database ------------------------------

def _no_pgrep(monkeypatch):
    monkeypatch.setattr(verify.subprocess, "run",
                        lambda *a, **kw: type("P", (), {"stdout": ""})())


def test_verify_refuses_without_a_scratch_database(tmp_path, monkeypatch):
    monkeypatch.delenv("KB_VERIFY_DATABASE_URL", raising=False)
    monkeypatch.setenv("KB_TEST_DATABASE_URL", "postgresql://kb_test:pw@127.0.0.1:5433/kb_mcp_test")
    _no_pgrep(monkeypatch)
    rec = _Rec()
    with pytest.raises(verify.VerifyError) as e:
        verify.verify(tmp_path, rec, which=lambda name: f"/opt/bin/{name}")
    assert e.value.step == "preflight" and "KB_VERIFY_DATABASE_URL" in str(e.value)
    assert rec.calls == []


@pytest.mark.parametrize("url", [
    "postgresql://kb_test:pw@127.0.0.1:5433/kb_mcp_test",       # the shared test DB
    "postgresql://kb:pw@127.0.0.1:5433/kb_mcp",                 # anything not kb_verify*
    "postgresql://kb_test:pw@127.0.0.1:5433/",                  # no database name
    "postgresql://kb_test:pw@127.0.0.1:5433/kb_verifyx?dbname=kb_mcp_test",
    "not a url",
])
def test_verify_refuses_a_non_scratch_database(tmp_path, monkeypatch, url):
    monkeypatch.setenv("KB_VERIFY_DATABASE_URL", url)
    _no_pgrep(monkeypatch)
    rec = _Rec()
    with pytest.raises(verify.VerifyError) as e:
        verify.verify(tmp_path, rec, which=lambda name: f"/opt/bin/{name}")
    assert e.value.step == "preflight" and "kb_verify" in str(e.value)
    assert rec.calls == []


def test_verify_points_the_scripts_at_the_scratch_database(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_VERIFY_DATABASE_URL", SCRATCH)
    monkeypatch.setenv("KB_TEST_DATABASE_URL", "postgresql://kb_test:pw@127.0.0.1:5433/kb_mcp_test")
    monkeypatch.setenv("KB_TEST_DATABASE_URL_B", "postgresql://kb_test:pw@127.0.0.1:5433/kb_mcp_test_b")
    _no_pgrep(monkeypatch)
    rec = _Rec()
    verify.verify(tmp_path, rec, which=lambda name: f"/opt/bin/{name}")
    assert len(rec.calls) == len(verify.STEPS)
    for env in rec.envs:
        assert env["KB_TEST_DATABASE_URL"] == SCRATCH
        assert "KB_TEST_DATABASE_URL_B" not in env      # python.sh derives <scratch>_b


def test_verify_withholds_the_publish_credential_and_target(tmp_path, monkeypatch):
    # The push session exports the deploy key and the real public remote. The
    # suites must run as CI does: a test resolving KB_PUBLISH_REMOTE fetched the
    # real public repo with the deploy key (one passphrase prompt per test).
    monkeypatch.setenv("KB_VERIFY_DATABASE_URL", SCRATCH)
    for var in verify.PUBLISH_ONLY_VARS:
        monkeypatch.setenv(var, "set-by-the-push-session")
    _no_pgrep(monkeypatch)
    rec = _Rec()
    verify.verify(tmp_path, rec, which=lambda name: f"/opt/bin/{name}")
    assert {"GIT_SSH_COMMAND", "KB_PUBLISH_REMOTE"} <= set(verify.PUBLISH_ONLY_VARS)
    for env in rec.envs:
        assert not set(verify.PUBLISH_ONLY_VARS) & set(env)


def test_verify_runs_the_scripts_in_a_one_commit_git_checkout(tmp_path, monkeypatch):
    # Like the public checkout and publish-candidate's export-ci: tests that use
    # git check-ignore / ls-files fail in a plain directory.
    monkeypatch.setenv("KB_VERIFY_DATABASE_URL", SCRATCH)
    (tmp_path / "a.py").write_text("a = 1\n")
    (tmp_path / ".gitignore").write_text("*.log\n")
    (tmp_path / "x.log").write_text("noise\n")
    verify.verify(tmp_path, _Rec(), which=lambda name: f"/opt/bin/{name}")
    files = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True,
                           text=True, check=True).stdout.split()
    assert files == [".gitignore", "a.py"]
    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                            capture_output=True, text=True, check=True).stdout
    assert status == ""


# --- concurrency: lock the scratch database, ignore unrelated pytests ----------

def test_unrelated_pytest_does_not_block_verify(tmp_path, monkeypatch):
    # Any pytest on the machine used to refuse --push ("another pytest is running").
    # verify's suites use a kb_verify* database nobody else may, so only another
    # verify on the SAME database is a collision.
    monkeypatch.setenv("KB_VERIFY_DATABASE_URL", SCRATCH)
    calls = []
    real_run = verify.subprocess.run
    monkeypatch.setattr(verify.subprocess, "run",
                        lambda argv, *a, **kw: calls.append(argv) or real_run(argv, *a, **kw))
    rec = _Rec()
    verify.verify(tmp_path, rec, which=lambda name: f"/opt/bin/{name}")
    assert not [c for c in calls if c and c[0] == "pgrep"]
    assert len(rec.calls) == len(verify.STEPS)


def test_second_verify_on_the_same_scratch_db_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_VERIFY_DATABASE_URL", SCRATCH)
    seen = []

    def nested(argv, **kw):
        if not seen:
            seen.append(argv)
            with pytest.raises(verify.VerifyError) as e:      # same DB, while held
                verify.verify(tmp_path, _Rec(), which=lambda name: f"/opt/bin/{name}")
            assert e.value.step == "preflight" and "kb_verify" in str(e.value)
            other = SCRATCH.rsplit("/", 1)[0] + "/kb_verify_other"
            monkeypatch.setenv("KB_VERIFY_DATABASE_URL", other)
            verify.verify(tmp_path, _Rec(), which=lambda name: f"/opt/bin/{name}")
            monkeypatch.setenv("KB_VERIFY_DATABASE_URL", SCRATCH)
        return type("P", (), {"returncode": 0, "stderr": b""})()

    verify.verify(tmp_path, nested, which=lambda name: f"/opt/bin/{name}")
    assert seen
