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
    assert cfg.avatar_dir.is_dir()   # digital-human presenter dir


class _FakeStore:
    def __init__(self, d):
        self.d = d

    def get_setting(self, key, default=None):
        return self.d.get(key, default)


def test_tts_expressive_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEDIA_AGENT_TTS_RATE", "+10%")
    monkeypatch.setenv("MEDIA_AGENT_TTS_PITCH", "+15Hz")
    monkeypatch.setenv("MEDIA_AGENT_TTS_INSTRUCT", "用亲切的语气")
    cfg = Config.load()
    assert cfg.tts_rate == "+10%"
    assert cfg.tts_pitch == "+15Hz"
    assert cfg.tts_instruct == "用亲切的语气"


def test_avatar_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEDIA_AGENT_AVATAR_ENABLED", "1")
    monkeypatch.setenv("MEDIA_AGENT_AVATAR_IMAGE", "p.png")
    monkeypatch.setenv("MEDIA_AGENT_AVATAR_POSITION", "full")
    monkeypatch.setenv("MEDIA_AGENT_AVATAR_PROVIDER", "sadtalker")
    monkeypatch.setenv("MEDIA_AGENT_SADTALKER_DIR", "/opt/SadTalker")
    cfg = Config.load()
    assert cfg.avatar_enabled is True
    assert cfg.avatar_image == "p.png"
    assert cfg.avatar_position == "full"
    assert cfg.avatar_provider == "sadtalker"
    assert cfg.sadtalker_dir == "/opt/SadTalker"


def test_avatar_enabled_defaults_off(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    assert Config.load().avatar_enabled is False


def test_db_overrides_avatar_and_tts(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    store = _FakeStore({
        "avatar_enabled": "1", "avatar_image": "db.png",
        "avatar_position": "pip", "sadtalker_dir": "/db/sad",
        "tts_rate": "-5%", "tts_pitch": "-10Hz", "tts_instruct": "热情",
    })
    cfg = Config.load(store=store)
    assert cfg.avatar_enabled is True
    assert cfg.avatar_image == "db.png"
    assert cfg.avatar_position == "pip"
    assert cfg.sadtalker_dir == "/db/sad"
    assert cfg.tts_rate == "-5%" and cfg.tts_pitch == "-10Hz"
    assert cfg.tts_instruct == "热情"


def test_db_override_avatar_enabled_off(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEDIA_AGENT_AVATAR_ENABLED", "1")
    # DB explicitly turns it off, overriding env
    cfg = Config.load(store=_FakeStore({"avatar_enabled": "0"}))
    assert cfg.avatar_enabled is False
