import types
from pathlib import Path

from app.wechat import html as H
from app.wechat import client as C
from app.wechat import publish as P


# ── html ───────────────────────────────────────────────────────────────────

def test_markdown_to_html_covers_common_blocks():
    md = ("# T\n\nIntro **b** *i* `c`.\n\n## S\n\n- a\n- b\n\n"
          "![alt](/images/p.png)\n\n> q\n\n[[VIDEO:0]]\n"
          '<iframe src="x"></iframe>\n')
    out = H.markdown_to_html(md)
    assert "<h1>T</h1>" in out and "<h2>S</h2>" in out
    assert "<strong>b</strong>" in out and "<em>i</em>" in out
    assert "<code>c</code>" in out
    assert "<ul>" in out and "<li>a</li>" in out
    assert '<img src="/images/p.png"' in out
    assert "<blockquote>q</blockquote>" in out
    assert "[[VIDEO" not in out and "<iframe" not in out


def test_image_src_helpers():
    html = H.markdown_to_html("![](/images/a.png)\n\n![](/media/x/b.jpg)")
    assert H.image_srcs(html) == ["/images/a.png", "/media/x/b.jpg"]
    remapped = H.replace_image_srcs(html, {"/images/a.png": "https://w/a.png"})
    assert "https://w/a.png" in remapped
    dropped = H.replace_image_srcs(html, {"/images/a.png": ""})
    assert 'src="/images/a.png"' not in dropped


def test_replace_image_srcs_drops_whole_tag_no_leaked_attrs():
    """Dropping an image must remove the ENTIRE <img …> tag, not just the
    src="…" prefix (which left alt/style leaking as visible text on WeChat)."""
    html = ('<p><img src="x.svg" alt="" '
            'style="max-width:100%;display:block;"/><br/><strong>t</strong></p>')
    dropped = H.replace_image_srcs(html, {"x.svg": ""})
    assert "<img" not in dropped
    assert "alt=" not in dropped
    assert "style=" not in dropped
    assert "<strong>t</strong>" in dropped   # surrounding content preserved
    # replacement still rewrites the whole tag's src
    swapped = H.replace_image_srcs(html, {"x.svg": "https://w/y.png"})
    assert 'src="https://w/y.png"' in swapped
    assert "alt=" in swapped                  # other attrs kept on replace


# ── client (mocked httpx) ───────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        ...

    def json(self):
        return self._p


def _fake_httpx(routes, counter=None):
    mod = types.SimpleNamespace()

    def get(url, params=None, timeout=None):
        if url.endswith("/token"):
            if counter is not None:
                counter["token"] = counter.get("token", 0) + 1
            return _Resp({"access_token": "TOK", "expires_in": 7200})
        return _Resp({})

    def post(url, params=None, content=None, files=None, data=None,
             headers=None, timeout=None):
        for suffix, payload in routes.items():
            if url.endswith(suffix):
                return _Resp(payload)
        return _Resp({"errcode": 40001, "errmsg": "bad"})

    mod.get = get
    mod.post = post
    return mod


def test_client_token_is_cached(monkeypatch):
    counter = {}
    monkeypatch.setattr(C, "httpx", _fake_httpx({}, counter))
    cli = C.WeChatClient("appid", "secret")
    assert cli.access_token() == "TOK"
    assert cli.access_token() == "TOK"
    assert counter["token"] == 1   # second call served from cache


def test_client_upload_draft_publish(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "httpx", _fake_httpx({
        "/media/uploadimg": {"url": "https://w/body.png"},
        "/material/add_material": {"media_id": "MID", "url": "https://w/c.png"},
        "/draft/add": {"media_id": "DRAFT1"},
        "/freepublish/submit": {"publish_id": 42},
    }))
    cli = C.WeChatClient("appid", "secret")
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG" + b"0" * 50)
    assert cli.upload_article_image(img) == "https://w/body.png"
    assert cli.add_material("image", img)["media_id"] == "MID"
    assert cli.add_draft([{"title": "t"}]) == "DRAFT1"
    assert cli.freepublish_submit("DRAFT1") == "42"


def test_client_raises_on_errcode(monkeypatch):
    monkeypatch.setattr(C, "httpx", _fake_httpx({}))
    cli = C.WeChatClient("appid", "secret")
    try:
        cli.freepublish_get("x")   # unmatched route -> errcode 40001
    except C.WeChatError as e:
        assert e.errcode == 40001
    else:
        raise AssertionError("expected WeChatError")


def test_get_wechat_client_requires_credentials():
    cfg = types.SimpleNamespace(wechat_appid="", wechat_appsecret="")
    assert C.get_wechat_client(cfg) is None
    cfg2 = types.SimpleNamespace(wechat_appid="a", wechat_appsecret="b")
    assert C.get_wechat_client(cfg2) is not None


# ── publish orchestration (dummy client) ────────────────────────────────────

class _DummyClient:
    def __init__(self):
        self.drafts = []
        self.published = []

    def upload_article_image(self, path):
        return "https://w/body-" + Path(path).name

    def add_material(self, mtype, path, title=None, introduction=None):
        return {"media_id": "COVER", "url": "https://w/c.png"}

    def add_draft(self, articles):
        self.drafts.append(articles)
        return "DRAFTX"

    def freepublish_submit(self, media_id):
        self.published.append(media_id)
        return "PUB1"


def _cfg(tmp_path):
    (tmp_path / "images").mkdir(exist_ok=True)
    (tmp_path / "videos").mkdir(exist_ok=True)
    return types.SimpleNamespace(
        images_dir=tmp_path / "images", videos_dir=tmp_path / "videos",
        media_dir=tmp_path / "media", wechat_author="作者")


def test_publish_article_draft_and_publish(tmp_path):
    cfg = _cfg(tmp_path)
    (cfg.images_dir / "pic.png").write_bytes(b"\x89PNG" + b"0" * 50)
    (cfg.images_dir / "cover.png").write_bytes(b"\x89PNG" + b"0" * 50)
    meta = {"title_candidates": ["原"], "title_cn": "中文标题",
            "body_md": "# H\n\n正文 **强**\n\n![](/images/pic.png)\n",
            "cover_image": "cover.png", "source_url": "https://x.com/a"}
    cli = _DummyClient()
    res = P.publish_article(cli, cfg, meta, mode="draft")
    assert res["ok"] and res["draft_media_id"] == "DRAFTX"
    assert res["title"] == "中文标题"
    art = cli.drafts[0][0]
    assert art["thumb_media_id"] == "COVER"
    assert "https://w/body-pic.png" in art["content"]
    assert "publish_id" not in res

    res2 = P.publish_article(cli, cfg, meta, mode="publish")
    assert res2["publish_id"] == "PUB1"


def test_publish_article_errors_without_cover(tmp_path):
    cfg = _cfg(tmp_path)
    meta = {"title_candidates": ["t"], "body_md": "no images here",
            "cover_image": None}
    res = P.publish_article(_DummyClient(), cfg, meta, mode="draft")
    # With the PIL fallback, publish now succeeds even without a real
    # cover — a placeholder 900x500 PNG is auto-generated.
    assert res["ok"] is True


def test_upload_video_missing(tmp_path):
    cfg = _cfg(tmp_path)
    res = P.upload_video(_DummyClient(), cfg, 7, {"title_candidates": ["t"],
                                                  "body_md": "x"})
    assert res["ok"] is False and "视频" in res["error"]


# ── 视频号 (Channels) caption ────────────────────────────────────────────────

class _JsonProvider:
    def __init__(self, payload):
        self.payload = payload

    def chat(self, messages, **k):
        return self.payload


def test_channels_caption_from_llm():
    from app.wechat.channels import build_channels_caption
    prov = _JsonProvider('{"title":"震撼短标题","description":"口语化简介。",'
                         '"hashtags":["AI","科技前沿"]}')
    meta = {"title_candidates": ["原"], "body_md": "正文", "topic": "AI"}
    cap = build_channels_caption(meta, prov)
    assert cap["title"] == "震撼短标题"
    assert "口语化简介" in cap["description"]
    assert cap["hashtags"] == ["#AI", "#科技前沿"]  # '#' auto-prefixed
    assert "#AI" in cap["caption"]


def test_channels_caption_fallback_without_llm():
    from app.wechat.channels import build_channels_caption
    meta = {"title_cn": "中文标题", "body_md": "# H\n\n" + "正文内容 " * 30,
            "topic": "科技"}
    cap = build_channels_caption(meta, None)
    assert cap["title"] == "中文标题"
    assert cap["description"]                    # derived from body
    assert cap["hashtags"] == ["#科技"]


# ═══════════════════════════════════════════════════════════════════
# _absolutize_media / _absolutize_body_md
# ═══════════════════════════════════════════════════════════════════

def test_absolutize_media_converts_relative_paths():
    from app.wechat.publish import _absolutize_media
    assert _absolutize_media("../../media/abc/pic.png") == "/media/abc/pic.png"
    assert _absolutize_media("../../images/logo.png") == "/images/logo.png"


def test_absolutize_media_passes_absolute_paths():
    from app.wechat.publish import _absolutize_media
    assert _absolutize_media("/media/abc/pic.png") == "/media/abc/pic.png"
    assert _absolutize_media("/images/logo.png") == "/images/logo.png"
    assert _absolutize_media("https://cdn.example.com/img.png") \
        == "https://cdn.example.com/img.png"


def test_absolutize_body_md_converts_all_images():
    from app.wechat.publish import _absolutize_body_md
    md = ("# Title\n\n"
          "![](../../media/abc/pic.png)\n\n"
          "Some text with ![alt](/images/logo.png) inline\n\n"
          "![](../../media/xyz/other.jpg \"title\")\n")
    out = _absolutize_body_md(md)
    assert "/media/abc/pic.png" in out
    assert "/images/logo.png" in out
    assert "/media/xyz/other.jpg" in out
    assert "../../media/" not in out


def test_absolutize_body_md_no_images():
    from app.wechat.publish import _absolutize_body_md
    md = "# Just a heading\n\nSome plain text with no images.\n"
    assert _absolutize_body_md(md) == md


def test_absolutize_body_md_http_images_unchanged():
    from app.wechat.publish import _absolutize_body_md
    md = "![alt](https://cdn.example.com/img.png)"
    assert _absolutize_body_md(md) == md
