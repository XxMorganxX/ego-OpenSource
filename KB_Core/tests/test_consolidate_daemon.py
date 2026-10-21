"""Wiring tests for the churn-gated concept consolidation daemon."""
import types

import pytest


def test_daemon_passes_pinned_label_and_local_embedding_clients(monkeypatch):
    import kb_core.consolidate_daemon as daemon

    pool = object()
    kb = types.SimpleNamespace(consolidation_signal=lambda: {"probe": "stub"},
                               sweep_orphaned_concept_runs=lambda: 0,
                               claim_consolidation_requests=lambda: [],
                               release_consolidation_requests=lambda ids: None,
                               finish_consolidation_requests=lambda ids, run_id, error: None,
                               prompt=lambda name: "stub")
    label_client = object()
    embedding_client = object()
    seen = {}

    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(daemon, "open_pool", lambda dsn: pool)
    monkeypatch.setattr(daemon, "migrate_pool", lambda value: None)
    monkeypatch.setattr(daemon, "KnowledgeBase", lambda value, registry, **_kw: kb)
    monkeypatch.setattr(daemon, "default_registry", lambda: object())
    monkeypatch.setattr(
        daemon, "build_consolidation_clients", lambda env, pool=None: (label_client, embedding_client))

    def fake_run_once(got_kb, got_label, **kwargs):
        seen.update(kb=got_kb, label=got_label, embedding=kwargs["embedding_client"])
        return {"run_id": 1, "communities": 1, "kept": 1, "created": 1,
                "updated": 0, "retired": 0, "discarded": 0}

    monkeypatch.setattr(daemon, "run_once", fake_run_once)
    monkeypatch.setattr(daemon, "should_consolidate", lambda s, **kw: (True, "churn"))
    monkeypatch.setattr(daemon, "_open_listen", lambda dsn: types.SimpleNamespace(closed=False))
    monkeypatch.setattr(daemon, "_wait", lambda listen, seconds: (_ for _ in ()).throw(
        KeyboardInterrupt))

    daemon.main()

    assert seen == {"kb": kb, "label": label_client, "embedding": embedding_client}


def test_daemon_hub_share_non_positive_maps_to_none(monkeypatch):
    import kb_core.consolidate_daemon as daemon

    pool = object()
    kb = types.SimpleNamespace(consolidation_signal=lambda: {"probe": "stub"},
                               sweep_orphaned_concept_runs=lambda: 0,
                               claim_consolidation_requests=lambda: [],
                               release_consolidation_requests=lambda ids: None,
                               finish_consolidation_requests=lambda ids, run_id, error: None,
                               prompt=lambda name: "stub")
    label_client = object()
    embedding_client = object()
    seen = {}

    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("KB_CONSOLIDATE_HUB_SHARE", "0")
    monkeypatch.setattr(daemon, "open_pool", lambda dsn: pool)
    monkeypatch.setattr(daemon, "migrate_pool", lambda value: None)
    monkeypatch.setattr(daemon, "KnowledgeBase", lambda value, registry, **_kw: kb)
    monkeypatch.setattr(daemon, "default_registry", lambda: object())
    monkeypatch.setattr(
        daemon, "build_consolidation_clients", lambda env, pool=None: (label_client, embedding_client))

    def fake_run_once(got_kb, got_label, **kwargs):
        seen["hub_share"] = kwargs["hub_share"]
        return {"run_id": 1, "communities": 1, "kept": 1, "created": 1,
                "updated": 0, "retired": 0, "discarded": 0}

    monkeypatch.setattr(daemon, "run_once", fake_run_once)
    monkeypatch.setattr(daemon, "should_consolidate", lambda s, **kw: (True, "churn"))
    monkeypatch.setattr(daemon, "_open_listen", lambda dsn: types.SimpleNamespace(closed=False))
    monkeypatch.setattr(daemon, "_wait", lambda listen, seconds: (_ for _ in ()).throw(
        KeyboardInterrupt))

    daemon.main()

    assert seen["hub_share"] is None


def test_daemon_warns_with_the_reason_when_embeddings_fail(monkeypatch, capsys):
    """The label half already warns with `label_error`; the embedding half only printed
    a count, so a run's lost vectors gave no clue why. Both halves warn alike now —
    an unembedded concept is unreachable by the label-cosine retrieval arm."""
    import kb_core.consolidate_daemon as daemon

    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(daemon, "open_pool", lambda dsn: object())
    monkeypatch.setattr(daemon, "migrate_pool", lambda value: None)
    monkeypatch.setattr(daemon, "KnowledgeBase", lambda value, registry, **_kw:
                        types.SimpleNamespace(consolidation_signal=lambda: {"probe": "stub"},
                                               sweep_orphaned_concept_runs=lambda: 0,
                                               claim_consolidation_requests=lambda: [],
                                               release_consolidation_requests=lambda ids: None,
                                               finish_consolidation_requests=lambda ids, run_id, error: None,
                                               prompt=lambda name: "stub"))
    monkeypatch.setattr(daemon, "default_registry", lambda: object())
    monkeypatch.setattr(daemon, "build_consolidation_clients",
                        lambda env, pool=None: (object(), object()))
    monkeypatch.setattr(daemon, "run_once", lambda *a, **k: {
        "run_id": 1, "communities": 1, "kept": 21, "created": 0, "updated": 21,
        "retired": 0, "discarded": 0, "embed_failures": 21,
        "embed_error": "HTTP 404 from http://127.0.0.1:8000/embed"})
    monkeypatch.setattr(daemon, "should_consolidate", lambda s, **kw: (True, "churn"))
    monkeypatch.setattr(daemon, "_open_listen", lambda dsn: types.SimpleNamespace(closed=False))
    monkeypatch.setattr(daemon, "_wait", lambda listen, seconds: (_ for _ in ()).throw(
        KeyboardInterrupt))

    daemon.main()

    out = capsys.readouterr().out
    assert "embed_failures=21" in out
    assert "WARNING" in out and "404" in out


def _wire(monkeypatch, daemon, decisions, runs, run_raises=None):
    """Shared harness: stub the world, feed a scripted decision sequence, record runs.
    The loop exits via KeyboardInterrupt from sleep once the script is exhausted (a
    max-iteration guard trips an AssertionError first if a regression makes it run away
    instead — a hang becomes a red test, not a stuck process).

    Returns `(calls, sleeps)`: `calls` is the list of `should_consolidate` invocations
    (each a dict of the `signal` positional arg merged with its kwargs, so a test can
    check e.g. `quiet_s`/`backstop_s`); `sleeps` is the list of `time.sleep` delays.

    `run_raises` (optional) controls whether a given fired `run_once` call raises: a
    callable is invoked with the 0-based call index and treated as truthy/falsy; a
    sequence is indexed the same way (an out-of-range index defaults to False, i.e. no
    raise).
    """
    kb = types.SimpleNamespace(consolidation_signal=lambda: {"probe": "stub"},
                               sweep_orphaned_concept_runs=lambda: 0,
                               claim_consolidation_requests=lambda: [],
                               release_consolidation_requests=lambda ids: None,
                               finish_consolidation_requests=lambda ids, run_id, error: None,
                               prompt=lambda name: "stub")
    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(daemon, "open_pool", lambda dsn: object())
    monkeypatch.setattr(daemon, "migrate_pool", lambda value: None)
    monkeypatch.setattr(daemon, "KnowledgeBase", lambda value, registry, **_kw: kb)
    monkeypatch.setattr(daemon, "default_registry", lambda: object())
    monkeypatch.setattr(daemon, "build_consolidation_clients",
                        lambda env, pool=None: (object(), object()))

    calls = []

    def fake_should_consolidate(signal, **kwargs):
        calls.append({"signal": signal, **kwargs})
        return decisions.pop(0)

    monkeypatch.setattr(daemon, "should_consolidate", fake_should_consolidate)

    def _run_should_raise(index):
        if run_raises is None:
            return False
        if callable(run_raises):
            return bool(run_raises(index))
        try:
            return bool(run_raises[index])
        except IndexError:
            return False

    def fake_run_once(*args, **kwargs):
        index = len(runs)
        runs.append(1)
        if _run_should_raise(index):
            raise RuntimeError("run_once boom")
        return {"run_id": 1, "communities": 1, "kept": 1, "created": 1,
                "updated": 0, "retired": 0, "discarded": 0}

    monkeypatch.setattr(daemon, "run_once", fake_run_once)

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 20:
            raise AssertionError("loop overran script")
        if not decisions:
            raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "_open_listen", lambda dsn: types.SimpleNamespace(closed=False))
    monkeypatch.setattr(daemon, "_wait", lambda listen, seconds: fake_sleep(seconds))

    return calls, sleeps


def test_daemon_startup_does_not_run_unconditionally(monkeypatch):
    """A restart mid-enrichment-drain must NOT persist a partial roster: the old
    run-once-on-start behavior is gone; the gate decides from poll one."""
    import kb_core.consolidate_daemon as daemon

    runs = []
    _wire(monkeypatch, daemon, [(False, "backlog")], runs)
    daemon.main()
    assert runs == []


def test_daemon_fires_once_gate_opens(monkeypatch):
    import kb_core.consolidate_daemon as daemon

    runs = []
    _wire(monkeypatch, daemon,
          [(False, "backlog"), (False, "not-quiet"), (True, "churn")], runs)
    daemon.main()
    assert runs == [1]


def test_daemon_logs_state_transitions_not_every_poll(monkeypatch, capsys):
    import kb_core.consolidate_daemon as daemon

    runs = []
    _wire(monkeypatch, daemon,
          [(False, "backlog"), (False, "backlog"), (True, "churn")], runs)
    daemon.main()
    out = capsys.readouterr().out
    assert out.count("state: backlog") == 1     # two identical polls, one line
    assert out.count("state: churn") == 1


def test_daemon_resets_transition_log_after_fired_run(monkeypatch, capsys):
    """`last_reason` is reset to None right after a fired run, so an identical hold
    reason immediately afterward logs afresh instead of staying silent forever."""
    import kb_core.consolidate_daemon as daemon

    runs = []
    _wire(monkeypatch, daemon,
          [(False, "backlog"), (True, "churn"), (False, "backlog")], runs)
    daemon.main()
    out = capsys.readouterr().out
    assert out.count("state: backlog") == 2
    assert out.count("state: churn") == 1


def test_daemon_should_consolidate_receives_configured_thresholds_and_signal(monkeypatch):
    """The loop must probe first and pass that exact signal, plus the quiet/backstop
    knobs (their env defaults here), straight through to should_consolidate."""
    import kb_core.consolidate_daemon as daemon

    runs = []
    calls, _sleeps = _wire(monkeypatch, daemon, [(False, "backlog")], runs)
    daemon.main()
    assert calls == [{"signal": {"probe": "stub"}, "quiet_s": 180.0, "backstop_s": 86400.0}]


def test_daemon_sleeps_poll_not_interval(monkeypatch):
    """The old loop slept KB_CONSOLIDATE_INTERVAL (daily); the gated loop must sleep
    the poll cadence instead — a regression here would silently reintroduce the
    once-a-day probe cadence."""
    import kb_core.consolidate_daemon as daemon

    monkeypatch.setenv("KB_CONSOLIDATE_POLL", "60")
    runs = []
    _calls, sleeps = _wire(monkeypatch, daemon, [(False, "backlog")], runs)
    daemon.main()
    assert sleeps == [60.0]
    assert 86400.0 not in sleeps


def test_daemon_backs_off_exponentially_on_failing_runs(monkeypatch, capsys):
    """A pass that keeps raising (probe/decide/fire — here, the fired run itself)
    must back off on an exponential schedule (2x, 4x, 8x poll, capped at 3600s)
    instead of hammering every poll."""
    import kb_core.consolidate_daemon as daemon

    runs = []
    decisions = [(True, "churn"), (True, "churn"), (True, "churn")]
    _calls, sleeps = _wire(monkeypatch, daemon, decisions, runs,
                           run_raises=lambda index: True)
    daemon.main()
    assert sleeps == [120.0, 240.0, 480.0]
    out = capsys.readouterr().out
    assert "retrying in 120s" in out
    assert "retrying in 240s" in out
    assert "retrying in 480s" in out


def test_daemon_backoff_resets_after_successful_pass(monkeypatch):
    """Any successful pass — fired or held — resets the failure streak, so the very
    next failure after a success starts the backoff over at 2x poll, not where the
    previous streak left off."""
    import kb_core.consolidate_daemon as daemon

    runs = []
    decisions = [(True, "churn"), (True, "churn"), (True, "churn")]
    _calls, sleeps = _wire(monkeypatch, daemon, decisions, runs,
                           run_raises=[True, False, True])
    daemon.main()
    assert sleeps == [120.0, 60.0, 120.0]


def test_daemon_backoff_caps_at_one_hour(monkeypatch):
    """8 consecutive failing fires must not overflow: the streak has to be clamped
    before `2 ** fail_streak` is computed, or a long enough outage eventually raises
    OverflowError inside the except handler itself and kills the daemon. With the
    clamp (streak capped at 6), the delay sequence is 120/240/480/960/1920/3600 and
    then holds at 3600 forever — never growing further."""
    import kb_core.consolidate_daemon as daemon

    runs = []
    decisions = [(True, "churn")] * 8
    _calls, sleeps = _wire(monkeypatch, daemon, decisions, runs,
                           run_raises=lambda index: True)
    daemon.main()
    assert sleeps == [120.0, 240.0, 480.0, 960.0, 1920.0, 3600.0, 3600.0, 3600.0]


def test_daemon_relogs_state_after_failed_pass(monkeypatch, capsys):
    """A failure must not freeze the transition log: if the daemon fails mid-hold and
    then recovers to the SAME reason, the state line has to print again so the log
    actually confirms recovery instead of staying silent forever. The interrupting
    fire must carry the identical "backlog" reason (not a different one) — otherwise
    the ordinary reason-changed branch would mask the bug on its own."""
    import kb_core.consolidate_daemon as daemon

    runs = []
    decisions = [(False, "backlog"), (True, "backlog"), (False, "backlog")]
    _wire(monkeypatch, daemon, decisions, runs, run_raises=[True])
    daemon.main()
    out = capsys.readouterr().out
    assert out.count("state: backlog") == 2


def _wire_requests(monkeypatch, daemon, claims, runs):
    """Like _wire, but scripts claim_consolidation_requests instead of the churn gate:
    `claims` is a list of id-lists, one per poll. should_consolidate always holds, so any
    run that happens was forced by a request."""
    finished = []
    released = []
    kb = types.SimpleNamespace(
        consolidation_signal=lambda: {"probe": "stub"},
        sweep_orphaned_concept_runs=lambda: 0,
        claim_consolidation_requests=lambda: claims.pop(0) if claims else [],
        release_consolidation_requests=lambda ids: released.append(ids),
        finish_consolidation_requests=lambda ids, run_id, error: finished.append(
            (ids, run_id, error)),
        prompt=lambda name: "stub")
    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(daemon, "open_pool", lambda dsn: object())
    monkeypatch.setattr(daemon, "migrate_pool", lambda value: None)
    monkeypatch.setattr(daemon, "KnowledgeBase", lambda value, registry, **_kw: kb)
    monkeypatch.setattr(daemon, "default_registry", lambda: object())
    monkeypatch.setattr(daemon, "build_consolidation_clients",
                        lambda env, pool=None: (object(), object()))
    monkeypatch.setattr(daemon, "should_consolidate", lambda s, **kw: (False, "no-change"))
    monkeypatch.setattr(daemon, "_open_listen", lambda dsn: types.SimpleNamespace(closed=False))

    def fake_run_once(*args, **kwargs):
        runs.append(kwargs.get("trigger_reason"))
        return {"run_id": 77, "communities": 1, "kept": 1, "created": 1,
                "updated": 0, "retired": 0, "discarded": 0}

    monkeypatch.setattr(daemon, "run_once", fake_run_once)

    waits = []

    def fake_wait(listen, seconds):
        waits.append(seconds)
        if len(waits) > 20:
            raise AssertionError("loop overran script")
        if not claims:
            raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "_wait", fake_wait)
    return finished, released, waits


def test_a_pending_request_forces_a_pass_the_churn_gate_would_hold(monkeypatch):
    """The whole point of the button: should_consolidate says no-change, and the pass
    runs anyway."""
    import kb_core.consolidate_daemon as daemon

    runs = []
    finished, _released, _waits = _wire_requests(monkeypatch, daemon, [[1, 2]], runs)
    daemon.main()
    assert runs == ["manual"]
    assert finished == [([1, 2], 77, None)]


def test_no_request_falls_through_to_the_churn_gate(monkeypatch):
    import kb_core.consolidate_daemon as daemon

    runs = []
    finished, _released, _waits = _wire_requests(monkeypatch, daemon, [[]], runs)
    daemon.main()
    assert runs == [] and finished == []


def test_lock_contention_releases_the_claim_instead_of_failing_it(monkeypatch, capsys):
    """A human's `kb-consolidate --apply` holds the lock: the click must survive to the
    next poll rather than being marked served."""
    import kb_core.consolidate_daemon as daemon
    from kb_core.knowledge_base import ConsolidationBusy

    runs = []
    finished, released, _waits = _wire_requests(monkeypatch, daemon, [[5]], runs)
    monkeypatch.setattr(daemon, "run_once", lambda *a, **k: (_ for _ in ()).throw(
        ConsolidationBusy("another consolidation pass is running")))
    daemon.main()
    assert released == [[5]]
    assert finished == []
    assert "another consolidation pass" in capsys.readouterr().out


def test_a_paused_label_provider_holds_the_pass_and_keeps_the_click(monkeypatch, capsys):
    """The pause row is shared: a pause the ingest lane recorded for `agent` must hold
    consolidation too, without calling the labeler, and the click must survive."""
    from datetime import datetime, timedelta, timezone

    import kb_core.consolidate_daemon as daemon

    runs = []
    finished, released, waits = _wire_requests(monkeypatch, daemon, [[4]], runs)
    until = datetime.now(timezone.utc) + timedelta(minutes=10)
    wired = daemon.KnowledgeBase

    def kb_with_pause(value, registry, **_kw):
        kb = wired(value, registry)
        kb.provider_paused = lambda prov: until if prov == "agent" else None
        return kb

    monkeypatch.setattr(daemon, "KnowledgeBase", kb_with_pause)
    monkeypatch.setattr(daemon, "build_consolidation_clients", lambda env, pool=None: (
        types.SimpleNamespace(provider="agent"), object()))
    daemon.main()
    assert runs == []                                  # labeler never reached
    assert released == [[4]] and finished == []
    assert 590 < waits[0] <= 600
    assert "agent paused" in capsys.readouterr().out


def test_an_offline_agent_releases_the_claim_and_waits_out_the_pause(monkeypatch, capsys):
    """Tenant consolidation with the kb-agent offline: ProviderUnavailable is not a failed
    pass. The click goes back (like lock contention), no fail streak is started, and the
    next attempt waits for the provider's pause window instead of the poll interval."""
    from datetime import datetime, timedelta, timezone

    import kb_core.consolidate_daemon as daemon
    from kb_core.ingest.inference import ProviderUnavailable

    runs = []
    finished, released, waits = _wire_requests(monkeypatch, daemon, [[6]], runs)
    until = datetime.now(timezone.utc) + timedelta(minutes=10)
    monkeypatch.setattr(daemon, "run_once", lambda *a, **k: (_ for _ in ()).throw(
        ProviderUnavailable("no agent answered", provider="agent", until=until)))
    daemon.main()
    assert released == [[6]]
    assert finished == []
    assert 590 < waits[0] <= 600                     # the pause window, not poll (60s)
    out = capsys.readouterr().out
    assert "agent unavailable" in out and "pass failed" not in out


def test_a_failed_forced_pass_reports_the_error_on_the_request(monkeypatch):
    import kb_core.consolidate_daemon as daemon

    runs = []
    finished, released, _waits = _wire_requests(monkeypatch, daemon, [[9]], runs)
    monkeypatch.setattr(daemon, "run_once", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("pass boom")))
    daemon.main()
    assert released == []
    assert len(finished) == 1
    ids, run_id, error = finished[0]
    assert ids == [9] and run_id is None and "pass boom" in error


def test_startup_sweeps_orphaned_running_rows(monkeypatch, capsys):
    import kb_core.consolidate_daemon as daemon

    swept = []
    runs = []
    claims = [[]]
    _finished, _released, _waits = _wire_requests(monkeypatch, daemon, claims, runs)
    monkeypatch.setattr(daemon, "KnowledgeBase", lambda value, registry, **_kw:
                        types.SimpleNamespace(
                            consolidation_signal=lambda: {"probe": "stub"},
                            sweep_orphaned_concept_runs=lambda: swept.append(1) or 2,
                            claim_consolidation_requests=lambda: claims.pop(0) if claims else [],
                            release_consolidation_requests=lambda ids: None,
                            finish_consolidation_requests=lambda i, r, e: None,
                            prompt=lambda name: "stub"))
    daemon.main()
    assert swept == [1]
    assert "swept 2" in capsys.readouterr().out


def test_preflight_prompts_raises_on_broken_concept_judge_overlay(pool, tmp_path):
    """A broken overlay must fail loud at daemon start (preflight_prompts, called from
    main() right after KnowledgeBase is constructed), not wait for the churn gate to
    fire hours or days later."""
    from kb_core.consolidate_daemon import preflight_prompts
    from kb_core.knowledge_base import KnowledgeBase
    from kb_core.policy import Policy, PolicyError, Prompts
    from kb_core.registry import default_registry

    (tmp_path / "concept_judge.system.md").write_text("{{include:missing.md}}")
    broken_kb = KnowledgeBase(pool, default_registry(),
                              policy=Policy(prompts=Prompts(str(tmp_path))))
    with pytest.raises(PolicyError, match="missing.md"):
        preflight_prompts(broken_kb)


def test_preflight_prompts_does_not_raise_with_the_legacy_kb_fixture(kb):
    from kb_core.consolidate_daemon import preflight_prompts

    preflight_prompts(kb)          # shipped concept_judge.system.md loads fine
