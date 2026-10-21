"""A tiny in-process scheduler: run each job on its own period; clock seeded per job."""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class ScheduledJob:
    name: str
    # 0 means "every heartbeat": the job runs once per scheduler heartbeat, never in a
    # tight loop (see IngestScheduler._period). Only `more` / poke() re-run it sooner.
    period: timedelta
    run: Callable[[], object]
    # Jobs in the same lane run serially on one scheduler thread; distinct lanes run
    # concurrently (see run_lanes). "fast" = cheap ingestion (pull/drain) that must
    # never queue behind LLM work ("slow" promotion sweeps, "enrich" enrichment batches).
    lane: str = "fast"
    # Backlog signal: when set, the run's return value is fed to this predicate; True
    # means the job left work behind (e.g. a full claim batch) and it comes due again
    # immediately instead of after `period` — the period is the idle poll, not a drain
    # throttle. A run that raises always backs off to the period (no hot error loop).
    more: "Callable[[object], bool] | None" = None


@dataclass
class JobStatus:
    """A heartbeat's-eye view of one job: its period and when it next comes due."""
    name: str
    period_seconds: float
    due_in_seconds: float          # <= 0 means due now (fires on this tick)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IngestScheduler:
    def __init__(self, jobs: "list[ScheduledJob]", *,
                 last_run: "Callable[[str], datetime | None] | None" = None,
                 clock: Callable[[], datetime] = _utcnow,
                 on_error: "Callable[[str, Exception], None] | None" = None,
                 on_tick: "Callable[[datetime, list[str], list[JobStatus]], None] | None"
                 = None,
                 heartbeat: float = 20.0) -> None:
        self._jobs = jobs
        # The idle wake interval, and the effective period of a period-0 job.
        self._heartbeat = heartbeat
        self._clock = clock
        self._on_error = on_error or (lambda name, exc: None)
        # Heartbeat hook: called once per loop with (now, ran_names, statuses) so a host
        # can show the tick even when nothing was due. No-op by default (tests stay quiet).
        self._on_tick = on_tick or (lambda now, ran, statuses: None)
        self._stop = threading.Event()
        # Cross-thread nudge: poke() sets this so an idle run_forever wakes mid-wait
        # instead of sleeping out its heartbeat. Cleared at the top of each loop pass,
        # BEFORE the tick, so a poke landing during a tick is never lost.
        self._wake = threading.Event()
        now = clock()
        # Seed next-due from the persisted last run (resume cadence across restarts);
        # an overdue job lands at-or-before `now` and fires once on the first tick.
        self._next: dict[str, datetime] = {}
        for job in jobs:
            seed = last_run(job.name) if last_run else None
            self._next[job.name] = (seed + self._period(job)) if seed else now

    def _period(self, job: ScheduledJob) -> timedelta:
        """A job's effective period. Period 0 means once per heartbeat: taken literally it
        rescheduled the job at `now`, next_wake() returned 0 and run_forever re-ticked with
        no wait at all — ~57 pulls/s and ~1,400 commits/s on a 4-note vault (the shipped
        sources.toml's obsidian job). Backlog draining is unaffected: `more` and poke()
        still make the job due at once."""
        if job.period > timedelta(0):
            return job.period
        return timedelta(seconds=self._heartbeat)

    def due(self, now: datetime) -> "list[ScheduledJob]":
        return [j for j in self._jobs if self._next[j.name] <= now]

    def status(self, now: datetime) -> "list[JobStatus]":
        """The scan view: every configured job, its period, and seconds until next due."""
        return [JobStatus(j.name, self._period(j).total_seconds(),
                          (self._next[j.name] - now).total_seconds())
                for j in self._jobs]

    def tick(self, now: datetime | None = None) -> "list[str]":
        now = now or self._clock()
        ran: list[str] = []
        for job in self.due(now):
            again = False
            try:
                result = job.run()
                again = job.more is not None and bool(job.more(result))
            except Exception as exc:               # one job must never break the loop
                self._on_error(job.name, exc)
            self._next[job.name] = now if again else now + self._period(job)
            ran.append(job.name)
        return ran

    def next_wake(self, now: datetime) -> float:
        if not self._next:
            return 0.0
        return max(0.0, (min(self._next.values()) - now).total_seconds())

    def poke(self, name: str) -> None:
        """Make job `name` due now and wake this lane's loop — the cross-lane nudge that
        turns "work just landed" into "claim now" instead of "claim at the next idle
        poll". Unknown names fail loud (a typo'd poke would otherwise no-op forever).
        Safe to call from any thread; poking a job that is due or mid-run is harmless
        (the run's own rescheduling wins, and its `more` signal reads current state)."""
        if name not in self._next:
            raise KeyError(name)
        self._next[name] = self._clock()
        self._wake.set()

    def run_forever(self, *, heartbeat: float | None = None) -> None:
        if heartbeat is not None:
            self._heartbeat = heartbeat
        heartbeat = self._heartbeat
        # Wake at least every `heartbeat` seconds so the tick is observable even when
        # nothing is due; wake sooner if a job comes due before then. After each tick the
        # on_tick hook reports what ran and when every job is next due.
        while not self._stop.is_set():
            self._wake.clear()
            now = self._clock()
            ran = self.tick(now)
            self._on_tick(now, ran, self.status(self._clock()))
            if not self._next:
                self._wake.wait(timeout=heartbeat)
                continue
            # Recompute the wake on a fresh clock: a run may have consumed the whole
            # period, and a job that signalled `more` is due right now — the old
            # `next_wake(now) or heartbeat` read an overdue job's 0.0s wake as falsy
            # and slept a full heartbeat between drain batches.
            wake = self.next_wake(self._clock())
            if wake > 0:
                # Waits on _wake (not _stop): a poke breaks the sleep so the poked job
                # runs now. A poke that raced the tick above left _wake set, so this
                # returns at once and the extra loop pass picks the job up.
                self._wake.wait(timeout=min(wake, heartbeat))

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()      # break any in-progress wait so shutdown is immediate


def run_lanes(jobs: "list[ScheduledJob]", *, heartbeat: float = 20.0,
              last_run: "Callable[[str], datetime | None] | None" = None,
              clock: Callable[[], datetime] = _utcnow,
              on_error: "Callable[[str, Exception], None] | None" = None,
              on_tick: "Callable[[str, datetime, list[str], list[JobStatus]], None] | None"
              = None) -> "dict[str, IngestScheduler]":
    """One scheduler THREAD per distinct job lane, so a lane of slow jobs (an enrichment
    batch that blocks for minutes) can never delay a lane of fast ones (pulls/drains).

    Same-lane jobs keep the serial, one-at-a-time semantics of a single IngestScheduler.
    `on_tick` (when given) receives the lane name ahead of the usual heartbeat args, so a
    host can label interleaved tick lines. Threads are daemons — stop a lane via its
    scheduler's .stop(); the dict is keyed by lane name.
    """
    grouped: "dict[str, list[ScheduledJob]]" = {}
    for job in jobs:
        grouped.setdefault(job.lane, []).append(job)
    lanes: "dict[str, IngestScheduler]" = {}
    for lane, lane_jobs in grouped.items():
        tick = (None if on_tick is None else
                (lambda now, ran, st, _lane=lane: on_tick(_lane, now, ran, st)))
        sched = IngestScheduler(lane_jobs, last_run=last_run, clock=clock,
                                on_error=on_error, on_tick=tick, heartbeat=heartbeat)
        lanes[lane] = sched
        threading.Thread(target=lambda s=sched: s.run_forever(heartbeat=heartbeat),
                         daemon=True, name=f"ingest-{lane}").start()
    return lanes
