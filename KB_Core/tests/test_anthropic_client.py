import pytest

from kb_core.ingest.anthropic_client import DEFAULT_MODEL, AnthropicInferenceClient
from kb_core.ingest.inference import (
    HTTPInferenceClient,
    InferenceClient,
    InferenceError,
    build_inference_client,
)


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class _FakeMessages:
    def __init__(self, outer: "FakeAnthropic") -> None:
        self._outer = outer

    def create(self, **kwargs):
        self._outer.calls.append(kwargs)
        if self._outer.raise_exc is not None:
            raise self._outer.raise_exc
        return _Resp(self._outer.reply)


class FakeAnthropic:
    """Stands in for anthropic.Anthropic — records messages.create calls, returns a scripted reply."""
    def __init__(self, reply: str = "{}", raise_exc: Exception | None = None) -> None:
        self.reply = reply
        self.raise_exc = raise_exc
        self.calls: list[dict] = []
        self.messages = _FakeMessages(self)


def test_anthropic_client_satisfies_inference_protocol():
    assert isinstance(AnthropicInferenceClient(client=FakeAnthropic()), InferenceClient)


def test_complete_passes_system_and_user_and_returns_text():
    fake = FakeAnthropic(reply="the reply")
    client = AnthropicInferenceClient(model="claude-sonnet-4-6", client=fake)
    out = client.complete("SYS", "USER")
    assert out == "the reply"
    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-4-6"
    assert call["system"] == "SYS"                          # schema-free: system passed verbatim
    assert call["messages"] == [{"role": "user", "content": "USER"}]


def test_complete_appends_schema_shape_to_system():
    fake = FakeAnthropic(reply="{}")
    client = AnthropicInferenceClient(client=fake)
    client.complete("SYS", "USER", schema={"type": "object", "required": ["entities"]})
    system = fake.calls[0]["system"]
    assert "SYS" in system and "entities" in system and "schema" in system.lower()


def test_complete_joins_text_blocks_and_ignores_non_text():
    fake = FakeAnthropic()
    fake.messages.create = lambda **kw: type(
        "R", (), {"content": [_Block("a"), type("T", (), {"type": "thinking"})(), _Block("b")]})()
    client = AnthropicInferenceClient(client=fake)
    assert client.complete("s", "u") == "ab"


def test_complete_wraps_transport_errors_as_inference_error():
    fake = FakeAnthropic(raise_exc=RuntimeError("boom"))
    client = AnthropicInferenceClient(client=fake)
    with pytest.raises(InferenceError):
        client.complete("s", "u")


def test_build_inference_client_defaults_to_local_http():
    assert isinstance(build_inference_client({}), HTTPInferenceClient)


def test_local_client_defaults_to_the_inference_port_not_the_dashboard():
    """8000 is the DASHBOARD port (kb-svc.sh KB_DASHBOARD_PORT); inference listens on
    8001 (KB_INFERENCE_PORT). Defaulting to 8000 makes an unset KB_INFERENCE_URL POST
    embeddings at the dashboard, which 404s — in a consolidation run that makes every
    concept embedding fail silently."""
    assert build_inference_client({})._base == "http://127.0.0.1:8001"
    assert HTTPInferenceClient()._base == "http://127.0.0.1:8001"
    # an explicit URL still wins
    assert build_inference_client(
        {"KB_INFERENCE_URL": "http://elsewhere:9000"})._base == "http://elsewhere:9000"


def test_no_module_falls_back_to_the_dashboard_port_for_inference():
    """The wrong default was copied into eight places across six modules. The daemons
    are masked by kb-svc.sh exporting KB_INFERENCE_URL; the hand-run one-shots
    (embed_backfill, the CLIs) are not. One constant, asserted here so a ninth copy
    cannot reintroduce it."""
    import pathlib

    import kb_core

    root = pathlib.Path(kb_core.__file__).parent
    offenders = [f"{p.relative_to(root)}:{i}"
                 for p in sorted(root.rglob("*.py"))
                 for i, line in enumerate(p.read_text().splitlines(), 1)
                 if "127.0.0.1:8000" in line]
    assert offenders == [], f"dashboard port used as an inference default: {offenders}"


def test_build_inference_client_selects_anthropic_with_default_model():
    client = build_inference_client({"KB_INFERENCE_PROVIDER": "anthropic"})
    assert isinstance(client, AnthropicInferenceClient)
    assert client.model == DEFAULT_MODEL == "claude-sonnet-5"


def test_build_inference_client_honours_model_override():
    client = build_inference_client(
        {"KB_INFERENCE_PROVIDER": "anthropic", "KB_ANTHROPIC_MODEL": "claude-haiku-4-5"})
    assert client.model == "claude-haiku-4-5"


def test_build_inference_client_anthropic_blank_model_falls_back_to_default():
    # Same trap as the claude-cli path: a launcher forwarding ${KB_ANTHROPIC_MODEL:-} sets the
    # var to '' when unset, and env.get(key, default) returns that '' (key present), so the model
    # must fall back to the default explicitly rather than passing an empty model to the API.
    client = build_inference_client(
        {"KB_INFERENCE_PROVIDER": "anthropic", "KB_ANTHROPIC_MODEL": ""})
    assert client.model == DEFAULT_MODEL == "claude-sonnet-5"


def test_build_inference_client_explicit_local_provider_overrides_environment():
    client = build_inference_client(
        {"KB_INFERENCE_PROVIDER": "anthropic"}, provider="local")
    assert isinstance(client, HTTPInferenceClient)


def test_declares_provider_identity():
    from kb_core.ingest.anthropic_client import AnthropicInferenceClient
    c = AnthropicInferenceClient(model="claude-sonnet-5")
    assert (c.provider, c.model) == ("anthropic", "claude-sonnet-5")
