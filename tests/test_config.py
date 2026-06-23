from app.config import Config


def test_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    cfg = Config.load()
    assert cfg.data_dir == tmp_path
    assert cfg.archive_dir == tmp_path / "archive"
    assert cfg.drafts_dir == tmp_path / "drafts"
    assert cfg.images_dir == tmp_path / "images"
    assert cfg.db_path == tmp_path / "media.db"
    assert cfg.llm_provider == "mock"


def test_config_reads_llm_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEDIA_AGENT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("MEDIA_AGENT_LLM_API_KEY", "sk-test")
    cfg = Config.load()
    assert cfg.llm_provider == "openai"
    assert cfg.llm_api_key == "sk-test"


def test_ensure_dirs_creates(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path / "d"))
    cfg = Config.load()
    cfg.ensure_dirs()
    assert cfg.archive_dir.is_dir()
    assert cfg.drafts_dir.is_dir()
    assert cfg.images_dir.is_dir()
