"""Turn a scan into gateway events; buffer them locally while offline."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from kb_agent.client import GatewayError
from kb_agent.config import AgentConfig
from kb_agent.scanner import Observed
from kb_agent.state import State

log = logging.getLogger("kb-agent")
BATCH = 100
# The gateway refuses bodies over 8 MiB. The client encodes with ensure_ascii=False, so UTF-8
# text goes over the wire at its byte size; only quotes, backslashes and newlines/tabs grow (2x)
# and other control characters grow up to 6x (\u0000). A quarter of the limit covers ordinary
# text with room to spare; a batch of pathological control-character text that still exceeds
# it is refused with 413 and dropped (PERMANENT), never wedged in the buffer.
BATCH_TEXT_BYTES = 2 * 1024 * 1024
# Statuses a resend of the same batch cannot fix. Everything else (0 = network, 401, 403, 408,
# 429, every 5xx) is transient and stays buffered.
PERMANENT = frozenset({400, 404, 413, 422})


def _batches(items: list, event=lambda x: x):
    """Split into batches of at most BATCH items and BATCH_TEXT_BYTES of note text. A single
    note above the byte cap still goes, alone."""
    batch: list = []
    size = 0
    for item in items:
        n = len((event(item).get("text") or "").encode("utf-8"))
        if batch and (len(batch) >= BATCH or size + n > BATCH_TEXT_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(item); size += n
    if batch:
        yield batch


class Pusher:
    def __init__(self, cfg: AgentConfig, state: State, client) -> None:
        self.cfg, self.state, self.client = cfg, state, client

    def diff(self, observed: list[Observed]) -> list[dict]:
        known = self.state.known()
        buffered = {p["external_id"] for _, _, p in self.state.pending_pushes()}
        events: list[dict] = []
        upserts: dict[str, dict] = {}
        seen = {o.rel_path for o in observed}
        live_ids = {o.external_id for o in observed}
        for o in observed:
            prev = known.get(o.rel_path)
            if (prev is None or prev[1] != o.sha) and o.external_id not in buffered:
                e = {"type": "upsert", "external_id": o.external_id, "rel_path": o.rel_path,
                     "text": o.text, "mtime": o.mtime_iso, "birthtime": o.birthtime_iso,
                     "_rel": o.rel_path, "_sha": o.sha}
                events.append(e); upserts[o.external_id] = e
        failed = getattr(observed, "failed", None)            # a Scan knows what it could not see
        for rel, (ext, _sha) in known.items():
            if rel in seen or ext in buffered:
                continue
            if failed is not None and failed(rel):
                continue                                        # unseen is not gone: keep its state
            if ext in live_ids:
                # A rename: the stamped id moved with the file, and the upsert already carries
                # the new path. A delete here would land last and remove the note server-side.
                if ext in upserts:
                    upserts[ext].setdefault("_moved_from", []).append(rel)
                continue
            events.append({"type": "delete", "external_id": ext, "_rel": rel})
        return events

    def _send(self, batch: list[dict]) -> None:
        wire = [{k: v for k, v in e.items() if not k.startswith("_")} for e in batch]
        reply = self.client.post_notes(wire) or {}
        for r in reply.get("rejected") or []:
            # Remembered below like the rest, so it is retried only when the file changes.
            log.warning("gateway rejected %s: %s", r.get("external_id"), r.get("reason"))
        for e in batch:
            if e["type"] == "upsert":
                self.state.remember(e["_rel"], e["external_id"], e["_sha"])
                for old in e.get("_moved_from", ()):
                    self.state.forget(old)
            else:
                self.state.forget(e["_rel"])

    @staticmethod
    def _permanent(exc: GatewayError, n: int) -> bool:
        """A refusal that retrying the same bytes cannot fix. The batch is dropped, not buffered
        (a buffered one would wedge every later flush) and not remembered, so the next diff
        re-emits it and the failure stays visible once per cycle."""
        if exc.status not in PERMANENT:
            return False
        log.error("gateway refused a batch of %d event(s) with %d (%s); dropped, retried next scan",
                  n, exc.status, exc.detail)
        return True

    def push(self, events: list[dict]) -> int:
        sent, offline = 0, False
        for batch in _batches(events):
            if not offline:
                try:
                    self._send(batch)
                    sent += len(batch)
                    continue
                except GatewayError as exc:
                    if self._permanent(exc, len(batch)):
                        continue
                    offline = True          # one timeout is enough: buffer the rest unattempted
            for e in batch:
                self.state.queue_push(e["type"], e)
        return sent

    def flush_pending(self) -> int:
        pending = self.state.pending_pushes()
        sent = 0
        for chunk in _batches(pending, event=lambda p: p[2]):
            try:
                self._send([p for _, _, p in chunk])
                sent += len(chunk)
            except GatewayError as exc:
                if not self._permanent(exc, len(chunk)):
                    break
            for pid, _, _ in chunk:
                self.state.drop_push(pid)
        return sent

    def manifest(self, observed: list[Observed]) -> dict | None:
        """Post the full id list; withheld (None, nothing posted, generation untouched) when the
        scan reported any error, because a partial list would prune notes server-side."""
        if getattr(observed, "errors", None):
            log.warning("manifest withheld: the scan reported %d error(s)", len(observed.errors))
            return None
        # Persist before posting: if the reply is lost, the retry still uses a fresh number
        # (the server rejects a repeated generation as stale; gaps are fine). Unix seconds,
        # never below stored + 1: a reinstalled agent (fresh state.sqlite) still outranks the
        # server's last applied generation, and a clock stepped back never repeats one.
        stored = int(self.state.get_kv("manifest_generation") or 0)
        gen = max(stored + 1, int(time.time()))
        self.state.set_kv("manifest_generation", str(gen))
        self.state.set_kv("manifest_sent_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return self.client.post_manifest([o.external_id for o in observed], gen, allow_empty=False) or {}
