from app.platforms.video_prepare import build_video_caption


class _JsonProvider:
    def __init__(self, payload):
        self.payload = payload

    def chat(self, messages, **k):
        return self.payload


def test_build_video_caption_from_llm():
    prov = _JsonProvider('{"title":"短标题","description":"简介一二三。",'
                         '"hashtags":["AI","科技"]}')
    cap = build_video_caption({"body_md": "正文", "topic": "AI"}, prov, "style")
    assert cap["title"] == "短标题"
    assert cap["hashtags"] == ["#AI", "#科技"]     # '#' auto-prefixed
    assert "#AI" in cap["caption"]


def test_build_video_caption_fallback_without_llm():
    cap = build_video_caption(
        {"title_cn": "中文标题", "body_md": "# H\n\n" + "正文 " * 30,
         "topic": "科技"}, None, "style")
    assert cap["title"] == "中文标题"
    assert cap["hashtags"] == ["#科技"]
    assert cap["description"]


def test_toutiao_platform_has_video_url():
    from app.platforms.registry import get
    t = get("toutiao")
    assert t is not None
    assert t.video_publish_url and "mp.toutiao.com" in t.video_publish_url
    assert t.video_caption_style()               # non-empty style prompt


def test_channels_caption_still_works_after_refactor():
    from app.wechat.channels import build_channels_caption
    cap = build_channels_caption(
        {"title_cn": "标题", "body_md": "正文内容", "topic": "AI"}, None)
    assert cap["title"] == "标题"
    assert cap["hashtags"] == ["#AI"]
