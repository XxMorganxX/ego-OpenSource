from kb_core.adapters.identity import (
    FrontmatterIdentity, IdentityStrategy, Observation, PathIdentity,
)


def test_path_identity_is_the_relative_path():
    obs = Observation(rel_path="Projects/KB MCP/design.md")
    assert PathIdentity().external_id(obs) == "Projects/KB MCP/design.md"


def test_frontmatter_identity_prefers_committed_id():
    obs = Observation(rel_path="a/b.md", frontmatter_id="fixed-id-123")
    assert FrontmatterIdentity().external_id(obs) == "fixed-id-123"


def test_frontmatter_identity_falls_back_to_path():
    obs = Observation(rel_path="a/b.md", frontmatter_id=None)
    assert FrontmatterIdentity().external_id(obs) == "a/b.md"


def test_frontmatter_fallback_is_composable():
    # The fallback is a strategy, not hardcoded path logic — swap it freely.
    class Const:
        def external_id(self, obs):  # noqa: ANN001
            return "CONST"

    obs = Observation(rel_path="a/b.md")
    assert FrontmatterIdentity(fallback=Const()).external_id(obs) == "CONST"


def test_concrete_strategies_satisfy_the_protocol():
    assert isinstance(PathIdentity(), IdentityStrategy)
    assert isinstance(FrontmatterIdentity(), IdentityStrategy)
