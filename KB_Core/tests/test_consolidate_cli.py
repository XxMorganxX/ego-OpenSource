"""CLI smoke: preview needs no inference and writes nothing; --list prints the layer."""
import sys

from tests.conftest import TEST_DATABASE_URL


def test_cli_preview_runs_without_inference(kb, pool, monkeypatch, capsys):
    from tests.test_consolidate import _seed_two_projects
    _seed_two_projects(kb)
    monkeypatch.setenv("KB_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(sys, "argv", ["kb-consolidate", "--no-label"])
    from kb_core.consolidate_cli import main
    main()
    out = capsys.readouterr().out
    assert "2 communities" in out and "Preview only" in out
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM concepts").fetchone()["n"] == 0


def test_cli_apply_then_list(kb, monkeypatch, capsys):
    from tests.test_consolidate import _seed_two_projects
    _seed_two_projects(kb)
    monkeypatch.setenv("KB_DATABASE_URL", TEST_DATABASE_URL)
    from kb_core.consolidate_cli import main
    monkeypatch.setattr(sys, "argv", ["kb-consolidate", "--no-label", "--apply"])
    main()
    monkeypatch.setattr(sys, "argv", ["kb-consolidate", "--list"])
    main()
    out = capsys.readouterr().out
    assert "applied run" in out and "5 members" in out


def test_cli_default_uses_split_label_and_embedding_clients(kb, monkeypatch, capsys):
    from tests.test_consolidate import EmbeddingOnlyClient, LabelOnlyClient, _seed_two_projects
    _seed_two_projects(kb)
    monkeypatch.setenv("KB_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(sys, "argv", ["kb-consolidate"])
    import kb_core.consolidate_cli as cli
    label_client = LabelOnlyClient()
    embedding_client = EmbeddingOnlyClient()
    monkeypatch.setattr(
        cli, "build_consolidation_clients", lambda env: (label_client, embedding_client))

    cli.main()

    assert label_client.completions == 2
    assert len(embedding_client.texts) == 2
    assert "Preview only" in capsys.readouterr().out


def test_cli_hub_share_non_positive_maps_to_none(kb, monkeypatch, capsys):
    from tests.test_consolidate import _seed_two_projects
    _seed_two_projects(kb)
    monkeypatch.setenv("KB_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(sys, "argv",
                        ["kb-consolidate", "--no-label", "--hub-share", "0"])
    import kb_core.consolidate_cli as cli
    seen = {}

    def fake_run_once(got_kb, got_label, **kwargs):
        seen["hub_share"] = kwargs["hub_share"]
        return {"vertices": 0, "edges": 0, "communities": 0, "kept": 0,
                "discarded": 0, "hub_dropped": 0, "preview": [], "retire": []}

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    cli.main()

    assert seen["hub_share"] is None
