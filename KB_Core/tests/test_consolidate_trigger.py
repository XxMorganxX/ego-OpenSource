"""Decision rule for the churn-triggered consolidate daemon — pure, no DB."""
from datetime import datetime, timedelta, timezone

from kb_core.consolidate import should_consolidate

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


def _sig(last_change=None, backlog=0, last_run=None):
    return {"last_change": last_change, "backlog": backlog,
            "last_run_finished": last_run, "db_now": NOW}


def test_fires_when_quiet_and_drained():
    sig = _sig(last_change=NOW - timedelta(seconds=300),
               last_run=NOW - timedelta(hours=2))
    assert should_consolidate(sig, quiet_s=180, backstop_s=86400) == (True, "churn")


def test_holds_inside_quiet_window():
    sig = _sig(last_change=NOW - timedelta(seconds=30),
               last_run=NOW - timedelta(hours=2))
    assert should_consolidate(sig, quiet_s=180, backstop_s=86400) == (False, "not-quiet")


def test_holds_while_enrichment_backlog_remains():
    sig = _sig(last_change=NOW - timedelta(seconds=300), backlog=7,
               last_run=NOW - timedelta(hours=2))
    assert should_consolidate(sig, quiet_s=180, backstop_s=86400) == (False, "backlog")


def test_holds_when_nothing_changed_since_last_run():
    sig = _sig(last_change=NOW - timedelta(hours=3),
               last_run=NOW - timedelta(hours=2))
    assert should_consolidate(sig, quiet_s=180, backstop_s=86400) == (False, "no-change")


def test_backstop_fires_even_with_backlog_and_fresh_writes():
    sig = _sig(last_change=NOW - timedelta(seconds=30), backlog=7,
               last_run=NOW - timedelta(seconds=86401))
    assert should_consolidate(sig, quiet_s=180, backstop_s=86400) == (True, "backstop")


def test_virgin_db_fires_via_churn_path_once_quiet_and_drained():
    sig = _sig(last_change=NOW - timedelta(seconds=300))
    assert should_consolidate(sig, quiet_s=180, backstop_s=86400) == (True, "churn")


def test_virgin_db_mid_first_drain_holds():
    # The backstop must NOT serve a DB with no completed run — that would
    # reintroduce fire-on-start mid-drain (the backstop path).
    sig = _sig(last_change=NOW - timedelta(seconds=300), backlog=12)
    assert should_consolidate(sig, quiet_s=180, backstop_s=86400) == (False, "backlog")


def test_empty_db_holds():
    assert should_consolidate(_sig(), quiet_s=180, backstop_s=86400) == (False, "no-content")
