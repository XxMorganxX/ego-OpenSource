"""Push-target validation and `kb-publish pin`.

The public repo is identified by the numeric id GitHub assigned it, pinned once
in private state by `kb-publish pin`. A push goes only to a URL that parses to
owner/name, is not the private origin, resolves (without a redirect) to a
public repo, and carries exactly the pinned id.
"""
import json
import subprocess

import pytest

from kb_core.publish import cli, github, state, target
from tests.publish_fakes import (PRIVATE, PRIVATE_ID, PRIVATE_URL, PUBLIC, PUBLIC_ID,
                                 PUBLIC_URL, default_github)


# --- state -----------------------------------------------------------------

def test_state_defaults_include_the_pin_fields(tmp_path):
    assert state.load(tmp_path) == {
        "last_published_ref": None, "pending_coauthors": [],
        "public_repo_id": None, "public_repo_full_name": None,
        "last_published_public_sha": None,
        "private_repo_id": None, "private_repo_full_name": None,
    }


def test_state_loads_a_pre_pin_file(tmp_path):
    # Files written before the pin existed keep loading: missing keys default.
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "publish-state.json").write_text(json.dumps(
        {"last_published_ref": "abc", "pending_coauthors": [{"name": "P", "email": "p@example.com"}]}))
    st = state.load(tmp_path)
    assert st["last_published_ref"] == "abc"
    assert st["pending_coauthors"] == [{"name": "P", "email": "p@example.com"}]
    assert st["public_repo_id"] is None and st["last_published_public_sha"] is None


def test_state_save_roundtrips_the_pin(tmp_path):
    st = state.load(tmp_path)
    st.update(public_repo_id=PUBLIC_ID, public_repo_full_name=PUBLIC)
    state.save(tmp_path, st)
    assert state.load(tmp_path)["public_repo_id"] == PUBLIC_ID


# --- validate ----------------------------------------------------------------

def _validate(gh=None, *, remote=PUBLIC_URL, pinned_id=PUBLIC_ID, pinned_full_name=PUBLIC,
              origin=PRIVATE):
    return target.validate(remote, pinned_id=pinned_id, pinned_full_name=pinned_full_name,
                           origin_full_name=origin, github=gh or default_github())


def test_validate_accepts_the_pinned_public_repo():
    t = _validate()
    assert t == target.Target(full_name=PUBLIC, repo_id=PUBLIC_ID, empty=True)


def test_validate_reports_non_empty_repo():
    assert _validate(default_github(public_empty=False)).empty is False


@pytest.mark.parametrize("kw,needle", [
    ({"remote": "/tmp/public.git"}, "not a github.com"),
    ({"remote": "git@github-deploy:acme/mirror.git"}, "not a github.com"),
    ({"pinned_id": None}, "kb-publish pin"),
    ({"remote": PRIVATE_URL}, "private origin"),
    ({"remote": "https://github.com/ACME/Private-Repo"}, "private origin"),
    ({"origin": None}, "origin"),
    ({"pinned_id": 999}, "pinned"),
    ({"pinned_full_name": "acme/old-name"}, "pinned"),
    ({"remote": "ssh://git@github.com/acme/nope.git"}, "404"),
])
def test_validate_refusals(kw, needle):
    with pytest.raises(target.TargetError) as e:
        _validate(**kw)
    assert needle.lower() in str(e.value).lower()


def test_validate_refuses_private_repo():
    gh = default_github()
    gh.set(PUBLIC, visibility="private")
    with pytest.raises(target.TargetError, match="not public"):
        _validate(gh)


def test_validate_refuses_redirected_name():
    # GitHub follows renames: an old name answers with the NEW repo. The URL
    # that was typed is then a name someone else can claim later — refuse.
    gh = default_github()
    gh.repos["acme/old-mirror"] = gh.repos[PUBLIC.lower()]
    with pytest.raises(target.TargetError, match="redirect"):
        _validate(gh, remote="git@github.com:acme/old-mirror.git")


def test_validate_refuses_target_with_origins_id():
    gh = default_github()
    gh.set(PUBLIC, id=PRIVATE_ID)
    with pytest.raises(target.TargetError):
        _validate(gh, pinned_id=PRIVATE_ID)


def test_validate_refuses_when_origin_lookup_fails():
    gh = default_github()
    del gh.repos[PRIVATE.lower()]
    with pytest.raises(target.TargetError, match="origin"):
        _validate(gh)


# --- origin --------------------------------------------------------------

def _git_repo(tmp_path, origin=PRIVATE_URL):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    if origin:
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=root, check=True)
    (root / "publish.deny.toml").write_text('[deny]\npatterns = ["secretname"]\n')
    return root


def test_origin_full_name(tmp_path):
    assert target.origin_full_name(_git_repo(tmp_path)) == PRIVATE


@pytest.mark.parametrize("origin", [None, "/srv/git/private.git"])
def test_origin_full_name_unresolvable(tmp_path, origin):
    with pytest.raises(target.TargetError, match="origin"):
        target.origin_full_name(_git_repo(tmp_path, origin))


# --- kb-publish pin --------------------------------------------------------

def test_pin_records_id_and_name(tmp_path, capsys):
    root = _git_repo(tmp_path)
    state.save(root, {**state.load(root), "last_published_ref": "abc"})
    assert cli.pin(root, repo=PUBLIC, github=default_github()) == 0
    st = state.load(root)
    assert (st["public_repo_id"], st["public_repo_full_name"]) == (PUBLIC_ID, PUBLIC)
    assert st["last_published_ref"] == "abc"          # other state untouched
    assert str(PUBLIC_ID) in capsys.readouterr().out


def test_pin_stores_canonical_case(tmp_path):
    root = _git_repo(tmp_path)
    assert cli.pin(root, repo="ACME/Mirror", github=default_github()) == 0
    assert state.load(root)["public_repo_full_name"] == PUBLIC


@pytest.mark.parametrize("repo,needle", [
    (PRIVATE, "private origin"),
    ("acme/nope", "404"),
    ("not-a-name", "owner/name"),
    ("acme/../x", "owner/name"),
])
def test_pin_refusals(tmp_path, capsys, repo, needle):
    root = _git_repo(tmp_path)
    assert cli.pin(root, repo=repo, github=default_github()) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and needle.lower() in out.lower()
    assert state.load(root)["public_repo_id"] is None


def test_pin_refuses_non_public(tmp_path, capsys):
    root = _git_repo(tmp_path)
    gh = default_github()
    gh.set(PUBLIC, visibility="private")
    assert cli.pin(root, repo=PUBLIC, github=gh) == 1
    assert "not public" in capsys.readouterr().out


def test_pin_refuses_same_id_as_origin(tmp_path, capsys):
    root = _git_repo(tmp_path)
    gh = default_github()
    gh.set(PUBLIC, id=PRIVATE_ID)
    assert cli.pin(root, repo=PUBLIC, github=gh) == 1
    assert "REFUSED" in capsys.readouterr().out


def test_pin_refuses_redirected_name(tmp_path, capsys):
    root = _git_repo(tmp_path)
    gh = default_github()
    gh.repos["acme/old-mirror"] = gh.repos[PUBLIC.lower()]
    assert cli.pin(root, repo="acme/old-mirror", github=gh) == 1
    assert "redirect" in capsys.readouterr().out.lower()


def test_pin_refuses_without_origin(tmp_path, capsys):
    root = _git_repo(tmp_path, origin=None)
    assert cli.pin(root, repo=PUBLIC, github=default_github()) == 1
    assert "origin" in capsys.readouterr().out


def test_pin_refuses_without_deny_file(tmp_path, capsys):
    root = _git_repo(tmp_path)
    (root / "publish.deny.toml").unlink()
    assert cli.pin(root, repo=PUBLIC, github=default_github()) == 1
    assert "private-repo operation" in capsys.readouterr().out


def test_repin_to_another_repo_needs_replace(tmp_path, capsys):
    root = _git_repo(tmp_path)
    gh = default_github()
    gh.repos["acme/mirror-two"] = github.RepoInfo(3003, "acme/mirror-two", "public", True)
    assert cli.pin(root, repo=PUBLIC, github=gh) == 0
    assert cli.pin(root, repo=PUBLIC, github=gh) == 0          # same pin: idempotent
    assert cli.pin(root, repo="acme/mirror-two", github=gh) == 1
    assert "--replace" in capsys.readouterr().out
    assert state.load(root)["public_repo_id"] == PUBLIC_ID
    assert cli.pin(root, repo="acme/mirror-two", github=gh, replace=True) == 0
    assert state.load(root)["public_repo_id"] == 3003


def test_main_pin_subcommand(tmp_path, monkeypatch):
    root = _git_repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.github_api, "gh_repo", default_github())
    assert cli.main(["pin", "--repo", PUBLIC]) == 0
    assert state.load(root)["public_repo_id"] == PUBLIC_ID


# --- pin --replace --reset-history: the recreated-repo incident path --------

MIRROR_TWO = "acme/mirror-two"
MIRROR_TWO_ID = 3003


def _published(tmp_path):
    root = _git_repo(tmp_path)
    gh = default_github()
    gh.repos[MIRROR_TWO] = github.RepoInfo(MIRROR_TWO_ID, MIRROR_TWO, "public", True)
    assert cli.pin(root, repo=PUBLIC, github=gh) == 0
    state.save(root, {**state.load(root), "last_published_ref": "a" * 40,
                      "last_published_public_sha": "b" * 40,
                      "pending_coauthors": [{"name": "C", "email": "c@example.com"}]})
    return root, gh


def test_reset_history_needs_replace(tmp_path, capsys):
    root, gh = _published(tmp_path)
    assert cli.pin(root, repo=MIRROR_TWO, github=gh, reset_history=True) == 1
    assert "--replace" in capsys.readouterr().out
    assert state.load(root)["last_published_public_sha"] == "b" * 40


def test_reset_history_clears_the_publish_record_and_says_what(tmp_path, capsys):
    root, gh = _published(tmp_path)
    capsys.readouterr()
    assert cli.pin(root, repo=MIRROR_TWO, github=gh, replace=True, reset_history=True) == 0
    st = state.load(root)
    assert st["public_repo_id"] == MIRROR_TWO_ID
    assert st["last_published_ref"] is None and st["last_published_public_sha"] is None
    assert st["pending_coauthors"] == [{"name": "C", "email": "c@example.com"}]
    out = capsys.readouterr().out
    assert "a" * 40 in out and "b" * 40 in out and "first publish" in out


def test_reset_history_after_a_plain_replace(tmp_path, capsys):
    """Re-pinning first and resetting after (the order an operator may hit the
    refusal in) works: the guard is GitHub reporting the repo empty."""
    root, gh = _published(tmp_path)
    assert cli.pin(root, repo=MIRROR_TWO, github=gh, replace=True) == 0
    assert cli.pin(root, repo=MIRROR_TWO, github=gh, replace=True, reset_history=True) == 0
    assert state.load(root)["last_published_public_sha"] is None


def test_reset_history_refused_for_a_non_empty_repo(tmp_path, capsys):
    root, gh = _published(tmp_path)
    gh.set(MIRROR_TWO, empty=False)
    assert cli.pin(root, repo=MIRROR_TWO, github=gh, replace=True, reset_history=True) == 1
    assert "not empty" in capsys.readouterr().out
    st = state.load(root)
    assert st["public_repo_id"] == PUBLIC_ID and st["last_published_public_sha"] == "b" * 40


def test_replace_onto_an_empty_repo_hints_at_reset_history(tmp_path, capsys):
    root, gh = _published(tmp_path)
    capsys.readouterr()
    assert cli.pin(root, repo=MIRROR_TWO, github=gh, replace=True) == 0
    assert "--reset-history" in capsys.readouterr().out
    assert state.load(root)["last_published_public_sha"] == "b" * 40


def test_recreated_public_repo_publishes_again_after_reset(tmp_path, monkeypatch, capsys):
    """End to end: publish, the public repo is deleted and recreated (new id,
    empty), the next push refuses until pin --replace --reset-history."""
    from tests.publish_fakes import ack, bare, gated_push, scratch_private

    root, _remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh) == 0
    bare(tmp_path / "gh", MIRROR_TWO)
    gh.repos[MIRROR_TWO] = github.RepoInfo(MIRROR_TWO_ID, MIRROR_TWO, "public", True)
    url = f"ssh://git@github.com/{MIRROR_TWO}.git"
    assert cli.pin(root, repo=MIRROR_TWO, github=gh, replace=True) == 0
    capsys.readouterr()
    assert gated_push(root, gh, remote=url, ack_gate_changes=ack(root, url)) == 1
    assert "deleted" in capsys.readouterr().out
    assert cli.pin(root, repo=MIRROR_TWO, github=gh, replace=True, reset_history=True) == 0
    assert gated_push(root, gh, remote=url, ack_gate_changes=ack(root, url)) == 0
    assert state.load(root)["last_published_public_sha"] is not None
