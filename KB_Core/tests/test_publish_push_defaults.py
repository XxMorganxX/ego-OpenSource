"""Publish defaults: the push URL comes from the pin, the identity never from
git config.

- `kb-publish --push` without --remote refused even though `kb-publish pin`
  already names the one repo it may write; it now pushes to
  ssh://git@github.com/<pinned>.git (the deploy-key transport).
- The identity fell back to `git config user.*` — the operator's real name and
  address, both denylisted — and the refusal then suggested
  --allow-identity-hit, which writes them into immutable public history and
  pins them for every later publish. The fallback is now the neutral mirror
  identity, an explicit KB_PUBLISH_IDENTITY_EMAIL must be a reserved / noreply
  address (not overridable), and the refusal no longer offers the override.
- A dry run that did not read public HEAD says so instead of a confident
  "first publish" verdict.
"""
from kb_core.publish import cli, state
from tests.publish_fakes import (MIRROR, PUBLIC, ack, gated, gated_push, git, public_main,
                                 scratch_private)


def test_push_without_remote_goes_to_the_pinned_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("KB_PUBLISH_REMOTE", raising=False)
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh, remote=None) == 0
    out = capsys.readouterr().out
    assert f"ssh://git@github.com/{PUBLIC}.git (source: pinned public repo)" in out
    assert public_main(remote)


def test_no_remote_and_no_pin_still_refuses_the_placeholder(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("KB_PUBLISH_REMOTE", raising=False)
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    st = state.load(root)
    st.update(public_repo_id=None, public_repo_full_name=None)
    state.save(root, st)
    assert gated_push(root, gh, remote=None) == 1
    assert "placeholder" in capsys.readouterr().out and public_main(remote) is None


def test_identity_never_falls_back_to_git_config(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    monkeypatch.delenv("KB_PUBLISH_IDENTITY_NAME")
    monkeypatch.delenv("KB_PUBLISH_IDENTITY_EMAIL")
    git(root, "config", "user.name", "secretname Person")
    git(root, "config", "user.email", "person" + "@" + "realmail.com")
    assert gated_push(root, gh) == 0
    author = git(remote, "log", "-1", "--format=%an <%ae>|%cn <%ce>", "main")
    assert author == f"{MIRROR[0]} <{MIRROR[1]}>|{MIRROR[0]} <{MIRROR[1]}>"


def test_explicit_identity_email_must_be_reserved_or_noreply(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_EMAIL", "mirror" + "@" + "realmail.com")
    assert gated_push(root, gh, allow_identity_hit=True) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "identity" in out and public_main(remote) is None


def test_identity_refusal_does_not_offer_the_override(tmp_path, monkeypatch, capsys):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_NAME", "secretname")
    assert gated_push(root, gh) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "--allow-identity-hit" not in out


def test_noreply_identity_accepted(tmp_path, monkeypatch):
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    monkeypatch.setenv("KB_PUBLISH_IDENTITY_EMAIL", "1+mirror@users.noreply.github.com")
    assert gated_push(root, gh) == 0


def test_dry_run_without_a_public_read_says_the_diff_is_unknown(tmp_path, monkeypatch,
                                                                 capsys):
    monkeypatch.delenv("KB_PUBLISH_REMOTE", raising=False)
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert cli.run(root, ref="HEAD", push=False, tag=None, skip_verify=True) == 0
    out = capsys.readouterr().out
    assert "public HEAD not read" in out and "kb-publish diff" in out
    assert ack(root) and gated(root)["ack_gate_changes"]
