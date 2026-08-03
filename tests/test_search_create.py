from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
import types

import pytest

from app.config import Config
from app.db import connect, init_db
from app.llm.providers.mock import MockProvider
from app.models import Article
from app.pipeline.search_create import (
    SearchCreateCancelled, SearchCreateOptions, expand_search_queries,
    rank_by_topic, run_search_create, scrape_search_hits)
from app.pipeline.synthesizer import (
    _image_manifest, _visual_instruction, expand_image_refs, referenced_images,
    synthesize)
from app.sources.web_search import (
    SearchHit, filter_hits_for_query, merge_search_hits, normalize_search_url,
    search_baidu_text, search_bing_text, search_ddgs_text, search_query)
from app.sources.dedup import dedup_near_content
from app.store import Store


def article(index: int, *, days_old: int = 1) -> Article:
    return Article(
        title=f"Article {index}",
        content_md=f"## Fact\nSource fact {index}. " * 30,
        url=f"https://example{index}.com/news",
        source_name=f"Source {index}",
        source_type="scrape",
        published_at=datetime.now(timezone.utc) - timedelta(days=days_old),
        images=[],
        raw_summary=None,
        fetched_at=datetime.now(timezone.utc),
        topic="AI",
        id=index,
    )


def test_search_url_and_cross_query_dedup():
    assert normalize_search_url(
        "HTTPS://Example.com/a/?utm_source=x#part") == "https://example.com/a"
    merged = merge_search_hits([
        [SearchHit("A", "https://example.com/a?utm_source=x")],
        [SearchHit("A2", "https://example.com/a#section"),
         SearchHit("B", "https://example.com/b")],
    ])
    assert [hit.url for hit in merged] == [
        "https://example.com/a", "https://example.com/b"]


def test_ddgs_failure_falls_back(monkeypatch):
    monkeypatch.setattr("app.sources.web_search.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "app.sources.web_search.search_ddgs_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(
        "app.sources.web_search.search_web",
        lambda *args, **kwargs: [
            "https://fallback.test/a?utm_campaign=x",
            "https://fallback.test/a",
        ])
    monkeypatch.setattr(
        "app.sources.web_search.search_bing_text",
        lambda *args, **kwargs: [])
    hits = search_query("topic", engines=["duckduckgo"])
    assert len(hits) == 2  # fallback preserves discovery output; merge removes it
    assert merge_search_hits([hits]) == [
        SearchHit("https://fallback.test/a?utm_campaign=x",
                  "https://fallback.test/a", "")]


def test_ddgs_uses_selected_search_engines(monkeypatch):
    captured = {}

    class FakeDDGS:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def text(self, query, **kwargs):
            captured.update(kwargs)
            return [{"title": "A", "href": "https://example.test/a"}]

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=FakeDDGS))
    hits = search_ddgs_text(
        "topic", engines=["duckduckgo", "google"], max_results=5,
        proxy="http://127.0.0.1:7890", timeout=7)
    assert hits[0].url == "https://example.test/a"
    assert captured["backend"] == "duckduckgo,google"
    assert captured["client"] == {
        "proxy": "http://127.0.0.1:7890", "timeout": 7}


def test_bing_result_short_circuits_blocked_engines(monkeypatch):
    expected = SearchHit("topic result", "https://example.test/bing")
    monkeypatch.setattr(
        "app.sources.web_search.search_bing_text",
        lambda *args, **kwargs: [expected])
    monkeypatch.setattr(
        "app.sources.web_search.search_ddgs_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("DDGS should not run after Bing succeeds")))
    assert search_query(
        "topic", region="cn-zh", engines=["bing", "duckduckgo"]) == [expected]


def test_irrelevant_bing_results_fall_through_to_next_engine(monkeypatch):
    calls = {"bing": 0, "ddgs": 0}

    def broad_bing(*args, **kwargs):
        calls["bing"] += 1
        return [SearchHit("儿童频道", "https://example.test/children")]

    def focused_ddgs(*args, **kwargs):
        calls["ddgs"] += 1
        return [SearchHit(
            "医生提醒：儿童做美甲可能损伤指甲",
            "https://example.test/manicure")]

    monkeypatch.setattr(
        "app.sources.web_search.search_bing_text", broad_bing)
    monkeypatch.setattr(
        "app.sources.web_search.search_ddgs_text", focused_ddgs)
    result = search_query(
        "儿童美甲 健康风险", region="cn-zh",
        engines=["bing", "duckduckgo"])
    assert result[0].url == "https://example.test/manicure"
    assert calls == {"bing": 1, "ddgs": 1}


def test_baidu_is_selectable_and_skips_ddgs_backends(monkeypatch):
    expected = SearchHit("远程办公 研究", "https://study.test/remote-work")
    monkeypatch.setattr(
        "app.sources.web_search.search_baidu_text",
        lambda *args, **kwargs: [expected])
    monkeypatch.setattr(
        "app.sources.web_search.search_ddgs_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("baidu must not be passed to DDGS")))
    monkeypatch.setattr(
        "app.sources.web_search.search_bing_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Bing should not run when only baidu is selected")))

    assert search_query(
        "远程办公 研究", region="cn-zh", engines=["baidu"]) == [expected]


def test_chinese_search_uses_baidu_after_selected_engines_fail(monkeypatch):
    expected = SearchHit(
        "医生提醒：儿童做美甲可能损伤指甲",
        "https://www.baidu.com/link?url=result")
    monkeypatch.setattr(
        "app.sources.web_search.search_bing_text", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "app.sources.web_search.search_ddgs_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("backend unavailable")))
    monkeypatch.setattr(
        "app.sources.web_search.search_baidu_text",
        lambda *args, **kwargs: [expected])
    monkeypatch.setattr("app.sources.web_search.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "app.sources.web_search.search_web",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("legacy fallback should not run")))

    assert search_query(
        "儿童美甲 健康风险", region="cn-zh",
        engines=["bing", "duckduckgo"]) == [expected]


def test_search_retries_three_times_before_success(monkeypatch):
    calls = {"count": 0}
    delays = []
    expected = SearchHit("topic recovered", "https://example.test/recovered")

    def flaky_bing(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] <= 3:
            raise TimeoutError("temporary timeout")
        return [expected]

    monkeypatch.setattr(
        "app.sources.web_search.search_bing_text", flaky_bing)
    monkeypatch.setattr(
        "app.sources.web_search.time.sleep", delays.append)
    assert search_query(
        "topic", region="cn-zh", engines=["bing"]) == [expected]
    assert calls["count"] == 4  # initial attempt + three retries
    assert delays == [0.4, 0.8, 1.6]


def test_bing_html_search_parses_results(monkeypatch):
    page = """
    <ol>
      <li class="b_algo"><h2><a href="https://news.test/a?utm_source=bing">
      Result A</a></h2><div><p>Useful summary.</p></div></li>
    </ol>
    """

    class Response:
        text = page

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url, params):
            assert url == "https://cn.bing.com/search"
            assert params["setlang"] == "zh-hans"
            return Response()

    monkeypatch.setattr("app.sources.web_search.httpx.Client", Client)
    hits = search_bing_text("topic", region="cn-zh")
    assert hits == [
        SearchHit("Result A", "https://news.test/a", "Useful summary.")]


def test_baidu_search_resolves_redirects_and_drops_ads(monkeypatch):
    page = """
    <div class="result"><h3><a href="http://www.baidu.com/link?url=real">
    远程办公 研究</a></h3><div class="c-abstract">一项长期跟踪研究。</div></div>
    <div class="result"><h3><a href="https://www.baidu.com/baidu.php?url=ad">
    推广链接</a></h3></div>
    <div class="result"><h3><a href="https://image.baidu.com/search/index">
    图片</a></h3></div>
    <div class="result"><h3><a href="http://www.baidu.com/link?url=broken">
    无法解析</a></h3></div>
    """

    class Response:
        content = page.encode()

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url, params):
            assert url == "https://www.baidu.com/s"
            assert params["wd"] == "远程办公"
            return Response()

        def head(self, url):
            if url.endswith("broken"):
                raise RuntimeError("redirect resolution failed")
            return types.SimpleNamespace(url="https://study.test/remote-work")

    monkeypatch.setattr("app.sources.web_search.httpx.Client", Client)
    assert search_baidu_text("远程办公") == [
        SearchHit("远程办公 研究", "https://study.test/remote-work",
                  "一项长期跟踪研究。"),
        SearchHit("无法解析", "http://www.baidu.com/link?url=broken", ""),
    ]


def test_query_expansion_parses_json_and_has_fallback():
    provider = MockProvider(['{"queries":["AI research","AI news","AI news"]}'])
    assert expand_search_queries("AI", "en", provider) == [
        "AI", "AI research", "AI news"]
    fallback = MockProvider(["not json"])
    assert expand_search_queries("AI", "zh", fallback)[0] == "AI"


def test_conversational_topic_falls_back_to_search_terms():
    provider = MockProvider(["not json"])
    assert expand_search_queries("长期远程办公，真的好吗", "zh", provider) == [
        "长期远程办公", "长期远程办公 分析", "长期远程办公 专家 建议"]
    keeps_terms = MockProvider(['{"queries":["远程办公 效率 研究"]}'])
    assert expand_search_queries(
        "长期远程办公，究竟好不好？", "zh", keeps_terms) == [
            "长期远程办公", "远程办公 效率 研究"]


def test_search_hit_filter_rejects_generic_partial_chinese_matches():
    rows = [
        SearchHit("儿童频道", "https://example.test/children"),
        SearchHit("医生提醒：儿童做美甲可能损伤指甲",
                  "https://example.test/manicure"),
    ]
    assert filter_hits_for_query(rows, "儿童美甲 健康风险") == [rows[1]]


def test_scrape_partial_failure_and_cancel(monkeypatch):
    def fake_scrape(url, source_name, **kwargs):
        if url.endswith("/bad"):
            raise RuntimeError("failed")
        return article(1)

    monkeypatch.setattr(
        "app.pipeline.search_create.scrape_single", fake_scrape)
    options = SearchCreateOptions(topic="AI", ref_count=5, workers=2)
    rows = scrape_search_hits([
        SearchHit("good", "https://one.test/good"),
        SearchHit("bad", "https://two.test/bad"),
    ], options)
    assert len(rows) == 1

    with pytest.raises(SearchCreateCancelled):
        scrape_search_hits(
            [SearchHit("good", "https://one.test/good")],
            options, should_stop=lambda: True)


def test_rank_by_topic_uses_scores():
    rows = [article(1), article(2), article(3)]
    provider = MockProvider([
        '{"scores":[{"index":0,"score":2},{"index":1,"score":99},'
        '{"index":2,"score":40}]}'])
    assert [row.id for row in rank_by_topic(rows, "AI", provider, 2)] == [2, 3]


def test_rank_by_topic_falls_back_to_lexical_and_diversifies_sources():
    rows = [article(1), article(2), article(3)]
    rows[0].title = "Unrelated"
    rows[0].source_name = "same.example"
    rows[1].title = "Quantum computing breakthrough"
    rows[1].source_name = "same.example"
    rows[2].title = "Quantum computing industry analysis"
    rows[2].source_name = "other.example"
    ranked = rank_by_topic(
        rows, "quantum computing", MockProvider(["invalid"]), 2)
    assert [row.id for row in ranked] == [2, 3]


def test_near_content_dedup_keeps_richer_copy():
    first = article(1)
    second = article(2)
    first.title = second.title = "Same report"
    first.content_md = "shared research result " * 80
    second.content_md = first.content_md + "additional detail " * 10
    unique = article(3)
    unique.content_md = "completely different subject " * 30
    result = dedup_near_content([first, second, unique])
    assert [row.id for row in result] == [2, 3]


def test_synthesize_retries_cites_sources_and_fact_checks():
    provider = MockProvider([
        "invalid",
        '{"title_candidates":["标题一","标题二"],'
        '"body_md":"## 进展\\n事实一 [1]，事实二 [2]。",'
        '"citations":[{"claim":"事实一","source_indexes":[1]}]}',
        '{"flagged_claims":["事实二缺少直接证据"]}',
    ])
    result = synthesize("AI", [article(1), article(2)], provider)
    assert result.title_candidates[0] == "标题一"
    assert "事实一 [1]" in result.body_md
    assert "## 参考文献" in result.body_md
    assert "1. Article 1 — Source 1" in result.body_md
    assert result.citations[0]["source_indexes"] == [1]
    assert result.flagged_claims == ["事实二缺少直接证据"]


def test_synthesize_body_carries_no_outbound_links():
    """公众号/头条把站外链接判为引流，正文里不能出现任何 URL。"""
    provider = MockProvider([
        '{"title_candidates":["标题"],'
        '"body_md":"## 进展\\n见 [原文](https://example1.com/news) 与 [1]。\\n'
        '![图](../../media/cover.jpg)",'
        '"citations":[]}',
        '{"flagged_claims":[]}',
    ])
    result = synthesize("AI", [article(1), article(2)], provider)
    assert "http://" not in result.body_md
    assert "https://" not in result.body_md
    assert "见 原文 与 [1]。" in result.body_md
    assert "![图](../../media/cover.jpg)" in result.body_md


def test_synthesize_requires_multiple_sources():
    with pytest.raises(ValueError, match="至少需要 2"):
        synthesize("AI", [article(1)], MockProvider())


def test_synthesize_applies_requested_output_language():
    class CaptureProvider(MockProvider):
        def __init__(self):
            super().__init__([
                '{"title_candidates":["Title"],"body_md":"## Body\\nFact [1]."}',
                '{"flagged_claims":[]}',
            ])
            self.calls = []

        def chat(self, messages, **opts):
            self.calls.append(messages)
            return super().chat(messages, **opts)

    provider = CaptureProvider()
    synthesize("AI", [article(1), article(2)], provider, lang="en")
    assert "输出语言：English" in provider.calls[0][-1].content


def test_synthesize_uses_content_style_settings_and_examples():
    class CaptureProvider(MockProvider):
        def __init__(self):
            super().__init__([
                '{"title_candidates":["标题"],'
                '"body_md":"## 正文\\n事实 [1]。\\n\\n'
                '**SEO 标签**：#AI #智能体"}',
                '{"flagged_claims":[]}',
            ])
            self.calls = []

        def chat(self, messages, **opts):
            self.calls.append(messages)
            return super().chat(messages, **opts)

    style = types.SimpleNamespace(
        prompt="使用克制、直接的编辑语气。",
        instruction="### 写作风格\n短句、短段，先给结论。\n\n### 通用要求\n只写事实。",
        analysis={},
        examples=[{"title": "样例", "content": "这是只用于学习节奏的样例。"}],
    )
    provider = CaptureProvider()
    result = synthesize(
        "AI", [article(1), article(2)], provider, style=style,
        promotion_footer="关注 Media Agent", seo_tags_enabled=True)

    system = provider.calls[0][0].content
    prompt = provider.calls[0][-1].content
    assert "使用克制、直接的编辑语气" in system
    assert "短句、短段，先给结论" in prompt
    assert "目标风格示例" in prompt
    assert "只用于模仿表达方式" in prompt
    assert "关注 Media Agent" in prompt
    assert "**标签**：#关键词1" in prompt
    assert "**标签**：#AI #智能体" in result.body_md


class CapturingProvider(MockProvider):
    def __init__(self):
        super().__init__([
            '{"title_candidates":["标题"],"body_md":"## 正文\\n事实 [1]。"}',
            '{"flagged_claims":[]}',
        ])
        self.calls = []

    def chat(self, messages, **opts):
        self.calls.append(messages)
        return super().chat(messages, **opts)


def test_depth_positioning_reaches_the_writer():
    provider = CapturingProvider()
    synthesize("AI", [article(1), article(2)], provider, depth="advanced")
    prompt = provider.calls[0][-1].content
    assert "### 内容深度" in prompt
    assert "面向从业者" in prompt
    assert "不少于 3500 字" in prompt
    # The complaint this answers: a page of terminology nobody can use.
    assert "只报名字不解释，等于没写" in prompt


def test_without_a_depth_nothing_is_claimed_about_it():
    provider = CapturingProvider()
    synthesize("AI", [article(1), article(2)], provider)
    assert "### 内容深度" not in provider.calls[0][-1].content


def test_deep_writing_reads_further_into_each_source():
    """The mechanisms worth writing about sit past a source's opening."""
    rows = [article(1), article(2)]
    for row in rows:
        row.content_md = "开头。" + "细节 " * 900 + "末尾的机制说明。"

    shallow = CapturingProvider()
    synthesize("AI", rows, shallow)
    deep = CapturingProvider()
    synthesize("AI", rows, deep, depth="advanced")

    assert "末尾的机制说明" not in shallow.calls[0][-1].content
    assert "末尾的机制说明" in deep.calls[0][-1].content


def test_image_manifest_lists_http_images_across_articles_in_order():
    rows = [article(1), article(2)]
    rows[0].images = ["https://img.cdn/a.png", "/media/keep.png"]
    rows[1].images = ["https://img.cdn/b.jpg", "ftp://x/y.png"]
    assert _image_manifest(rows) == [
        "https://img.cdn/a.png", "https://img.cdn/b.jpg"]


def test_referenced_images_returns_used_in_range_deduped():
    rows = [article(1), article(2)]
    rows[0].images = ["https://img.cdn/a.png", "https://img.cdn/b.png"]
    rows[1].images = ["https://img.cdn/c.png"]
    body = "见 [[IMG:0]] 与 [[IMG:0]]，还有 [[IMG:2]] 和越界 [[IMG:9]]。"
    assert referenced_images(body, rows) == [
        "https://img.cdn/a.png", "https://img.cdn/c.png"]


def test_expand_image_refs_drops_what_it_cannot_resolve():
    """占位符留在正文里就会变成读者看到的「[[IMG:1]]」，宁可没有图。"""
    rows = [article(1), article(2)]
    rows[0].images = ["https://img.cdn/a.png", "https://img.cdn/b.png"]
    body = "[[IMG:0]] 与下载失败的 [[IMG:1]]，越界 [[IMG:9]]。"
    local_map = {"https://img.cdn/a.png": "/media/hash/dl-1.png"}
    assert expand_image_refs(body, rows, local_map) == (
        "![](/media/hash/dl-1.png) 与下载失败的 ，越界 。")


def test_expand_image_refs_does_not_leave_a_hole_in_the_layout():
    rows = [article(1)]
    rows[0].images = ["https://img.cdn/a.png"]
    body = "上一段。\n\n[[IMG:7]]\n\n下一段。"
    assert expand_image_refs(body, rows, {}) == "上一段。\n\n下一段。"


def test_visual_instruction_always_asks_for_mermaid_fence():
    assert "```mermaid" in _visual_instruction(True)
    assert "```mermaid" in _visual_instruction(False)


def test_visual_instruction_teaches_img_refs_only_when_images_exist():
    assert "[[IMG:N]]" in _visual_instruction(True)
    assert "[[IMG:N]]" not in _visual_instruction(False)


def test_synthesize_prompt_teaches_mermaid_and_img_refs():
    class CaptureProvider(MockProvider):
        def __init__(self):
            super().__init__([
                '{"title_candidates":["标题"],"body_md":"## 正文\\n事实 [1]。"}',
                '{"flagged_claims":[]}',
            ])
            self.calls = []

        def chat(self, messages, **opts):
            self.calls.append(messages)
            return super().chat(messages, **opts)

    rows = [article(1), article(2)]
    rows[0].images = ["https://img.cdn/a.png", "https://img.cdn/b.png"]
    rows[1].images = ["https://img.cdn/c.png"]
    provider = CaptureProvider()
    synthesize("AI", rows, provider)
    prompt = provider.calls[0][-1].content
    assert "```mermaid" in prompt
    assert "[[IMG:N]]" in prompt
    assert "配图（" in prompt
    assert "- [[IMG:0]]（https://img.cdn/a.png）" in prompt


def test_the_prompt_numbers_images_the_way_they_resolve():
    """提示词里的编号和 [[IMG:N]] 的解析必须是同一套，否则会配错图。"""
    class CaptureProvider(MockProvider):
        def __init__(self):
            super().__init__([
                '{"title_candidates":["标题"],"body_md":"## 正文\\n事实 [1]。"}',
                '{"flagged_claims":[]}',
            ])
            self.calls = []

        def chat(self, messages, **opts):
            self.calls.append(messages)
            return super().chat(messages, **opts)

    rows = [article(1), article(2)]
    rows[0].images = ["https://img.cdn/a.png", "https://img.cdn/b.png"]
    rows[1].images = ["https://img.cdn/c.png", "https://img.cdn/d.png"]
    provider = CaptureProvider()
    synthesize("AI", rows, provider)
    prompt = provider.calls[0][-1].content

    # The second source used to restart at 图1 while the manifest kept
    # counting, so its images resolved to the first source's pictures.
    manifest = _image_manifest(rows)
    for n, url in enumerate(manifest):
        assert f"- [[IMG:{n}]]（{url}）" in prompt
    assert manifest[2] == "https://img.cdn/c.png"
    assert expand_image_refs(
        "[[IMG:2]]", rows, {"https://img.cdn/c.png": "/media/h/c.png"}
    ) == "![](/media/h/c.png)"


def test_the_pages_own_logos_and_buttons_are_not_offered_as_pictures():
    """来源的图片清单是整页 <img>，评论图标和站点 logo 混在照片里。"""
    rows = [article(1)]
    rows[0].images = [
        "https://mbdp01.bdstatic.com/static/landing-pc/img/logo_top.79f.png",
        "https://pics1.baidu.com/feed/8c1001e93901213f103df46ec613.jpeg",
        "https://mbdp01.bdstatic.com/static/landing-pc/img/icon_share.409.png",
        "https://www.digitalchina.gov.cn/images/250311szfh_ewm.jpg",
    ]

    assert _image_manifest(rows) == [
        "https://pics1.baidu.com/feed/8c1001e93901213f103df46ec613.jpeg"]


def test_the_sources_own_image_markers_never_reach_the_model():
    """归档正文自带 [[IMG:N]]（抓取时插入），模型照抄就会写出配不上的记号。"""
    class CaptureProvider(MockProvider):
        def __init__(self):
            super().__init__([
                '{"title_candidates":["标题"],"body_md":"## 正文\\n事实 [1]。"}',
                '{"flagged_claims":[]}',
            ])
            self.calls = []

        def chat(self, messages, **opts):
            self.calls.append(messages)
            return super().chat(messages, **opts)

    rows = [article(1), article(2)]
    rows[0].content_md = "开头。\n\n[[IMG:0]]\n\n正文继续。[[VIDEO:1]]"
    provider = CaptureProvider()
    synthesize("AI", rows, provider)
    prompt = provider.calls[0][-1].content
    assert "[[IMG:0]]" not in prompt
    assert "[[VIDEO:1]]" not in prompt
    assert "正文继续。" in prompt


def test_a_source_with_many_images_does_not_shift_the_later_ones():
    """提示词按每源 12 张截断，清单必须照做，否则后面的编号全错位。"""
    rows = [article(1), article(2)]
    rows[0].images = [f"https://img.cdn/a{i}.png" for i in range(20)]
    rows[1].images = ["https://img.cdn/b.png"]
    manifest = _image_manifest(rows)
    assert len(manifest) == 13
    assert manifest[12] == "https://img.cdn/b.png"


def test_synthesize_prompt_without_images_omits_img_refs():
    class CaptureProvider(MockProvider):
        def __init__(self):
            super().__init__([
                '{"title_candidates":["标题"],"body_md":"## 正文\\n事实 [1]。"}',
                '{"flagged_claims":[]}',
            ])
            self.calls = []

        def chat(self, messages, **opts):
            self.calls.append(messages)
            return super().chat(messages, **opts)

    provider = CaptureProvider()
    synthesize("AI", [article(1), article(2)], provider)
    prompt = provider.calls[0][-1].content
    assert "```mermaid" in prompt
    assert "[[IMG:N]]" not in prompt
    assert "配图：" not in prompt


def test_run_search_create_mock_end_to_end(tmp_path, monkeypatch):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    conn = connect(config.db_path)
    init_db(conn)
    store = Store(conn, config)
    rows = [article(1, days_old=365), article(2, days_old=365)]
    monkeypatch.setattr(
        "app.pipeline.search_create.search_queries",
        lambda *args, **kwargs: [
            SearchHit("one", rows[0].url), SearchHit("two", rows[1].url)])
    monkeypatch.setattr(
        "app.pipeline.search_create.scrape_search_hits",
        lambda *args, **kwargs: rows)
    monkeypatch.setattr("app.store._web_image_for_title", lambda *args: None)
    provider = MockProvider([
        '{"queries":["AI research"]}',
        "[0,1]",
        '{"scores":[{"index":0,"score":90},{"index":1,"score":80}]}',
        '{"title_candidates":["综合标题"],'
        '"body_md":"## 正文\\n最佳事实 [1] 和事实 [2]。",'
        '"citations":[{"claim":"事实","source_indexes":[1,2]}]}',
        '{"flagged_claims":[]}',
    ])
    events = []
    result = run_search_create(
        store, provider,
        SearchCreateOptions(topic="AI", time_range_days=30, ref_count=5),
        sensitive_words={"最佳"},
        progress=events.append)
    assert result["draft_id"]
    assert events[-1]["stage"] == "done"
    meta = store.read_draft_body(result["draft_id"])
    assert meta["origin"] == "search_create"
    assert len(meta["sources"]) == 2
    assert "最佳" not in meta["body_md"]
    assert meta["sensitive_hits"] == ["最佳×1"]
    assert result["stats"]["time_range_relaxed"] is True
    assert any("自动扩大到不限时间" in event["detail"] for event in events)


class _Resp:
    content = b"\x89PNG\r\n\x1a\nfake"
    headers = {"content-type": "image/png"}

    def raise_for_status(self):
        ...


def _wire_search_e2e(monkeypatch, rows):
    monkeypatch.setattr(
        "app.pipeline.search_create.search_queries",
        lambda *args, **kwargs: [
            SearchHit("one", rows[0].url), SearchHit("two", rows[1].url)])
    monkeypatch.setattr(
        "app.pipeline.search_create.scrape_search_hits",
        lambda *args, **kwargs: rows)
    monkeypatch.setattr("app.store._web_image_for_title", lambda *args: None)


def _img_search_script():
    return [
        '{"queries":["AI research"]}',
        "[0,1]",
        '{"scores":[{"index":0,"score":90},{"index":1,"score":80}]}',
        '{"title_candidates":["综合标题"],'
        '"body_md":"## 正文\\n图 [[IMG:0]] 与 [[IMG:2]]，越界 [[IMG:9]]。\\n'
        '事实 [1] 和事实 [2]。",'
        '"citations":[{"claim":"事实","source_indexes":[1,2]}]}',
        '{"flagged_claims":[]}',
    ]


def test_run_search_create_localizes_referenced_images(tmp_path, monkeypatch):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    conn = connect(config.db_path)
    init_db(conn)
    store = Store(conn, config)
    rows = [article(1, days_old=365), article(2, days_old=365)]
    rows[0].images = ["https://img.cdn/a.png", "https://img.cdn/b.png"]
    rows[1].images = ["https://img.cdn/c.png"]
    _wire_search_e2e(monkeypatch, rows)
    monkeypatch.setattr("app.pipeline.localize.httpx.get",
                        lambda *a, **k: _Resp())

    result = run_search_create(
        store, MockProvider(_img_search_script()),
        SearchCreateOptions(topic="AI", time_range_days=30, ref_count=5),
        config=config)

    assert result["draft_id"]
    body = store.read_draft_body(result["draft_id"])["body_md"]
    assert "[[IMG:0]]" not in body
    assert "[[IMG:2]]" not in body
    assert "[[IMG:9]]" not in body    # made-up number, dropped rather than shown
    assert body.count("![](/media/") == 2
    # the two referenced images really landed under data/media/<hash>/
    files = [p for p in config.media_dir.glob("*/*") if p.is_file()]
    assert len(files) == 2


def test_run_search_create_without_config_still_clears_img_placeholders(
        tmp_path, monkeypatch):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    conn = connect(config.db_path)
    init_db(conn)
    store = Store(conn, config)
    rows = [article(1, days_old=365), article(2, days_old=365)]
    rows[0].images = ["https://img.cdn/a.png", "https://img.cdn/b.png"]
    rows[1].images = ["https://img.cdn/c.png"]
    _wire_search_e2e(monkeypatch, rows)

    result = run_search_create(
        store, MockProvider(_img_search_script()),
        SearchCreateOptions(topic="AI", time_range_days=30, ref_count=5))

    assert result["draft_id"]
    body = store.read_draft_body(result["draft_id"])["body_md"]
    assert "[[IMG:" not in body
    assert "![](/media/" not in body
