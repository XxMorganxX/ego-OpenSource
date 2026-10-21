"""Unit tests for the output cap and truncation logging (no Ollama contact).

ChatOllama is replaced with a recorder so we can assert exactly which kwargs the
manager hands the model, and the LLM is faked so chat() runs without a server.
"""

import json
import logging

import conversation
from conversation import ConversationManager


class _FakeChatOllama:
    """Records the kwargs it was constructed with."""
    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        _FakeChatOllama.last_kwargs = kwargs


class _FakeReply:
    def __init__(self, text, done_reason):
        self.content = text
        self.response_metadata = {"done_reason": done_reason}


class _FakeLLM:
    def __init__(self, reply):
        self._reply = reply

    def invoke(self, messages):
        return self._reply


def test_num_predict_is_passed_to_the_model(monkeypatch):
    monkeypatch.setattr(conversation, "ChatOllama", _FakeChatOllama)
    ConversationManager(num_predict=512)
    assert _FakeChatOllama.last_kwargs["num_predict"] == 512


def test_none_num_predict_leaves_the_model_uncapped(monkeypatch):
    monkeypatch.setattr(conversation, "ChatOllama", _FakeChatOllama)
    ConversationManager(num_predict=None)
    assert "num_predict" not in _FakeChatOllama.last_kwargs


def test_default_cap_is_8192_tokens():
    assert conversation.DEFAULT_NUM_PREDICT == 8192


def test_think_on_is_passed_as_reasoning(monkeypatch):
    monkeypatch.setattr(conversation, "ChatOllama", _FakeChatOllama)
    ConversationManager(reasoning=True)
    assert _FakeChatOllama.last_kwargs["reasoning"] is True


def test_think_off_is_passed_as_reasoning(monkeypatch):
    monkeypatch.setattr(conversation, "ChatOllama", _FakeChatOllama)
    ConversationManager(reasoning=False)
    assert _FakeChatOllama.last_kwargs["reasoning"] is False


def test_unset_think_does_not_override_the_model_default(monkeypatch):
    """Unset must not collapse to False — that would silently disable a thinking model."""
    monkeypatch.setattr(conversation, "ChatOllama", _FakeChatOllama)
    ConversationManager(reasoning=None)
    assert "reasoning" not in _FakeChatOllama.last_kwargs


def test_parse_think_reads_truthy_spellings():
    for raw in ("1", "true", "TRUE", "on", "yes", " On "):
        assert conversation._parse_think(raw) is True, raw


def test_parse_think_reads_falsy_spellings():
    for raw in ("0", "false", "off", "no", "nonsense"):
        assert conversation._parse_think(raw) is False, raw


def test_parse_think_treats_unset_and_blank_as_undecided():
    assert conversation._parse_think(None) is None
    assert conversation._parse_think("") is None
    assert conversation._parse_think("   ") is None


def test_truncated_reply_is_logged_as_truncated(monkeypatch, caplog):
    monkeypatch.setattr(conversation, "ChatOllama", _FakeChatOllama)
    mgr = ConversationManager()
    monkeypatch.setattr(mgr, "_llm_for", lambda fmt: _FakeLLM(_FakeReply("x" * 20, "length")))
    with caplog.at_level(logging.INFO, logger="kb_inference"):
        mgr.chat("s1", "hi")
    assert "truncated=True" in caplog.text


def test_normal_reply_is_not_logged_as_truncated(monkeypatch, caplog):
    monkeypatch.setattr(conversation, "ChatOllama", _FakeChatOllama)
    mgr = ConversationManager()
    monkeypatch.setattr(mgr, "_llm_for", lambda fmt: _FakeLLM(_FakeReply("done", "stop")))
    with caplog.at_level(logging.INFO, logger="kb_inference"):
        mgr.chat("s1", "hi")
    assert "truncated=False" in caplog.text


def test_manager_exposes_truncation_per_session(monkeypatch):
    # The eval harness needs this machine-readable, not only in the log text.
    monkeypatch.setattr(conversation, "ChatOllama", _FakeChatOllama)
    mgr = ConversationManager()
    monkeypatch.setattr(mgr, "_llm_for", lambda fmt: _FakeLLM(_FakeReply("x", "length")))
    mgr.chat("s1", "hi")
    assert mgr.was_truncated("s1") is True


def test_untouched_session_reports_no_truncation(monkeypatch):
    monkeypatch.setattr(conversation, "ChatOllama", _FakeChatOllama)
    assert ConversationManager().was_truncated("never-used") is False


# --- bounded session store ---------------------------------------------------------

def test_session_store_is_bounded_and_evicts_the_least_recently_used(monkeypatch):
    """A caller that never purges (or a flood of fresh ids) must not grow memory forever:
    past max_sessions the least recently used session is dropped."""
    monkeypatch.setattr(conversation, "ChatOllama", _FakeChatOllama)
    m = ConversationManager(max_sessions=3)
    for sid in ("a", "b", "c"):
        m.set_system_prompt(sid, sid)
    m.get_system_prompt("a")                    # touch: "b" is now the oldest
    m.set_system_prompt("d", "d")
    assert sorted(m.list_sessions()) == ["a", "c", "d"]
    assert m.get_system_prompt("a") == "a"


def test_exists_does_not_create_or_refresh_a_session(monkeypatch):
    monkeypatch.setattr(conversation, "ChatOllama", _FakeChatOllama)
    m = ConversationManager(max_sessions=2)
    assert m.exists("nope") is False
    assert m.list_sessions() == []


def test_health_failure_does_not_echo_the_exception(monkeypatch):
    """/health is reachable by anything allowed to talk to the server: a raw exception
    string can carry internal URLs or paths. The detail goes to the log instead."""
    monkeypatch.setattr(conversation, "ChatOllama", _FakeChatOllama)
    m = ConversationManager(base_url="http://127.0.0.1:9")

    def boom(*a, **kw):
        raise OSError("connection refused to /private/internal/path")

    monkeypatch.setattr(conversation.urllib.request, "urlopen", boom)
    h = m.health()
    assert h["ollama"] == "down"
    assert "/private/internal/path" not in json.dumps(h)
    assert h["error"] == "ollama unreachable"
