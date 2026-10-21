import os

from kb_agent.client import GatewayError
from kb_agent.config import AgentConfig
from kb_agent import pusher as pusher_mod
from kb_agent.pusher import Pusher
from kb_agent.scanner import Observed, scan_vault
from kb_agent.state import State


class FakeClient:
    def __init__(self, fail=False, reject=(), status=503, fail_calls=()):
        self.fail, self.notes, self.manifests, self.reject = fail, [], [], set(reject)
        self.status, self.fail_calls, self.calls = status, set(fail_calls), 0

    def post_notes(self, events):
        self.calls += 1
        if self.fail or self.calls in self.fail_calls:
            raise GatewayError(self.status, "down")
        self.notes.append(events)
        rejected = [{"external_id": e["external_id"], "reason": "text over 1 MiB"} for e in events if e["external_id"] in self.reject]
        return {"queued": len(events) - len(rejected), "rejected": rejected}

    def post_manifest(self, ids, generation, allow_empty=False):
        self.manifests.append((sorted(ids), generation, allow_empty)); return {}


def _obs(rel, text, ext=None):
    return Observed(rel_path=rel, external_id=ext or f"id-{rel}", text=text, sha=f"sha-{text}",
                    mtime_iso="2026-10-09T10:00:00+00:00", birthtime_iso=None)


def _pusher(tmp_path, client):
    cfg = AgentConfig(gateway="https://g", tenant="t", secret="s" * 64, vault=tmp_path)
    return Pusher(cfg, State(tmp_path / "state.sqlite"), client)


def test_diff_emits_upserts_for_new_and_changed_and_deletes_for_gone(tmp_path):
    client = FakeClient(); p = _pusher(tmp_path, client)
    first = p.diff([_obs("a.md", "v1"), _obs("b.md", "v1")])
    assert sorted(e["type"] + ":" + e["external_id"] for e in first) == ["upsert:id-a.md", "upsert:id-b.md"]
    assert p.push(first) == 2
    second = p.diff([_obs("a.md", "v2")])
    assert sorted(e["type"] + ":" + e["external_id"] for e in second) == ["delete:id-b.md", "upsert:id-a.md"]
    p.push(second)
    assert p.diff([_obs("a.md", "v2")]) == []                       # steady state


def test_a_rename_is_one_upsert_never_a_delete(tmp_path):
    client = FakeClient(); p = _pusher(tmp_path, client)
    p.push(p.diff([_obs("a.md", "v1", ext="E")]))
    moved = p.diff([_obs("moved/a.md", "v1", ext="E")])               # the stamped id moved with the file
    assert [(e["type"], e["external_id"], e["rel_path"]) for e in moved] == [("upsert", "E", "moved/a.md")]
    p.push(moved)
    assert p.state.known() == {"moved/a.md": ("E", "sha-v1")}
    assert p.diff([_obs("moved/a.md", "v1", ext="E")]) == []


def test_an_unlistable_directory_deletes_nothing_and_withholds_the_manifest(tmp_path):
    vault = tmp_path / "vault"; (vault / "Locked").mkdir(parents=True)
    (vault / "a.md").write_text("a\n"); (vault / "Locked" / "b.md").write_text("b\n")
    client = FakeClient(); p = _pusher(tmp_path, client)
    first = scan_vault(vault, [])
    p.push(p.diff(first)); p.manifest(first)
    assert len(client.manifests) == 1
    gen = p.state.get_kv("manifest_generation")
    os.chmod(vault / "Locked", 0o000)
    try:
        partial = scan_vault(vault, [])
    finally:
        os.chmod(vault / "Locked", 0o755)
    assert partial.errors
    assert p.diff(partial) == []                                       # Locked/b.md is not deleted
    assert p.manifest(partial) is None and len(client.manifests) == 1  # nothing posted
    assert p.state.get_kv("manifest_generation") == gen                # generation not advanced
    assert "Locked/b.md" in p.state.known()


def test_a_pushed_note_that_gains_a_yaml_typo_is_not_deleted(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    (vault / "a.md").write_text("a\n")
    client = FakeClient(); p = _pusher(tmp_path, client)
    p.push(p.diff(scan_vault(vault, [])))
    text = (vault / "a.md").read_text()
    (vault / "a.md").write_text(text.replace("---\n", "---\nkey: [unclosed\n", 1))
    scan = scan_vault(vault, [])
    assert list(scan) == [] and scan.errors
    assert p.diff(scan) == [] and "a.md" in p.state.known()


def test_push_buffers_offline_and_flushes_later(tmp_path):
    client = FakeClient(fail=True); p = _pusher(tmp_path, client)
    events = p.diff([_obs("a.md", "v1")])
    assert p.push(events) == 0 and len(p.state.pending_pushes()) == 1
    assert p.diff([_obs("a.md", "v1")]) == []                        # not re-emitted while buffered
    client.fail = False
    assert p.flush_pending() == 1 and p.state.pending_pushes() == [] and client.notes[-1][0]["external_id"] == "id-a.md"


def test_offline_push_makes_one_request_and_buffers_the_rest(tmp_path):
    client = FakeClient(fail=True); p = _pusher(tmp_path, client)
    events = p.diff([_obs(f"{i}.md", "v") for i in range(250)])   # three batches
    assert p.push(events) == 0
    assert client.calls == 1 and len(p.state.pending_pushes()) == 250


def test_flush_stops_at_the_first_failure(tmp_path):
    client = FakeClient(fail=True); p = _pusher(tmp_path, client)
    p.push(p.diff([_obs(f"{i}.md", "v") for i in range(250)]))
    client.fail, client.fail_calls = False, {3}                     # call 2 succeeds, call 3 fails
    assert p.flush_pending() == 100 and client.calls == 3 and len(p.state.pending_pushes()) == 150
    assert p.flush_pending() == 150 and p.state.pending_pushes() == []


def test_a_permanent_4xx_is_dropped_not_buffered(tmp_path, caplog):
    client = FakeClient(fail=True, status=422); p = _pusher(tmp_path, client)
    with caplog.at_level("ERROR", logger="kb-agent"):
        assert p.push(p.diff([_obs("a.md", "v1")])) == 0
    assert p.state.pending_pushes() == []                                   # never wedges a flush
    assert any(r.levelname == "ERROR" and "422" in r.getMessage() and "down" in r.getMessage()
               for r in caplog.records)
    assert [e["external_id"] for e in p.diff([_obs("a.md", "v1")])] == ["id-a.md"]   # not remembered


def test_a_permanent_4xx_is_not_mistaken_for_offline(tmp_path):
    client = FakeClient(fail_calls={1}, status=413); p = _pusher(tmp_path, client)
    assert p.push(p.diff([_obs(f"{i}.md", "v") for i in range(150)])) == 50   # batch 2 still goes
    assert client.calls == 2 and p.state.pending_pushes() == []


def test_transient_errors_stay_buffered(tmp_path):
    for status in (0, 401, 403, 408, 429, 500, 503):
        client = FakeClient(fail=True, status=status); p = _pusher(tmp_path / str(status), client)
        p.push(p.diff([_obs("a.md", "v1")]))
        assert len(p.state.pending_pushes()) == 1, status


def test_a_buffered_batch_that_turns_permanent_is_dropped_on_flush(tmp_path):
    client = FakeClient(fail=True); p = _pusher(tmp_path, client)
    p.push(p.diff([_obs(f"{i}.md", "v") for i in range(150)]))
    client.status, client.fail_calls, client.fail = 400, {client.calls + 1}, False
    assert p.flush_pending() == 50 and p.state.pending_pushes() == []      # dropped, flush carried on


def test_manifest_is_never_empty_by_accident(tmp_path):
    client = FakeClient(); p = _pusher(tmp_path, client)
    p.manifest([_obs("a.md", "v1")])
    assert client.manifests[-1][0] == ["id-a.md"] and client.manifests[-1][2] is False
    p.manifest([])
    assert client.manifests[-1][0] == [] and client.manifests[-1][2] is False   # server refuses it
    assert client.manifests[-1][1] > client.manifests[-2][1]                    # generation grows


def test_manifest_generation_advances_even_when_the_post_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(pusher_mod.time, "time", lambda: 1_800_000_000.0)   # one frozen second
    client = FakeClient(); p = _pusher(tmp_path, client)
    p.manifest([_obs("a.md", "v1")])
    first = client.manifests[-1][1]

    def lost(ids, generation, allow_empty=False):
        raise GatewayError(0, "reply lost")
    client.post_manifest, real = lost, client.post_manifest
    try:
        p.manifest([_obs("a.md", "v1")])
    except GatewayError:
        pass
    assert int(p.state.get_kv("manifest_generation")) == first + 1   # persisted before the post
    client.post_manifest = real
    p.manifest([_obs("a.md", "v1")])
    assert client.manifests[-1][1] == first + 2                       # the retry never reuses a number


def test_manifest_generation_is_unix_seconds_so_a_reinstall_still_outranks_the_server(
        tmp_path, monkeypatch):
    """A fresh state.sqlite (new Mac, deleted state) used to restart at 1 while the
    server's cursor sat in the thousands, so every later manifest was dropped as stale."""
    clock = [1_800_000_000.7]
    monkeypatch.setattr(pusher_mod.time, "time", lambda: clock[0])
    client = FakeClient(); p = _pusher(tmp_path, client)
    p.manifest([_obs("a.md", "v1")])
    p.manifest([_obs("a.md", "v1")])                                  # same second: stored + 1
    assert [m[1] for m in client.manifests] == [1_800_000_000, 1_800_000_001]
    clock[0] = 1_800_000_600.0                                        # reinstall, ten minutes later
    fresh = Pusher(p.cfg, State(tmp_path / "reinstalled.sqlite"), client)
    fresh.manifest([_obs("a.md", "v1")])
    assert client.manifests[-1][1] == 1_800_000_600 > 1_800_000_001
    clock[0] = 1_700_000_000.0                                        # clock stepped back
    p.manifest([_obs("a.md", "v1")])
    assert client.manifests[-1][1] == 1_800_000_002                   # still monotonic
    assert p.state.get_kv("manifest_generation") == "1800000002"
    assert p.state.get_kv("manifest_sent_at")                         # what `kb-agent status` shows


def test_rejected_notes_are_logged_and_not_resent_until_changed(tmp_path, caplog):
    client = FakeClient(reject={"id-big.md"}); p = _pusher(tmp_path, client)
    with caplog.at_level("WARNING", logger="kb-agent"):
        p.push(p.diff([_obs("big.md", "v1"), _obs("ok.md", "v1")]))
    assert any("id-big.md" in r.message and "1 MiB" in r.message for r in caplog.records)
    assert p.diff([_obs("big.md", "v1"), _obs("ok.md", "v1")]) == []         # not resent as-is
    assert [e["external_id"] for e in p.diff([_obs("big.md", "v2"), _obs("ok.md", "v1")])] == ["id-big.md"]


def test_batches_close_at_2_mib_of_text_and_a_big_note_goes_alone(tmp_path):
    mib = 1024 * 1024
    client = FakeClient(); p = _pusher(tmp_path, client)
    p.push(p.diff([_obs(f"{i}.md", str(i) * (3 * mib // 4)) for i in range(3)]))   # 0.75 MiB each
    assert [len(b) for b in client.notes] == [2, 1]
    client2 = FakeClient(); p2 = _pusher(tmp_path / "two", client2)
    small_big_small = [_obs("a.md", "a"), _obs("big.md", "b" * (3 * mib)), _obs("c.md", "c")]
    p2.push(p2.diff(small_big_small))
    assert [[e["rel_path"] for e in b] for b in client2.notes] == [["a.md"], ["big.md"], ["c.md"]]
    client2.notes.clear(); client2.fail = True
    p2.push(p2.diff(small_big_small + [_obs(f"x{i}.md", str(i) * (3 * mib // 4)) for i in range(3)]))  # buffered
    client2.fail = False
    assert p2.flush_pending() == 3 and [len(b) for b in client2.notes] == [2, 1]   # flush batches the same way


def test_batches_of_at_most_100(tmp_path):
    client = FakeClient(); p = _pusher(tmp_path, client)
    events = p.diff([_obs(f"{i}.md", "v") for i in range(250)])
    assert p.push(events) == 250 and [len(b) for b in client.notes] == [100, 100, 50]
