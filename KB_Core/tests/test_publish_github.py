"""The GitHub seam: remote-URL parsing and the default `gh api` repo lookup.

The default lookup is exercised through an injected runner, so no test reaches
GitHub; it is the one place kb-publish turns "owner/name" into an id and a
visibility.
"""
import json
import subprocess

import pytest

from kb_core.publish import github


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:acme/mirror.git", "acme/mirror"),
    ("git@github.com:acme/mirror", "acme/mirror"),
    ("ssh://git@github.com/acme/mirror.git", "acme/mirror"),
    ("ssh://git@github.com:22/acme/mirror.git", "acme/mirror"),
    ("ssh://git@ssh.github.com:443/acme/mirror.git", "acme/mirror"),
    ("https://github.com/acme/mirror.git", "acme/mirror"),
    ("https://github.com/acme/mirror", "acme/mirror"),
    ("https://token@github.com/acme/my.repo_name-2.git", "acme/my.repo_name-2"),
    ("https://github.com/Acme-Org/Mirror/", "Acme-Org/Mirror"),
    ("git@github.com:acme/.github.git", "acme/.github"),
])
def test_parse_remote_accepts_github_forms(url, expected):
    assert github.parse_remote(url) == expected


@pytest.mark.parametrize("url", [
    "/tmp/public.git",                                   # local path: no GitHub identity
    "file:///tmp/public.git",
    "git@gitlab.com:acme/mirror.git",
    "git@github-deploy:acme/mirror.git",                 # ssh host alias: unverifiable
    "https://github.com.evil.example/acme/mirror.git",
    "https://evil.example/github.com/acme/mirror.git",
    "ssh://git@github.com/acme/mirror/extra.git",
    "git@github.com:acme/..",
    "git@github.com:acme/.git",
    "git@github.com:-acme/mirror.git",
    "https://github.com/acme/mirror.git?x=1",
    "",
])
def test_parse_remote_rejects_everything_else(url):
    assert github.parse_remote(url) is None


@pytest.mark.parametrize("name,ok", [
    ("acme/mirror", True), ("Acme-Org/my.repo_2", True),
    ("acme", False), ("acme/mirror/x", False), ("../acme", False),
    ("acme/..", False), ("acme/mirror?per_page=1", False), ("-x/mirror", False),
])
def test_valid_full_name(name, ok):
    assert github.valid_full_name(name) is ok


class _Runner:
    """Records argv/env and answers `gh api` paths from a dict (or fails)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        path = argv[-1]
        ans = self.answers.get(path)
        if isinstance(ans, Exception):
            raise ans
        if ans is None:
            return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")
        return subprocess.CompletedProcess(argv, 0, json.dumps(ans), "")


def _repo_json(**over):
    data = {"id": 2002, "full_name": "acme/mirror", "private": False,
            "visibility": "public", "size": 0}
    data.update(over)
    return data


def test_gh_repo_reads_id_name_visibility_and_emptiness():
    run = _Runner({"repos/acme/mirror": _repo_json(),
                   "repos/acme/mirror/branches?per_page=1": []})
    info = github.gh_repo("acme/mirror", run=run)
    assert info == github.RepoInfo(id=2002, full_name="acme/mirror",
                                   visibility="public", empty=True)
    argv, kw = run.calls[0]
    assert argv[:2] == ["gh", "api"] and argv[-1] == "repos/acme/mirror"
    # Never another host (GH_HOST from the shell), never a prompt, always a timeout.
    assert kw["env"]["GH_HOST"] == "github.com"
    assert kw["env"]["GH_PROMPT_DISABLED"] == "1"
    assert "GH_REPO" not in kw["env"]
    assert kw["timeout"] > 0


def test_gh_repo_non_empty_when_branches_exist_or_size_nonzero():
    run = _Runner({"repos/acme/mirror": _repo_json(size=0),
                   "repos/acme/mirror/branches?per_page=1": [{"name": "main"}]})
    assert github.gh_repo("acme/mirror", run=run).empty is False
    run = _Runner({"repos/acme/mirror": _repo_json(size=12)})
    assert github.gh_repo("acme/mirror", run=run).empty is False


@pytest.mark.parametrize("over,vis", [
    ({"visibility": "private", "private": True}, "private"),
    ({"visibility": "internal", "private": True}, "internal"),
    ({"visibility": "public", "private": True}, "private"),     # contradiction: fail closed
    ({"visibility": None, "private": False}, "public"),         # older API: no field
    ({"visibility": None, "private": True}, "private"),
])
def test_gh_repo_visibility_fails_closed(over, vis):
    run = _Runner({"repos/acme/mirror": _repo_json(size=5, **over)})
    assert github.gh_repo("acme/mirror", run=run).visibility == vis


@pytest.mark.parametrize("answers", [
    {},                                                             # 404
    {"repos/acme/mirror": subprocess.TimeoutExpired("gh", 60)},
    {"repos/acme/mirror": FileNotFoundError("gh")},                 # gh not installed
    {"repos/acme/mirror": {"full_name": "acme/mirror"}},            # no id
    {"repos/acme/mirror": _repo_json(id="2002")},                   # id not an int
    {"repos/acme/mirror": ["not", "an", "object"]},
])
def test_gh_repo_errors_raise_github_error(answers):
    with pytest.raises(github.GitHubError):
        github.gh_repo("acme/mirror", run=_Runner(answers))


def test_gh_repo_refuses_malformed_name_without_calling_gh():
    run = _Runner({})
    with pytest.raises(github.GitHubError):
        github.gh_repo("acme/../other", run=run)
    assert run.calls == []
