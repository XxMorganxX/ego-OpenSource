"""The manual-consolidation request queue: schema, claim/finish lifecycle, and the
status state machine.
"""


def test_migration_creates_the_request_queue_and_run_provenance(kb):
    with kb._pool.connection() as conn:
        conn.execute(
            "INSERT INTO concept_run_requests (source) VALUES ('dashboard')")
        row = conn.execute(
            "SELECT source, claimed_at, finished_at, run_id, error, "
            "       requested_at IS NOT NULL AS stamped "
            "FROM concept_run_requests").fetchone()
        assert row == {"source": "dashboard", "claimed_at": None, "finished_at": None,
                       "run_id": None, "error": None, "stamped": True}
        # concept_runs gains provenance + a coarse phase, defaulting to the churn path.
        run = conn.execute(
            "INSERT INTO concept_runs (params) VALUES ('{}'::jsonb) "
            "RETURNING trigger_reason, phase").fetchone()
        assert run == {"trigger_reason": "churn", "phase": None}


def test_both_new_tables_are_on_the_live_change_feed(kb):
    # The dashboard's WebSocket carries kb_change; concept state has to reach it or the
    # rebuild button would have to poll instead.
    with kb._pool.connection() as conn:
        rows = conn.execute(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
            "  AND tgrelid IN ('concept_runs'::regclass, "
            "                  'concept_run_requests'::regclass)").fetchall()
    assert {r["tgname"] for r in rows} == {"concept_runs_notify",
                                          "concept_run_requests_notify"}


def test_consolidation_lock_is_exclusive(kb):
    import pytest

    from kb_core.knowledge_base import ConsolidationBusy

    with kb.consolidation_lock():
        with pytest.raises(ConsolidationBusy, match="another consolidation pass"):
            with kb.consolidation_lock():
                pass


def test_consolidation_lock_is_released_after_the_block(kb):
    with kb.consolidation_lock():
        pass
    with kb.consolidation_lock():          # re-acquirable, so the unlock really ran
        pass


def test_run_once_refuses_to_start_while_a_pass_holds_the_lock(kb):
    import pytest

    from kb_core.consolidate import run_once
    from kb_core.knowledge_base import ConsolidationBusy

    with kb.consolidation_lock():
        with pytest.raises(ConsolidationBusy):
            run_once(kb, None, min_memories=3)
    # ...and nothing was written: the refusal happens before the audit row opens.
    with kb._pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM concept_runs").fetchone()["n"] == 0


def test_orphan_sweep_fails_rows_left_running(kb):
    with kb._pool.connection() as conn:
        conn.execute("INSERT INTO concept_runs (params, status) "
                     "VALUES ('{}'::jsonb, 'running')")
    assert kb.sweep_orphaned_concept_runs() == 1
    with kb._pool.connection() as conn:
        row = conn.execute("SELECT status, finished_at IS NOT NULL AS closed "
                           "FROM concept_runs").fetchone()
    assert row == {"status": "failed", "closed": True}


def test_orphan_sweep_skips_entirely_while_a_pass_holds_the_lock(kb):
    """A held lock means a live `kb-consolidate --apply` run — its row must survive."""
    with kb._pool.connection() as conn:
        conn.execute("INSERT INTO concept_runs (params, status) "
                     "VALUES ('{}'::jsonb, 'running')")
    with kb.consolidation_lock():
        assert kb.sweep_orphaned_concept_runs() == 0
    with kb._pool.connection() as conn:
        assert conn.execute(
            "SELECT status FROM concept_runs").fetchone()["status"] == "running"


def _insert_run(kb, status, *, phase=None, trigger="manual", age_s=0, stats=None):
    """A concept_runs row aged `age_s` seconds into the past, on the DB clock."""
    import json

    with kb._pool.connection() as conn:
        return conn.execute(
            "INSERT INTO concept_runs (params, status, phase, trigger_reason, "
            "                          started_at, finished_at, stats) "
            "VALUES ('{}'::jsonb, %(s)s, %(ph)s, %(t)s, "
            "        now() - make_interval(secs => %(age)s), "
            "        CASE WHEN %(s)s = 'running' THEN NULL "
            "             ELSE now() - make_interval(secs => %(age)s) END, "
            "        %(st)s::jsonb) RETURNING id",
            {"s": status, "ph": phase, "t": trigger, "age": age_s,
             "st": json.dumps(stats or {})}).fetchone()["id"]


def test_idle_status_on_a_virgin_db(kb):
    st = kb.consolidation_run_status()
    assert st["state"] == "idle"
    assert st["request"] is None and st["running"] is None and st["last"] is None
    assert st["backlog"] == 0


def test_requesting_queues_one_row_and_reports_queued(kb):
    st = kb.request_consolidation()
    assert st["state"] == "queued"
    assert st["request"]["claimed_at"] is None
    with kb._pool.connection() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM concept_run_requests").fetchone()["n"] == 1


def test_requesting_twice_while_queued_does_not_queue_a_second_pass(kb):
    kb.request_consolidation()
    kb.request_consolidation()
    with kb._pool.connection() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM concept_run_requests").fetchone()["n"] == 1


def test_claim_takes_every_unclaimed_request_so_clicks_coalesce(kb):
    with kb._pool.connection() as conn:
        conn.execute("INSERT INTO concept_run_requests (source) "
                     "VALUES ('dashboard'), ('dashboard'), ('dashboard')")
    ids = kb.claim_consolidation_requests()
    assert len(ids) == 3
    assert kb.claim_consolidation_requests() == []      # nothing left to claim


def test_release_puts_a_claim_back_for_the_next_poll(kb):
    kb.request_consolidation()
    ids = kb.claim_consolidation_requests()
    kb.release_consolidation_requests(ids)
    assert kb.claim_consolidation_requests() == ids


def test_finish_stamps_the_serving_run_on_every_coalesced_request(kb):
    with kb._pool.connection() as conn:
        conn.execute("INSERT INTO concept_run_requests (source) "
                     "VALUES ('dashboard'), ('dashboard')")
    ids = kb.claim_consolidation_requests()
    run_id = _insert_run(kb, "done", phase="applying")
    kb.finish_consolidation_requests(ids, run_id, None)
    with kb._pool.connection() as conn:
        rows = conn.execute("SELECT run_id, error, finished_at IS NOT NULL AS done "
                            "FROM concept_run_requests").fetchall()
    assert rows == [{"run_id": run_id, "error": None, "done": True}] * 2


def test_running_row_reports_running_with_phase(kb):
    _insert_run(kb, "running", phase="labeling")
    st = kb.consolidation_run_status()
    assert st["state"] == "running"
    assert st["running"]["phase"] == "labeling"
    assert st["running"]["trigger_reason"] == "manual"


def test_an_old_unclaimed_request_during_a_running_pass_is_never_stalled(kb):
    """The daemon cannot claim while it is mid-pass, so an old unclaimed request is NOT
    evidence of a dead daemon. Without this the UI would cry wolf on every click made
    during a run. An in-flight pass wins the label — "Consolidating…" is the honest thing
    to show — but the crucial part is that it is not 'stalled'."""
    _insert_run(kb, "running", phase="labeling")
    with kb._pool.connection() as conn:
        conn.execute("INSERT INTO concept_run_requests (source, requested_at) "
                     "VALUES ('dashboard', now() - interval '10 minutes')")
    assert kb.consolidation_run_status()["state"] == "running"


def test_a_click_during_a_running_pass_is_queued_for_the_next_one(kb):
    """The running pass already read its inputs, so the click must survive to a
    following pass rather than being swallowed."""
    _insert_run(kb, "running", phase="labeling")
    kb.request_consolidation()
    with kb._pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM concept_run_requests "
                            "WHERE claimed_at IS NULL").fetchone()["n"] == 1


def test_unclaimed_request_with_nothing_running_goes_stalled(kb):
    with kb._pool.connection() as conn:
        conn.execute("INSERT INTO concept_run_requests (source, requested_at) "
                     "VALUES ('dashboard', now() - interval '5 minutes')")
    assert kb.consolidation_run_status()["state"] == "stalled"


def test_a_run_older_than_the_timeout_is_stalled_not_running(kb):
    _insert_run(kb, "running", phase="labeling", age_s=3600)
    assert kb.consolidation_run_status(run_timeout_s=1800)["state"] == "stalled"


def test_last_run_carries_the_stats_the_ui_shows(kb):
    _insert_run(kb, "done", phase="applying", trigger="churn",
                stats={"communities": 27, "kept": 23, "created": 1, "updated": 20,
                       "retired": 1, "discarded": 2, "label_failures": 0,
                       "embed_failures": 0, "label_error": None, "embed_error": None})
    last = kb.consolidation_run_status()["last"]
    assert last["status"] == "done" and last["trigger_reason"] == "churn"
    assert last["kept"] == 23 and last["created"] == 1 and last["retired"] == 1


def test_a_failed_run_surfaces_its_crash_reason_and_partial_progress(kb):
    """run_once's `except` stores `{**progress, "error": str(exc)}` on a failed row —
    prove the API-facing read (consolidation_run_status) actually flattens `error` onto
    `last`, and that a partial counter collected before the crash survives alongside
    it. Regression for the reason never reaching the UI."""
    _insert_run(kb, "failed", phase="labeling", trigger="manual",
                stats={"error": "apply boom", "communities": 3})
    last = kb.consolidation_run_status()["last"]
    assert last["status"] == "failed"
    assert last["error"] == "apply boom"
    assert last["communities"] == 3
