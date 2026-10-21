from datetime import datetime, timedelta, timezone

from kb_core.ingest.scheduler import IngestScheduler, ScheduledJob

T0 = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)


def _job(name, secs, calls):
    return ScheduledJob(name, timedelta(seconds=secs), lambda: calls.append(name))


def test_runs_due_jobs_and_reschedules():
    calls = []
    s = IngestScheduler([_job("a", 60, calls), _job("b", 300, calls)],
                        clock=lambda: T0)
    assert sorted(s.tick(T0)) == ["a", "b"]                  # first tick: both due (seed=now)
    assert s.tick(T0 + timedelta(seconds=61)) == ["a"]       # only a's period elapsed
    assert calls == ["a", "b", "a"]


def test_overdue_at_boot_runs_once():
    calls = []
    # b last ran 1h ago with a 5m period -> overdue; should fire once on the first tick.
    last = {"b": T0 - timedelta(hours=1)}
    s = IngestScheduler([_job("b", 300, calls)], clock=lambda: T0,
                        last_run=lambda name: last.get(name))
    assert s.tick(T0) == ["b"]
    assert s.tick(T0) == []                                  # not repeatedly
    assert calls == ["b"]


def test_next_wake_is_soonest_due():
    s = IngestScheduler([_job("a", 60, []), _job("b", 300, [])], clock=lambda: T0)
    s.tick(T0)
    assert s.next_wake(T0) == 60.0                           # a is due first


def test_status_reports_period_and_due_in():
    s = IngestScheduler([_job("a", 60, []), _job("b", 300, [])], clock=lambda: T0)
    s.tick(T0)                                                  # both fire -> reschedule
    st = {x.name: x for x in s.status(T0)}
    assert st["a"].period_seconds == 60 and st["a"].due_in_seconds == 60
    assert st["b"].period_seconds == 300 and st["b"].due_in_seconds == 300


def test_run_forever_emits_heartbeat_and_can_stop():
    seen = []
    s = IngestScheduler(
        [_job("a", 60, [])], clock=lambda: T0,
        on_tick=lambda now, ran, statuses: (seen.append((ran, [x.name for x in statuses])),
                                            s.stop()))
    s.run_forever(heartbeat=0.01)                               # on_tick stops it after tick 1
    assert seen == [(["a"], ["a"])]                             # first tick ran 'a', scanned 'a'


def test_job_error_does_not_break_the_loop():
    ran = []
    def boom(): raise RuntimeError("x")
    s = IngestScheduler(
        [ScheduledJob("bad", timedelta(seconds=60), boom),
         ScheduledJob("good", timedelta(seconds=60), lambda: ran.append("good"))],
        clock=lambda: T0, on_error=lambda n, e: ran.append(f"err:{n}"))
    s.tick(T0)
    assert ran == ["err:bad", "good"]


def test_run_lanes_starts_one_scheduler_per_lane_and_slow_cannot_block_fast():
    import threading
    import time

    from kb_core.ingest.scheduler import run_lanes

    entered = threading.Event()      # slow job signals it is now occupying its lane
    unblock = threading.Event()      # test releases the slow job at the end
    fast_runs = []

    def slow_run():
        entered.set()
        unblock.wait(5)

    jobs = [ScheduledJob("slow", timedelta(seconds=0), slow_run, lane="slow"),
            ScheduledJob("fast", timedelta(seconds=0), lambda: fast_runs.append(1))]
    lanes = run_lanes(jobs, heartbeat=0.01)
    try:
        assert set(lanes) == {"fast", "slow"}
        assert entered.wait(2)                      # slow lane is now parked in slow_run
        deadline = time.monotonic() + 2
        while len(fast_runs) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        # The whole point of lanes: fast work keeps flowing while slow work blocks.
        assert len(fast_runs) >= 3
    finally:
        unblock.set()
        for s in lanes.values():
            s.stop()


def test_scheduled_job_lane_defaults_to_fast():
    assert _job("a", 60, []).lane == "fast"


def test_more_signal_makes_job_due_again_immediately():
    # A full claim batch means backlog remains: the job must come due again at once,
    # not after its period — the period is the idle poll, not a drain throttle.
    runs = []
    j = ScheduledJob("drain", timedelta(seconds=120),
                     lambda: (runs.append(1), 10)[1], more=lambda n: n >= 10)
    s = IngestScheduler([j], clock=lambda: T0)
    assert s.tick(T0) == ["drain"]
    assert s.tick(T0 + timedelta(seconds=1)) == ["drain"]    # not parked for 120s
    assert len(runs) == 2


def test_more_signal_false_backs_off_to_period():
    j = ScheduledJob("drain", timedelta(seconds=120), lambda: 3, more=lambda n: n >= 10)
    s = IngestScheduler([j], clock=lambda: T0)
    s.tick(T0)
    assert s.tick(T0 + timedelta(seconds=1)) == []           # partial batch -> idle poll
    assert s.tick(T0 + timedelta(seconds=121)) == ["drain"]


def test_more_job_error_backs_off_to_period():
    # A failing job must never hot-loop, no matter what `more` would say.
    def boom(): raise RuntimeError("x")
    errs = []
    j = ScheduledJob("drain", timedelta(seconds=120), boom, more=lambda n: True)
    s = IngestScheduler([j], clock=lambda: T0, on_error=lambda n, e: errs.append(n))
    s.tick(T0)
    assert s.tick(T0 + timedelta(seconds=1)) == []
    assert errs == ["drain"]


def test_poke_makes_job_due_immediately():
    calls = []
    s = IngestScheduler([_job("enrichment", 300, calls)], clock=lambda: T0)
    s.tick(T0)                                               # ran; parked for 300s
    assert s.tick(T0 + timedelta(seconds=1)) == []
    s.poke("enrichment")
    assert s.tick(T0 + timedelta(seconds=2)) == ["enrichment"]


def test_poke_unknown_job_fails_loud():
    import pytest

    s = IngestScheduler([_job("enrichment", 300, [])], clock=lambda: T0)
    with pytest.raises(KeyError):
        s.poke("typo")


def test_poke_wakes_a_sleeping_run_forever_promptly():
    # An idle lane parked on its wait must wake and run a poked job at once — the poke
    # is what turns "note just landed" into "claim now" instead of "claim at the next
    # idle poll". heartbeat=30 would fail this test if the poke relied on the tick.
    import threading
    import time

    runs = []
    holder = {}

    def run():
        runs.append(1)
        if len(runs) >= 2:
            holder["s"].stop()

    j = ScheduledJob("enrichment", timedelta(seconds=3600), run)
    s = holder["s"] = IngestScheduler([j])                   # real clock
    t = threading.Thread(target=lambda: s.run_forever(heartbeat=30.0), daemon=True)
    t.start()
    deadline = time.monotonic() + 2
    while not runs and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runs                                              # boot run done; lane now idle
    t0 = time.monotonic()
    s.poke("enrichment")
    t.join(5)
    assert len(runs) == 2
    assert time.monotonic() - t0 < 2                         # woke on the poke, not a sleep


def test_run_forever_reruns_more_job_without_heartbeat_wait():
    # The rerun must be immediate: run_forever may not sleep a heartbeat between the
    # batches of a drain (the old wait treated an overdue job's 0s wake as falsy and
    # slept the full heartbeat).
    import time

    runs = []
    holder = {}

    def run():
        runs.append(1)
        if len(runs) >= 3:
            holder["s"].stop()
        return 10

    j = ScheduledJob("drain", timedelta(seconds=3600), run, more=lambda n: n >= 10)
    s = holder["s"] = IngestScheduler([j])                   # real clock
    t0 = time.monotonic()
    s.run_forever(heartbeat=1.0)
    assert len(runs) == 3
    assert time.monotonic() - t0 < 0.5                       # no heartbeat sleeps between reruns


# --- period 0 = once per heartbeat, never a spin ---------------------------------------

def test_period_zero_job_comes_due_once_per_heartbeat():
    calls = []
    s = IngestScheduler([_job("obsidian", 0, calls)], clock=lambda: T0, heartbeat=20.0)
    assert s.tick(T0) == ["obsidian"]
    assert s.tick(T0) == []                                   # not again on the same instant
    assert s.next_wake(T0) == 20.0                            # was 0.0: run_forever spun
    assert s.tick(T0 + timedelta(seconds=19)) == []
    assert s.tick(T0 + timedelta(seconds=20)) == ["obsidian"]
    assert {x.name: x.period_seconds for x in s.status(T0)} == {"obsidian": 20.0}


def test_period_zero_job_with_backlog_still_reruns_at_once():
    """`more` keeps meaning 'due again now' for a period-0 job: draining is not throttled."""
    backlog = [3]
    def run():
        backlog[0] -= 1
        return backlog[0]
    s = IngestScheduler([ScheduledJob("drain", timedelta(0), run, more=lambda n: n > 0)],
                        clock=lambda: T0, heartbeat=20.0)
    assert s.tick(T0) == ["drain"] and s.next_wake(T0) == 0.0
    s.tick(T0)
    s.tick(T0)                                                # backlog now empty
    assert backlog[0] == 0 and s.next_wake(T0) == 20.0


def test_run_forever_does_not_spin_a_period_zero_job():
    """Real clock, real loop: over ~1 s with a 0.25 s heartbeat a period-0 job runs about
    four or five times — the old loop managed thousands."""
    import threading
    import time

    calls = []
    s = IngestScheduler([_job("obsidian", 0, calls)], heartbeat=0.25)
    t = threading.Thread(target=s.run_forever, daemon=True)
    t.start()
    time.sleep(1.0)
    s.stop()
    t.join(2)
    assert 2 <= len(calls) <= 6, len(calls)


def test_poke_still_wakes_an_idle_period_zero_job():
    import threading
    import time

    calls = []
    s = IngestScheduler([_job("obsidian", 0, calls)], heartbeat=30.0)
    t = threading.Thread(target=s.run_forever, daemon=True)
    t.start()
    time.sleep(0.2)
    assert len(calls) == 1                                    # boot tick, then a 30 s wait
    s.poke("obsidian")
    time.sleep(0.2)
    s.stop()
    t.join(2)
    assert len(calls) == 2


def test_run_lanes_passes_the_heartbeat_to_period_zero_jobs():
    import time

    from kb_core.ingest.scheduler import run_lanes

    calls = []
    lanes = run_lanes([_job("obsidian", 0, calls)], heartbeat=0.25)
    time.sleep(1.0)
    for sched in lanes.values():
        sched.stop()
    assert 2 <= len(calls) <= 6, len(calls)
