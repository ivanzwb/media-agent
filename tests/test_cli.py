from typer.testing import CliRunner

from app.cli import app

runner = CliRunner()


def test_init_command_creates_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / "media.db").exists()


def test_run_command_uses_mock(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEDIA_AGENT_LLM_PROVIDER", "mock")
    feeds = tmp_path / "feeds.yaml"
    feeds.write_text(
        "topics: [{name: AI, keywords: [GPT]}]\nsources: []\n", encoding="utf-8")
    result = runner.invoke(app, ["run", "--feeds", str(feeds)])
    assert result.exit_code == 0
    assert "fetched" in result.stdout
