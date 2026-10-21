import os

from kb_core.cli import main


def _db_url():
    return os.environ.get("KB_TEST_DATABASE_URL", "postgresql://kb_test:kb_test@localhost:5433/kb_mcp_test")


def test_cli_syncs_vault(tmp_path, kb, capsys):
    vault = tmp_path / "v"
    vault.mkdir()
    (vault / "note.md").write_text("hello world", encoding="utf-8")
    rc = main(["--vault", str(vault), "--database-url", _db_url()])
    assert rc == 0
    assert "upserted=1" in capsys.readouterr().out
    assert kb.get_by_path("Obsidian.note") is not None   # default "Obsidian" source layer


def test_cli_errors_without_vault(tmp_path):
    import pytest
    with pytest.raises(SystemExit):
        main(["--database-url", _db_url()])
