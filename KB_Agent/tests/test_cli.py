import plistlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from kb_agent import cli, launchd
from kb_agent.client import GatewayError
from kb_agent.config import load_config

import pytest

FAKE_CLAUDE = "/fake/bin/claude"


@pytest.fixture(autouse=True)
def _claude_on_path(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: FAKE_CLAUDE if name == "claude" else None)


def test_init_writes_config_and_installs_launchd(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    installed = {}
    def install(program, path_dirs=()):
        installed.update(program=program, path_dirs=list(path_dirs))
        return tmp_path / "x.plist"
    monkeypatch.setattr(cli.launchd, "install", install)
    vault = tmp_path / "V"; vault.mkdir()
    rc = cli.main(["init", "--gateway", "https://g", "--tenant", "alice", "--secret", "s" * 64, "--vault", str(vault),
                   "--exclude", "Private/**", "--no-launchd"])
    assert rc == 0 and (tmp_path / "config.toml").exists() and "program" not in installed
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.exclude == ["Private/**"] and cfg.claude_path == FAKE_CLAUDE   # absolute, for launchd's PATH
    rc = cli.main(["init", "--gateway", "https://g", "--tenant", "alice", "--secret", "s" * 64, "--vault", str(vault)])
    assert rc == 0 and installed["program"][-1] == "run"
    assert load_config(tmp_path / "config.toml").install_id == cfg.install_id   # re-init keeps the install id
    # launchd + a shebang script under ~/Desktop or ~/Documents is TCC-denied: exec the interpreter.
    assert installed["program"] == [sys.executable, "-m", "kb_agent.cli", "run"]
    assert installed["path_dirs"] == ["/fake/bin"]                # claude's own dir: nvm keeps node there


def test_init_prompts_for_the_secret_instead_of_requiring_it_on_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "p" * 64)
    vault = tmp_path / "V"; vault.mkdir()
    assert cli.main(["init", "--gateway", "https://g/", "--tenant", "a", "--vault", str(vault), "--no-launchd"]) == 0
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.secret == "p" * 64 and cfg.gateway == "https://g"


def test_the_launchd_module_path_actually_runs():
    out = subprocess.run([sys.executable, "-m", "kb_agent.cli", "--help"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "init" in out.stdout


def test_status_reports_clock_skew(tmp_path, monkeypatch, capsys):
    """A 401 plus a server_time far from ours is explained as clock skew."""
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    vault = tmp_path / "V"; vault.mkdir()
    cli.main(["init", "--gateway", "https://g", "--tenant", "a", "--secret", "s" * 64, "--vault", str(vault), "--no-launchd"])

    class _GW:
        def status(self):
            raise GatewayError(401, "unauthorized")

        def probe_time(self):
            return datetime.now(timezone.utc) - timedelta(minutes=20)
    monkeypatch.setattr(cli, "_gateway", lambda cfg: _GW())
    monkeypatch.setattr(cli, "_claude_status", lambda cfg: {"logged_in": True, "email": "a@x"})
    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert "clock skew" in out and "20 min" in out


def test_status_ok_and_plain_401(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    vault = tmp_path / "V"; vault.mkdir()
    cli.main(["init", "--gateway", "https://g", "--tenant", "a", "--secret", "s" * 64, "--vault", str(vault), "--no-launchd"])
    monkeypatch.setattr(cli, "_claude_status", lambda cfg: {"logged_in": False, "email": None})

    class _OK:
        def status(self):
            return {"open_jobs": 3, "inbox_pending": 0, "agent_last_seen_at": "now"}
    monkeypatch.setattr(cli, "_gateway", lambda cfg: _OK())
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "open jobs 3" in out and "NOT logged in" in out
    assert "manifest none sent yet" in out                       # a fresh install says so
    from kb_agent.config import state_path
    from kb_agent.state import State
    st = State(state_path())
    st.set_kv("manifest_generation", "1800000002")
    st.set_kv("manifest_sent_at", "2027-01-15T08:00:00+00:00")
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "manifest generation 1800000002 sent 2027-01-15T08:00:00+00:00" in out

    class _Denied:
        def status(self):
            raise GatewayError(401, "unauthorized")

        def probe_time(self):
            return datetime.now(timezone.utc)
    monkeypatch.setattr(cli, "_gateway", lambda cfg: _Denied())
    assert cli.main(["status"]) == 1
    assert "secret or tenant rejected" in capsys.readouterr().out


def test_init_fails_clearly_when_claude_is_not_on_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    vault = tmp_path / "V"; vault.mkdir()
    rc = cli.main(["init", "--gateway", "https://g", "--tenant", "a", "--secret", "s" * 64, "--vault", str(vault),
                   "--no-launchd"])
    assert rc == 2 and "claude CLI not found" in capsys.readouterr().err
    assert not (tmp_path / "config.toml").exists()


def test_status_says_claude_is_missing_not_logged_out(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    vault = tmp_path / "V"; vault.mkdir()
    cli.main(["init", "--gateway", "https://g", "--tenant", "a", "--secret", "s" * 64, "--vault", str(vault), "--no-launchd"])
    seen = {}

    def status(*, binary, **kw):
        seen["binary"] = binary
        return {"logged_in": False, "email": None, "error": "not_found", "path": binary}
    monkeypatch.setattr(cli, "claude_auth_status", status)

    class _OK:
        def status(self):
            return {}
    monkeypatch.setattr(cli, "_gateway", lambda cfg: _OK())
    cli.main(["status"])
    out = capsys.readouterr().out
    assert seen["binary"] == FAKE_CLAUDE
    assert f"claude CLI not found at {FAKE_CLAUDE}; re-run kb-agent init" in out and "logged in" not in out.lower()


def test_commands_without_a_config_say_run_init(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    assert cli.main(["status"]) == 2
    assert "kb-agent init" in capsys.readouterr().err


# --- launchd ----------------------------------------------------------------------------------

def test_plist_is_valid_xml_and_escapes_its_arguments(monkeypatch):
    monkeypatch.setenv("KB_AGENT_HOME", "/Users/a/K&B <home>")
    program = ["/Users/a/Docs & Things/.venv/bin/python", "-m", "kb_agent.cli", "run"]
    p = plistlib.loads(launchd.render(program).encode())
    assert p["Label"] == "com.kb.agent" and p["ProgramArguments"] == program
    assert p["KeepAlive"] is True and p["RunAtLoad"] is True
    assert p["EnvironmentVariables"]["KB_AGENT_HOME"] == "/Users/a/K&B <home>"
    assert "/opt/homebrew/bin" in p["EnvironmentVariables"]["PATH"]


def test_the_plist_path_starts_with_claudes_own_directory():
    """An npm/nvm `claude` is a `#!/usr/bin/env node` script; launchd's fixed PATH has no node."""
    nvm = "/Users/a/.nvm/versions/node/v20.11.0/bin"
    p = plistlib.loads(launchd.render(["python", "-m", "kb_agent.cli", "run"], path_dirs=[nvm]).encode())
    assert p["EnvironmentVariables"]["PATH"].split(":")[0] == nvm
    assert "/opt/homebrew/bin" in p["EnvironmentVariables"]["PATH"]


def test_install_bootstraps_and_uninstall_boots_out(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "AGENT_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(launchd, "LOG_DIR", tmp_path / "Logs")
    calls = []

    def run(args, **kw):
        calls.append((args, kw)); return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(launchd.subprocess, "run", run)
    path = launchd.install([sys.executable, "-m", "kb_agent.cli", "run"])
    assert path == tmp_path / "LaunchAgents" / "com.kb.agent.plist" and path.exists()
    assert [c[0][:2] for c in calls] == [["launchctl", "bootout"], ["launchctl", "bootstrap"]]
    assert calls[1][0][-1] == str(path) and all(c[1].get("timeout") for c in calls)
    calls.clear()
    launchd.uninstall()
    assert [c[0][:2] for c in calls] == [["launchctl", "bootout"]] and not path.exists()


def test_install_retries_a_bootstrap_that_races_the_bootout(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "AGENT_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(launchd, "LOG_DIR", tmp_path / "Logs")
    monkeypatch.setattr(launchd.time, "sleep", lambda s: None)
    rcs = {"bootout": [0], "bootstrap": [5, 0]}

    def run(args, **kw):
        return subprocess.CompletedProcess(args, rcs[args[1]].pop(0), "", "Bootstrap failed: 5: Input/output error")
    monkeypatch.setattr(launchd.subprocess, "run", run)
    assert launchd.install(["python", "-m", "kb_agent.cli", "run"]).exists() and rcs["bootstrap"] == []


def test_init_refuses_a_concurrency_outside_1_to_3(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    vault = tmp_path / "V"; vault.mkdir()
    with pytest.raises(SystemExit) as exc:
        cli.main(["init", "--gateway", "https://g", "--tenant", "a", "--secret", "s" * 64, "--vault", str(vault),
                  "--no-launchd", "--concurrency", "4"])
    assert exc.value.code == 2 and "1-3" in capsys.readouterr().err
    assert not (tmp_path / "config.toml").exists()


def test_a_bad_config_is_one_clear_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    vault = tmp_path / "V"; vault.mkdir()
    cli.main(["init", "--gateway", "https://g", "--tenant", "a", "--secret", "s" * 64, "--vault", str(vault), "--no-launchd"])
    p = tmp_path / "config.toml"
    p.write_text(p.read_text().replace("job_concurrency = 2", "job_concurrency = 8"))
    assert cli.main(["status"]) == 2 and "job_concurrency" in capsys.readouterr().err


def test_a_failed_launchd_install_is_one_line_and_keeps_the_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    vault = tmp_path / "V"; vault.mkdir()
    for exc in (RuntimeError("launchctl bootstrap failed (5): Input/output error"),
                subprocess.TimeoutExpired(["launchctl", "bootstrap"], 30)):
        def install(program, path_dirs=(), exc=exc):
            raise exc
        monkeypatch.setattr(cli.launchd, "install", install)
        rc = cli.main(["init", "--gateway", "https://g", "--tenant", "a", "--secret", "s" * 64, "--vault", str(vault)])
        err = capsys.readouterr().err
        assert rc == 1 and (tmp_path / "config.toml").exists()
        assert "could not install the login agent" in err and len(err.strip().splitlines()) == 1


def test_run_logs_to_a_rotating_file(tmp_path):
    import logging
    from logging.handlers import RotatingFileHandler
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    try:
        cli._setup_logging(tmp_path / "logs")
        [h] = [h for h in root.handlers if h not in before]
        assert isinstance(h, RotatingFileHandler)
        assert h.maxBytes == 5 * 1024 * 1024 and h.backupCount == 3
        assert h.baseFilename == str(tmp_path / "logs" / "agent.log")
        logging.getLogger("kb-agent").info("job 7 done")
        h.flush()
        assert "job 7 done" in (tmp_path / "logs" / "agent.log").read_text()
    finally:
        for h in [h for h in root.handlers if h not in before]:
            root.removeHandler(h); h.close()
        root.setLevel(level)


def test_launchd_output_goes_to_its_own_file_not_the_rotated_log():
    """Two writers on one file break rotation: launchd keeps its fd on the renamed file."""
    p = plistlib.loads(launchd.render(["python", "-m", "kb_agent.cli", "run"]).encode())
    assert p["StandardOutPath"] == p["StandardErrorPath"] == str(launchd.LOG_DIR / "launchd.log")


# --- the gateway must be https --------------------------------------------------------

@pytest.mark.parametrize("gateway", ["http://kb-agent.example.com", "http://192.168.1.5:8040",
                                     "ftp://g", "kb-agent.example.com"])
def test_init_refuses_a_gateway_that_is_not_https(tmp_path, monkeypatch, capsys, gateway):
    """Every request carries the CF service-token secret and the vault's note text."""
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(cli.getpass, "getpass",
                        lambda prompt: pytest.fail("prompted before refusing the gateway"))
    vault = tmp_path / "V"; vault.mkdir()
    rc = cli.main(["init", "--gateway", gateway, "--tenant", "a", "--vault", str(vault),
                   "--no-launchd"])
    assert rc == 2 and "https" in capsys.readouterr().err
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.parametrize("gateway", ["http://127.0.0.1:8040", "http://localhost:8040",
                                     "http://[::1]:8040", "https://kb-agent.example.com"])
def test_init_accepts_https_or_a_loopback_gateway(tmp_path, monkeypatch, gateway):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    vault = tmp_path / "V"; vault.mkdir()
    assert cli.main(["init", "--gateway", gateway, "--tenant", "a", "--secret", "s" * 64,
                     "--vault", str(vault), "--no-launchd"]) == 0


def test_load_config_refuses_a_plain_http_gateway(tmp_path):
    from kb_agent.config import AgentConfig, ConfigError, save_config
    p = save_config(AgentConfig(gateway="http://kb-agent.example.com", tenant="a",
                                secret="s" * 64, vault=tmp_path), tmp_path / "config.toml")
    with pytest.raises(ConfigError, match="https"):
        load_config(p)


def test_init_prompts_for_the_cf_secret_instead_of_requiring_it_on_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("KB_AGENT_CF_CLIENT_SECRET", raising=False)
    prompts = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: prompts.append(prompt) or "cs")
    vault = tmp_path / "V"; vault.mkdir()
    assert cli.main(["init", "--gateway", "https://g", "--tenant", "a", "--secret", "s" * 64,
                     "--vault", str(vault), "--cf-client-id", "cid", "--no-launchd"]) == 0
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.cf_client_id == "cid" and cfg.cf_client_secret == "cs"
    assert len(prompts) == 1 and "Cloudflare" in prompts[0]


def test_init_reads_the_cf_secret_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("KB_AGENT_CF_CLIENT_SECRET", "from-env")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: pytest.fail("prompted"))
    vault = tmp_path / "V"; vault.mkdir()
    assert cli.main(["init", "--gateway", "https://g", "--tenant", "a", "--secret", "s" * 64,
                     "--vault", str(vault), "--cf-client-id", "cid", "--no-launchd"]) == 0
    assert load_config(tmp_path / "config.toml").cf_client_secret == "from-env"
