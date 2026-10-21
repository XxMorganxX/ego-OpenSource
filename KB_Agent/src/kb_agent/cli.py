"""`kb-agent init|run|status|sync|uninstall`."""
from __future__ import annotations

import argparse
import getpass
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from kb_agent import launchd
from kb_agent.client import GatewayClient, GatewayError
from kb_agent.config import (
    AgentConfig,
    ConfigError,
    check_concurrency,
    check_gateway,
    config_path,
    load_config,
    save_config,
    state_path,
)
from kb_agent.jobs import claude_auth_status, not_found_message
from kb_agent.loop import AgentLoop
from kb_agent.state import State

SKEW_LIMIT_S = 300          # the gateway refuses signatures more than five minutes off


def _gateway(cfg: AgentConfig) -> GatewayClient:
    return GatewayClient(cfg)


def _claude_status(cfg: AgentConfig) -> dict:
    return claude_auth_status(binary=cfg.claude_path or "claude")


def _concurrency(value: str) -> int:
    try:
        return check_concurrency(int(value))
    except (ValueError, ConfigError) as exc:
        raise argparse.ArgumentTypeError(str(exc) if isinstance(exc, ConfigError) else "must be 1-3") from None


def _kept_install_id() -> dict:
    """Re-running init keeps this install's id; only a first init makes a new one."""
    try:
        return {"install_id": load_config().install_id}
    except Exception:           # no config yet, or one too broken to load: start fresh
        return {}


def cmd_init(args) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        print(f"vault not found: {vault}", file=sys.stderr)
        return 2
    # Resolved here, from the user's shell PATH: the LaunchAgent's PATH is fixed and short.
    claude = shutil.which("claude")
    if not claude:
        print("claude CLI not found on PATH; install Claude Code and run `claude auth login`, "
              "then re-run kb-agent init", file=sys.stderr)
        return 2
    # Prompted, not required on argv: a flag lands in `ps` and in shell history.
    try:
        gateway = check_gateway(args.gateway.rstrip("/"))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    secret = args.secret or getpass.getpass("agent secret (from the owner): ").strip()
    if not secret:
        print("an agent secret is required", file=sys.stderr)
        return 2
    # Same rule for the Cloudflare service-token secret: --cf-client-secret still works,
    # but KB_AGENT_CF_CLIENT_SECRET or the prompt keep it out of `ps` and shell history.
    cf_secret = args.cf_client_secret or os.environ.get("KB_AGENT_CF_CLIENT_SECRET", "")
    if args.cf_client_id and not cf_secret:
        cf_secret = getpass.getpass("Cloudflare service token client secret: ").strip()
        if not cf_secret:
            print("--cf-client-id needs its client secret", file=sys.stderr)
            return 2
    cfg = AgentConfig(gateway=gateway, tenant=args.tenant, secret=secret, vault=vault,
                      cf_client_id=args.cf_client_id or "", cf_client_secret=cf_secret,
                      exclude=list(args.exclude or []), job_concurrency=args.concurrency,
                      claude_path=str(Path(claude).absolute()),
                      **_kept_install_id())
    p = save_config(cfg)
    print(f"wrote {p}")
    if not args.no_launchd:
        # The interpreter itself, never a shebang script: see launchd's module docstring.
        try:
            plist = launchd.install([sys.executable, "-m", "kb_agent.cli", "run"],
                                    path_dirs=[str(Path(cfg.claude_path).parent)])
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"could not install the login agent ({exc}); the config is written, "
                  "so re-run kb-agent init to retry", file=sys.stderr)
            return 1
        print(f"installed {plist} (runs at login; logs in ~/Library/Logs/kb-agent)")
    return 0


LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3


def _setup_logging(log_dir: Path) -> None:
    """agent.log, rotated at 5 MB with 3 backups; echoed to the terminal when run by hand."""
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        log_dir / "agent.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    root = logging.getLogger()
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(logging.INFO)


def cmd_run(_args) -> int:
    _setup_logging(launchd.LOG_DIR)
    cfg = load_config()
    AgentLoop(cfg, State(state_path())).run_forever()
    return 0


def cmd_sync(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_config()
    print(AgentLoop(cfg, State(state_path())).sync_once(manifest=bool(args.full)))
    return 0


def cmd_status(_args) -> int:
    cfg = load_config()
    print(f"tenant   {cfg.tenant}\ngateway  {cfg.gateway}\nvault    {cfg.vault}\nconfig   {config_path()}")
    auth = _claude_status(cfg)
    if auth.get("logged_in"):
        print(f"claude   logged in as {auth.get('email')}")
    elif auth.get("error") == "not_found":
        print(f"claude   {not_found_message(auth.get('path'))}")
    else:
        print("claude   NOT logged in; run: claude auth login")
    gw = _gateway(cfg)
    try:
        st = gw.status()
    except GatewayError as exc:
        if exc.status == 401:
            server = gw.probe_time()
            if server is not None:
                skew = abs((datetime.now(timezone.utc) - server).total_seconds())
                if skew > SKEW_LIMIT_S:
                    print(f"gateway  401: clock skew of {skew / 60:.0f} min between this Mac and the server; "
                          "fix the clock (System Settings > General > Date & Time)")
                    return 1
            print("gateway  401: secret or tenant rejected (ask the owner to check tenants.toml)")
            return 1
        print(f"gateway  error {exc.status}: {exc.detail[:120]}")
        return 1
    print(f"gateway  ok · open jobs {st.get('open_jobs')} · inbox pending {st.get('inbox_pending')} · "
          f"last seen {st.get('agent_last_seen_at')}")
    print(f"manifest {_manifest_line()}")
    return 0


def _manifest_line() -> str:
    """The last manifest generation this agent sent (it must keep rising: the server skips,
    and logs, any generation at or below the last one it applied)."""
    path = state_path()
    st = State(path) if path.exists() else None
    gen = st.get_kv("manifest_generation") if st is not None else None
    if not gen:
        return "none sent yet"
    return f"generation {gen} sent {st.get_kv('manifest_sent_at') or 'at an unknown time'}"


def cmd_uninstall(_args) -> int:
    launchd.uninstall()
    print("launchd agent removed; config and state left in place")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb-agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="write the config and install the login agent")
    i.add_argument("--gateway", required=True); i.add_argument("--tenant", required=True)
    i.add_argument("--secret", help="the agent secret; prompted for when omitted (keeps it out of shell history)")
    i.add_argument("--vault", required=True)
    i.add_argument("--cf-client-id")
    i.add_argument("--cf-client-secret",
                   help="prompted for (or read from KB_AGENT_CF_CLIENT_SECRET) when omitted with "
                        "--cf-client-id, which keeps it out of shell history")
    i.add_argument("--exclude", action="append", help="a vault path or glob never to upload (repeatable)")
    i.add_argument("--concurrency", type=_concurrency, default=2, help="concurrent claude jobs, 1-3")
    i.add_argument("--no-launchd", action="store_true"); i.set_defaults(fn=cmd_init)
    sub.add_parser("run", help="run the sync and job loops (what launchd starts)").set_defaults(fn=cmd_run)
    s = sub.add_parser("sync", help="one scan and push now")
    s.add_argument("--full", action="store_true", help="also post the manifest (prunes notes gone from the vault)")
    s.set_defaults(fn=cmd_sync)
    sub.add_parser("status", help="check the claude login and the gateway").set_defaults(fn=cmd_status)
    sub.add_parser("uninstall", help="remove the login agent").set_defaults(fn=cmd_uninstall)
    args = ap.parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except (FileNotFoundError, ConfigError) as exc:   # no config yet / an unusable one
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
