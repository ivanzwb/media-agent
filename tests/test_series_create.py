from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from app.config import Config
from app.db import connect, init_db
from app.models import Article
from app.pipeline.search_create import SearchCreateCancelled
from app.pipeline.series_create import (
    SeriesCreateOptions, _series_nav_note, generate_knowledge_map,
    generate_series_outline, plan_series, probe_topic, retry_chapter,
    run_series_chapters, run_series_create)
from app.sources.web_search import SearchHit
from app.store import Store


def article(index: int) -> Article:
    return Article(
        title=f"Article {index}",
        content_md=f"## Fact\nSource fact {index}. " * 30,
        url=f"https://example{index}.test/page",
        source_name=f"Source {index}",
        source_type="scrape",
        published_at=datetime.now(timezone.utc),
        images=[],
        raw_summary=None,
        fetched_at=datetime.now(timezone.utc),
        topic="RL",
    )


OUTLINE = (
    '{"chapters":['
    '{"title":"第一章","scope":"打底","search_queries":["q1"],'
    '"prerequisites":[]},'
    '{"title":"第二章","scope":"展开","search_queries":["q2"],'
    '"prerequisites":["第一章"]},'
    '{"title":"第三章","scope":"收束","search_queries":["q3"],'
    '"prerequisites":["第二章"]}]}'
)


KNOWLEDGE_MAP = (
    'graph TD\n'
    '    N1["强化学习"]\n'
    '    N2["基础概念"]\n'
    '    N3["马尔可夫决策过程"]\n'
    '    N4["核心算法"]\n'
    '    N5["时序差分"]\n'
    '    N1 --> N2\n'
    '    N2 --> N3\n'
    '    N1 --> N4\n'
    '    N4 --> N5'
)


class ScriptedProvider:
    """Answers by prompt shape, so chapter count never shifts the script."""

    def __init__(self, outline: str = OUTLINE, fixes: str = '{"fixes":[]}'):
        self.outline = outline
        self.fixes = fixes
        self.prompts: list[str] = []

    def chat(self, messages, **opts) -> str:
        prompt = messages[-1].content
        self.prompts.append(prompt)
        if "事实核查员" in prompt:
            return '{"flagged_claims":[]}'
        if "梳理知识主题" in prompt:
            return f"这是脉络：\n{KNOWLEDGE_MAP}\n\n以上仅供参考。"
        if '"fixes"' in prompt:
            return self.fixes
        if "系列文章提纲" in prompt:
            return self.outline
        if "title_candidates" in prompt:
            index = sum(1 for item in self.prompts if "title_candidates" in item)
            return (
                f'{{"title_candidates":["成稿 {index}"],'
                f'"body_md":"## 小节\\n这一章的开头段落。\\n\\n正文 [1] 与 [2]。",'
                '"citations":[]}'
            )
        if '"scores"' in prompt:
            return '{"scores":[{"index":0,"score":90},{"index":1,"score":80}]}'
        return "[0,1,2,3,4,5,6,7,8,9]"


def make_store(tmp_path) -> Store:
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    conn = connect(config.db_path)
    init_db(conn)
    return Store(conn, config)


def options(**overrides) -> SeriesCreateOptions:
    values = {
        "topic": "强化学习",
        "parts": 3,
        "ref_count": 5,
        "engines": ["duckduckgo"],
        "workers": 2,
    }
    values.update(overrides)
    return SeriesCreateOptions(**values)


def wire(monkeypatch, *, hits_for, articles_by_url, scrape_calls=None,
         search_calls=None):
    """Route search and scrape through in-memory fixtures."""
    def fake_search(queries, opts, **kwargs):
        if search_calls is not None:
            search_calls.append(list(queries))
        collected: list[SearchHit] = []
        for query in queries:
            collected.extend(hits_for(query))
        seen, unique = set(), []
        for item in collected:
            if item.url not in seen:
                seen.add(item.url)
                unique.append(item)
        return unique

    def fake_scrape(hits, opts, **kwargs):
        if scrape_calls is not None:
            scrape_calls.append([item.url for item in hits])
        return [articles_by_url[item.url] for item in hits
                if item.url in articles_by_url]

    monkeypatch.setattr(
        "app.pipeline.search_create.search_queries", fake_search)
    monkeypatch.setattr(
        "app.pipeline.series_create.search_queries", fake_search)
    monkeypatch.setattr(
        "app.pipeline.search_create.scrape_search_hits", fake_scrape)
    monkeypatch.setattr("app.store._web_image_for_title", lambda *args: None)


def pool(count: int) -> tuple[list[SearchHit], dict[str, Article]]:
    articles = [article(index) for index in range(1, count + 1)]
    hits = [SearchHit(row.title, row.url, f"摘要 {index}")
            for index, row in enumerate(articles, 1)]
    return hits, {row.url: row for row in articles}


def test_probe_topic_reads_titles_without_fetching_pages(monkeypatch):
    hits = [
        SearchHit("入门指南", "https://a.test/one", "  讲清楚了  基础  "),
        SearchHit("第二篇", "https://a.test/two", "同域第二篇"),
        SearchHit("第三篇", "https://a.test/three", "同域第三篇，应被丢弃"),
        SearchHit("另一站", "https://b.test/one", "另一个域名"),
    ]
    scrape_calls: list[list[str]] = []
    wire(monkeypatch, hits_for=lambda query: hits, articles_by_url={},
         scrape_calls=scrape_calls)

    stats: dict = {}
    grounding = probe_topic(options(), stats=stats)

    assert scrape_calls == []  # grounding must never fetch a page
    assert "入门指南：讲清楚了 基础" in grounding
    assert "另一站：另一个域名" in grounding
    assert "第三篇" not in grounding  # capped at two hits per domain
    assert stats["grounding_hits"] == 3


def test_outline_falls_back_to_topic_terms_when_queries_missing():
    provider = ScriptedProvider(
        '{"chapters":[{"title":"背景","scope":"","prerequisites":"入门"}]}')
    chapters = generate_series_outline(
        "强化学习", "", provider, parts=1, depth="intermediate")
    assert chapters[0]["search_queries"] == ["强化学习 背景"]
    assert chapters[0]["prerequisites"] == ["入门"]


def test_outline_rejects_unusable_model_output():
    provider = ScriptedProvider('{"chapters":[]}')
    with pytest.raises(ValueError, match="系列提纲生成失败"):
        generate_series_outline(
            "强化学习", "", provider, parts=3, depth="intermediate")


def test_series_writes_linked_drafts_in_order(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)

    result = run_series_create(store, ScriptedProvider(), options())

    assert result["status"] == "done"
    assert len(result["draft_ids"]) == 3
    drafts = store.get_series_drafts(result["series_id"])
    assert [row["series_order"] for row in drafts] == [1, 2, 3]
    assert {row["origin"] for row in drafts} == {"series"}
    chapters = store.list_chapters(result["series_id"])
    assert [row["status"] for row in chapters] == ["done"] * 3
    assert all(row["draft_id"] for row in chapters)
    assert store.get_series(result["series_id"])["status"] == "done"

    body = store.read_draft_body(drafts[0]["id"])
    assert body["series_part_label"] == "第 1 章"
    assert body["search_meta"]["chapter"] == "第一章"


def test_chapters_never_reuse_an_earlier_chapters_sources(
        tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    scrape_calls: list[list[str]] = []
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url, scrape_calls=scrape_calls)

    result = run_series_create(store, ScriptedProvider(), options())

    drafts = store.get_series_drafts(result["series_id"])
    per_chapter = [
        {source["url"]
         for source in store.read_draft_body(row["id"])["sources"]}
        for row in drafts]
    assert sum(len(item) for item in per_chapter) == len(set().union(*per_chapter))
    # Reserved sources are dropped before the fetch, so the saving is real.
    # Pages chapter 1 merely looked at stay available: a page it ranked low is
    # not necessarily a poor fit for a later chapter.
    assert not per_chapter[0] & set(scrape_calls[1])


def test_starved_chapter_fails_alone_and_series_reports_partial(
        tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    # The middle chapter is the narrow one: nothing matches its terms, and the
    # widened retry finds nothing either.
    wire(monkeypatch,
         hits_for=lambda query: [] if "q2" in query or "第二章" in query
         else hits,
         articles_by_url=articles_by_url)

    result = run_series_create(store, ScriptedProvider(), options())

    assert result["status"] == "partial"
    assert result["stats"]["chapters_done"] == 2
    assert result["stats"]["chapters_failed"] == 1
    chapters = store.list_chapters(result["series_id"])
    assert [row["status"] for row in chapters] == ["done", "failed", "done"]
    assert "不足 2 篇" in chapters[1]["error"]
    assert len(store.get_series_drafts(result["series_id"])) == 2
    assert store.get_series(result["series_id"])["status"] == "partial"


def test_widened_retry_rescues_a_chapter_the_outline_mistermed(
        tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch,
         hits_for=lambda query: hits if "强化学习" in query else [],
         articles_by_url=articles_by_url)

    result = run_series_create(store, ScriptedProvider(), options(parts=3))

    # None of the outline's own terms match; only the topic-widened retry does.
    assert result["stats"]["chapters_done"] == 3
    assert result["status"] == "done"


def test_cancel_keeps_the_chapters_already_written(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)
    def should_stop() -> bool:
        """Cancel as soon as the first chapter is on disk."""
        series = store.latest_series()
        return bool(series) and any(
            row["status"] == "done" for row in store.list_chapters(series["id"]))

    with pytest.raises(SearchCreateCancelled):
        run_series_create(
            store, ScriptedProvider(), options(), should_stop=should_stop)

    series = store.latest_series()
    assert series["status"] == "cancelled"
    statuses = [row["status"] for row in store.list_chapters(series["id"])]
    assert "done" in statuses
    assert "cancelled" in statuses
    assert store.get_series_drafts(series["id"])  # finished work survives


def test_later_chapters_receive_earlier_chapter_summaries(
        tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)
    provider = ScriptedProvider()

    run_series_create(store, provider, options())

    writes = [item for item in provider.prompts if "title_candidates" in item]
    assert "系列上下文" not in writes[0]
    assert "第 1 章《第一章》：这一章的开头段落。" in writes[1]
    assert "第 2 章《第二章》" in writes[2]


def test_knowledge_map_is_stored_and_shapes_the_outline(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)
    provider = ScriptedProvider()

    result = run_series_create(store, provider, options())

    stored = store.get_series(result["series_id"])["knowledge_map"]
    assert stored == KNOWLEDGE_MAP  # prose around the list is stripped
    outline_prompt = next(
        item for item in provider.prompts if "系列文章提纲" in item)
    assert "马尔可夫决策过程" in outline_prompt


def test_preflight_rewrites_terms_that_find_nothing(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    # The outline's own terms are dead; only the repaired ones match.
    wire(monkeypatch,
         hits_for=lambda query: hits if "修好的词" in query else [],
         articles_by_url=articles_by_url)
    provider = ScriptedProvider(fixes=(
        '{"fixes":[{"index":1,"search_queries":["修好的词 一"]},'
        '{"index":2,"search_queries":["修好的词 二"]},'
        '{"index":3,"search_queries":["修好的词 三"]}]}'))

    result = run_series_create(store, provider, options())

    assert result["status"] == "done"
    chapters = store.list_chapters(result["series_id"])
    assert [json.loads(row["search_queries"]) for row in chapters] == [
        ["修好的词 一"], ["修好的词 二"], ["修好的词 三"]]
    assert result["stats"]["preflight_thin"] == []


def test_preflight_reports_chapters_it_could_not_rescue(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch,
         hits_for=lambda query: [] if "q2" in query or "第二章" in query
         else hits,
         articles_by_url=articles_by_url)

    result = run_series_create(store, ScriptedProvider(), options())

    # The warning arrives during the outline stage, not ten minutes later.
    assert result["stats"]["preflight_thin"] == ["第二章"]
    assert result["status"] == "partial"


def test_preflight_hits_seed_research_instead_of_searching_twice(
        tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    search_calls: list[list[str]] = []
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url, search_calls=search_calls)

    run_series_create(store, ScriptedProvider(), options())

    # One grounding pass plus one probe per chapter — the research stage adds
    # none, because it reuses what the probe already found.
    assert len(search_calls) == 4
    assert search_calls[1:] == [["q1"], ["q2"], ["q3"]]


def test_options_reject_out_of_range_part_counts():
    with pytest.raises(ValueError, match="章节数必须在"):
        options(parts=2).validate()
    with pytest.raises(ValueError, match="章节数必须在"):
        options(parts=11).validate()


def test_chapter_search_ignores_the_recency_window():
    """Knowledge series cover settled material, not the last 30 days."""
    assert options().chapter_options().time_range_days is None


def test_the_map_is_rebuilt_into_a_diagram_that_parses():
    """Whatever the model calls its nodes must not reach the renderer."""
    provider = ScriptedProvider()
    provider.chat = lambda messages, **opts: (  # type: ignore[assignment]
        "```mermaid\n"
        "flowchart LR\n"
        "  强化学习[强化学习 (RL)] -->|包含| 基础(基础概念)\n"
        "  基础 --> MDP{马尔可夫决策过程};\n"
        "  强化学习 --> 基础\n"
        "```\n"
        "希望这张图对你有帮助。")

    diagram = generate_knowledge_map("强化学习", "", provider,
                                     depth="intermediate")

    assert diagram.splitlines() == [
        "graph TD",
        '    N1["强化学习 RL"]',      # the parentheses that would break it
        '    N2["基础概念"]',
        '    N3["马尔可夫决策过程"]',
        "    N1 --> N2",
        "    N2 --> N3",             # the repeated edge appears once
    ]


def test_an_unusable_map_is_dropped_rather_than_shown_broken():
    provider = ScriptedProvider()
    provider.chat = lambda messages, **opts: (  # type: ignore[assignment]
        "抱歉，我无法为这个主题绘制脉络图。")
    assert generate_knowledge_map(
        "强化学习", "", provider, depth="intermediate") == ""


def test_each_chapter_points_at_its_neighbours(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)

    result = run_series_create(store, ScriptedProvider(), options())

    bodies = [store.read_draft_body(row["id"])["body_md"]
              for row in store.get_series_drafts(result["series_id"])]
    assert "本文是《强化学习》系列第 1 篇，共 3 篇。" in bodies[0]
    assert "上一篇" not in bodies[0]  # nothing precedes the opener
    assert "下一篇《第二章》" in bodies[0]
    assert "上一篇《第一章》　下一篇《第三章》" in bodies[1]
    # The outline named chapter one as a prerequisite, so it is named by number.
    assert "建议先读：第 1 篇《第一章》" in bodies[1]
    assert "下一篇" not in bodies[2]


def test_the_navigation_note_stays_out_of_the_chapter_summary(
        tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)

    result = run_series_create(store, ScriptedProvider(), options())

    chapters = store.list_chapters(result["series_id"])
    assert chapters[0]["summary"] == "这一章的开头段落。"


def test_an_english_series_reads_as_parts():
    note = _series_nav_note(
        "Reinforcement Learning", 2, ["Basics", "Value", "Policy"],
        ["Basics", "linear algebra"], "en")
    assert 'Part 2 of 3 in the "Reinforcement Learning" series.' in note
    assert 'Previous: "Basics" | Next: "Policy"' in note
    assert 'Read first: Part 1 "Basics"; linear algebra' in note


def test_a_lone_chapter_gets_no_navigation():
    assert _series_nav_note("话题", 1, ["只有一章"], [], "zh") == ""


def test_plan_stops_before_writing_and_records_what_it_found(
        tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch,
         hits_for=lambda query: [] if "q2" in query or "第二章" in query
         else hits,
         articles_by_url=articles_by_url)

    series_id, chapters, _ = plan_series(
        store, ScriptedProvider(), options(), status="planned")

    assert store.get_series(series_id)["status"] == "planned"
    rows = store.list_chapters(series_id)
    assert [row["status"] for row in rows] == ["pending"] * 3
    assert list(store.get_series_drafts(series_id)) == []
    # The per-chapter counts are what makes the warning actionable.
    assert [row["preflight_hits"] for row in rows] == [12, 0, 12]
    assert len(chapters) == 3


def test_a_reviewed_outline_is_what_gets_written(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)
    series_id, _, _ = plan_series(
        store, ScriptedProvider(), options(), status="planned")

    store.replace_chapters(series_id, [
        {"title": "第三章", "scope": "收束", "search_queries": ["q3"],
         "prerequisites": []},
        {"title": "改过的开头", "scope": "打底",
         "search_queries": ["改过的词"], "prerequisites": ["第三章"]},
        {"title": "补的一章", "scope": "补充", "search_queries": ["q1"],
         "prerequisites": []},
    ])
    result = run_series_chapters(store, ScriptedProvider(), series_id)

    assert result["status"] == "done"
    assert store.get_series(series_id)["parts"] == 3
    drafts = store.get_series_drafts(series_id)
    assert len(drafts) == 3
    body = store.read_draft_body(drafts[1]["id"])
    assert body["search_meta"]["chapter"] == "改过的开头"
    assert body["search_meta"]["queries"] == ["改过的词"]
    assert body["series_part_label"] == "第 2 章"


def test_outline_cannot_be_replaced_once_a_chapter_is_written(
        tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)
    result = run_series_create(store, ScriptedProvider(), options())

    with pytest.raises(ValueError, match="不能再改提纲"):
        store.replace_chapters(result["series_id"], [
            {"title": "改过的", "search_queries": ["q"]}])


def test_writing_resumes_only_the_chapters_without_a_draft(
        tmp_path, monkeypatch):
    store, series_id, hits, articles_by_url = starved_series(
        tmp_path, monkeypatch)
    scrape_calls: list[list[str]] = []
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url, scrape_calls=scrape_calls)

    result = run_series_chapters(store, ScriptedProvider(), series_id)

    # Only the starved chapter is written again; the other two are left alone.
    assert result["stats"]["chapters_done"] == 1
    assert result["status"] == "done"
    assert len(scrape_calls) == 1
    assert len(store.get_series_drafts(series_id)) == 3


def starved_series(tmp_path, monkeypatch):
    """A run whose middle chapter found nothing, ready to be reran."""
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch,
         hits_for=lambda query: [] if "q2" in query or "第二章" in query
         else hits,
         articles_by_url=articles_by_url)
    result = run_series_create(store, ScriptedProvider(), options())
    assert result["status"] == "partial"
    return store, result["series_id"], hits, articles_by_url


def test_retry_completes_a_failed_chapter_without_rerunning_the_series(
        tmp_path, monkeypatch):
    store, series_id, hits, articles_by_url = starved_series(
        tmp_path, monkeypatch)
    failed = store.list_chapters(series_id)[1]
    # Second time around the chapter's terms find sources.
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)

    result = retry_chapter(store, ScriptedProvider(), series_id, failed["id"])

    assert result["status"] == "done"  # the series is whole again
    chapters = store.list_chapters(series_id)
    assert [row["status"] for row in chapters] == ["done"] * 3
    assert chapters[1]["error"] is None
    drafts = store.get_series_drafts(series_id)
    assert [row["series_order"] for row in drafts] == [1, 2, 3]
    body = store.read_draft_body(drafts[1]["id"])
    assert body["series_part_label"] == "第 2 章"
    assert body["search_meta"]["chapter"] == "第二章"


def test_retry_leaves_the_other_chapters_sources_alone(tmp_path, monkeypatch):
    store, series_id, hits, articles_by_url = starved_series(
        tmp_path, monkeypatch)
    failed = store.list_chapters(series_id)[1]
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)

    retry_chapter(store, ScriptedProvider(), series_id, failed["id"])

    per_chapter = [
        {source["url"]
         for source in store.read_draft_body(row["id"])["sources"]}
        for row in store.get_series_drafts(series_id)]
    assert sum(len(item) for item in per_chapter) == len(
        set().union(*per_chapter))


def test_retry_passes_the_earlier_chapters_as_context(tmp_path, monkeypatch):
    store, series_id, hits, articles_by_url = starved_series(
        tmp_path, monkeypatch)
    failed = store.list_chapters(series_id)[1]
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)
    provider = ScriptedProvider()

    retry_chapter(store, provider, series_id, failed["id"])

    write = next(item for item in provider.prompts if "title_candidates" in item)
    assert "第 1 章《第一章》：这一章的开头段落。" in write
    assert "第 3 章" not in write  # only what the reader has read by then


def test_retry_that_fails_again_keeps_the_series_partial(
        tmp_path, monkeypatch):
    store, series_id, _, _ = starved_series(tmp_path, monkeypatch)
    failed = store.list_chapters(series_id)[1]

    result = retry_chapter(store, ScriptedProvider(), series_id, failed["id"])

    assert result["status"] == "partial"
    assert result["draft_ids"] == []
    chapter = store.list_chapters(series_id)[1]
    assert chapter["status"] == "failed"
    assert "不足 2 篇" in chapter["error"]


def test_retry_after_a_cancelled_run_reports_partial(tmp_path, monkeypatch):
    """A chapter the cancel never reached is still an unwritten chapter."""
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)
    def should_stop() -> bool:
        series = store.latest_series()
        return bool(series) and any(
            row["status"] == "done" for row in store.list_chapters(series["id"]))
    with pytest.raises(SearchCreateCancelled):
        run_series_create(
            store, ScriptedProvider(), options(), should_stop=should_stop)
    series_id = store.latest_series()["id"]
    cancelled = store.list_chapters(series_id)[1]

    result = retry_chapter(store, ScriptedProvider(), series_id, cancelled["id"])

    assert result["status"] == "partial"  # chapter 3 was never started
    statuses = [row["status"] for row in store.list_chapters(series_id)]
    assert statuses == ["done", "done", "pending"]
    assert store.get_series(series_id)["status"] == "partial"


def test_retry_refuses_a_chapter_that_already_has_a_draft(
        tmp_path, monkeypatch):
    store, series_id, _, _ = starved_series(tmp_path, monkeypatch)
    written = store.list_chapters(series_id)[0]

    with pytest.raises(ValueError, match="已有草稿"):
        retry_chapter(store, ScriptedProvider(), series_id, written["id"])


def test_retry_reuses_the_series_own_search_settings(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch,
         hits_for=lambda query: [] if "q2" in query or "第二章" in query
         else hits,
         articles_by_url=articles_by_url)
    run_series_create(
        store, ScriptedProvider(),
        options(ref_count=10, engines=["baidu", "bing"]))
    series = store.latest_series()
    assert json.loads(series["engines"]) == ["baidu", "bing"]

    seen: list = []
    def fake_search(queries, opts, **kwargs):
        seen.append(opts)
        return hits
    monkeypatch.setattr(
        "app.pipeline.series_create.search_queries", fake_search)
    monkeypatch.setattr(
        "app.pipeline.search_create.search_queries", fake_search)
    failed = store.list_chapters(series["id"])[1]

    retry_chapter(store, ScriptedProvider(), series["id"], failed["id"])

    assert list(seen[0].engines) == ["baidu", "bing"]
    assert seen[0].ref_count == 10
