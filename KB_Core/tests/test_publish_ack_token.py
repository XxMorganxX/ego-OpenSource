"""--ack-gate-changes is bound to WHAT changed, and new paths are shown.

The acknowledgement used to be the sha being published — the same value as
--expect-sha, printed verbatim by every dry run, and accepted when nothing had
changed at all. An invocation that always passed it (a scripted
`[--ack-gate-changes S]`, the tests' own helper) pre-acknowledged any future
gate change. Now:

- the token is a digest of the gate change itself (each changed gate file's
  old and new blob, the removed-pattern count, base and sha), printed by the
  dry run / `kb-publish diff` right after the diff it acknowledges — never in
  a push's REFUSED line;
- passing it when nothing in the gate changed is refused (no reflex acks);
- every run prints the exported paths added and removed since the last
  publish: a path approved with `kb-publish approve` and a
  lockfile commit used to ship with a silent "0 new".
"""
import re

from kb_core.publish import cli
from tests.publish_fakes import (PUBLIC_URL, ack, commit, gated_push, git, head, public_main,
                                 push_origin, scratch_private)

TOKEN = re.compile(r"--ack-gate-changes ([0-9a-f]{16})\b")


def _dry(root, capsys):
    capsys.readouterr()
    rc = cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True, remote=PUBLIC_URL)
    return rc, capsys.readouterr().out


def _weaken(root):
    sha = commit(root, "weaken + leak", **{
        "publish.deny.toml": '[deny]\npatterns = ["secretname"]\n',
        "src/b.py": "who = 'othername'\n"})
    push_origin(root)
    return sha


def test_the_sha_is_no_longer_an_acknowledgement(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch, deny=("secretname", "othername"))
    assert gated_push(root, gh) == 0
    first = public_main(remote)
    sha = _weaken(root)
    capsys.readouterr()
    assert gated_push(root, gh, ack_gate_changes=sha) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and not TOKEN.search(out)        # the token is not handed out
    assert public_main(remote) == first

    rc, out = _dry(root, capsys)
    assert rc == 0 and "1 deny pattern(s) removed" in out
    token = TOKEN.search(out)[1]
    assert sha not in token
    assert gated_push(root, gh, ack_gate_changes=token) == 0


def test_token_tracks_the_content_of_the_change(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh) == 0
    commit(root, "widen", **{"publish.manifest.toml": git(root, "show", "HEAD:publish.manifest.toml")
                             .replace('"src/**"', '"src/**", "extra/**"') + "\n"})
    push_origin(root)
    t1 = ack(root)
    commit(root, "widen more", **{"publish.manifest.toml": git(
        root, "show", "HEAD:publish.manifest.toml").replace('"extra/**"', '"extra/**", "x/**"')
        + "\n"})
    push_origin(root)
    t2 = ack(root)
    assert t1 and t2 and t1 != t2
    capsys.readouterr()
    assert gated_push(root, gh, ack_gate_changes=t1) == 1        # read diff #1, not #2
    assert gated_push(root, gh, ack_gate_changes=t2) == 0


def test_ack_with_nothing_to_acknowledge_refused(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh) == 0
    commit(root, "plain", **{"src/a.py": "a = 2\n"})
    push_origin(root)
    capsys.readouterr()
    assert gated_push(root, gh, ack_gate_changes="0123456789abcdef") == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "nothing in the gate changed" in out
    assert gated_push(root, gh, ack_gate_changes=None) == 0


def test_new_paths_since_the_last_publish_are_printed(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch, files={"src/old.py": "o = 1\n"})
    assert gated_push(root, gh) == 0
    git(root, "rm", "-q", "src/old.py")
    commit(root, "journal", **{"src/journal/2026-10-14-therapy.md": "# notes\n"})
    push_origin(root)
    rc, out = _dry(root, capsys)
    assert rc == 0
    assert "lockfile: 0 new" in out                                  # already approved
    assert "1 added, 1 removed" in out
    assert "+ src/journal/2026-10-14-therapy.md" in out and "- src/old.py" in out
    capsys.readouterr()
    assert gated_push(root, gh) == 0
    assert "+ src/journal/2026-10-14-therapy.md" in capsys.readouterr().out


def test_first_publish_lists_every_path(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    rc, out = _dry(root, capsys)
    assert rc == 0 and "+ src/a.py" in out and TOKEN.search(out)
    assert head(root)
