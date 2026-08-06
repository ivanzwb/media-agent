import json

from app.llm.providers.mock import MockProvider
from app.pipeline.script import build_script, extract_media


BODY = (
    "## 开头\n这是引子段落。\n\n"
    "![](https://x.com/pic1.png)\n\n"
    "## 进展\n第二段讲了进展。\n\n"
    '<iframe src="https://www.youtube.com/embed/abc" allowfullscreen></iframe>\n'
    "[▶ 视频链接](https://www.youtube.com/embed/abc)\n"
)


def test_extract_media_finds_images_and_videos():
    images, videos = extract_media(BODY)
    assert images == ["https://x.com/pic1.png"]
    assert "https://www.youtube.com/embed/abc" in videos
    assert len(videos) == 1  # deduped across iframe + link


def test_build_script_parses_llm_json():
    scenes = {"scenes": [
        {"narration": "开场白", "visual": "标题卡", "media": "image:1"},
        {"narration": "讲进展", "visual": "要点", "media": "video:1"},
    ]}
    provider = MockProvider(responses=[json.dumps(scenes)])
    script = build_script(BODY, "标题", provider)
    assert len(script["scenes"]) == 2
    assert script["scenes"][0]["narration"] == "开场白"
    assert script["scenes"][1]["media"] == "video:1"
    assert script["images"] and script["videos"]


def test_narration_is_written_under_the_anti_slop_rules():
    """Narration is read aloud, and asking for 口语化 is exactly what pulls
    the model toward 「说实话」「敲黑板」 — imitation speech, which sounds
    faker spoken than plain prose does."""
    systems: list[str] = []

    class _Spy(MockProvider):
        def chat(self, messages, **opts):
            systems.extend(m.content for m in messages if m.role == "system")
            return super().chat(messages, **opts)

    build_script(BODY, "标题", _Spy())
    assert systems and "严禁 AI 腔" in systems[0]


def test_build_script_falls_back_to_paragraphs():
    # default mock echoes non-JSON -> fallback splits body into scenes
    script = build_script(BODY, "标题", MockProvider())
    assert script["scenes"]
    # narration should not contain raw image markdown
    assert all("![" not in s["narration"] for s in script["scenes"])
    # fallback assigns available images
    assert any(s["media"].startswith("image:") for s in script["scenes"])
