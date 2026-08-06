from app.config import Config
from app.models import Draft
from app.pipeline.sanitizer import (
    sanitize, builtin_words, load_words, sanitize_draft)


def test_sanitize_removes_words_and_reports_hits():
    text = "这是全网最低价，100%有效，最好的产品"
    words = {"全网最低", "100%", "最好"}
    clean, hits = sanitize(text, words)
    assert "全网最低" not in clean
    assert "100%" not in clean
    assert "最好" not in clean
    found = {h["word"] for h in hits}
    assert found == {"全网最低", "100%", "最好"}


def test_sanitize_longest_first():
    # "最好" should be removed as a whole, not leave a dangling "好"
    clean, _ = sanitize("最好的", {"最好", "最"})
    assert clean == "的"


def test_ordinary_phrases_survive_the_adlaw_list():
    """A draft title lost its 第一: the filter deletes what it matches, and
    "第一" matches inside "第一次", leaving "次握住水杯"."""
    words = builtin_words("basic")
    for text in ("10年瘫痪患者第一次握住水杯", "迈出第一步", "第一时间响应",
                 "全球第一次实现商业化", "最高人民法院裁定", "取最大值"):
        clean, hits = sanitize(text, words)
        assert clean == text
        assert hits == []


def test_the_claim_itself_is_still_masked():
    words = builtin_words("basic")
    assert sanitize("行业第一的品牌", words)[0] == "行业的品牌"
    assert sanitize("这是全球第一", words)[0] == "这是"
    assert sanitize("效果最好", words)[0] == "效果"
    assert sanitize("首个商业化案例", words)[0] == "商业化案例"


def test_exception_suffixes_are_counted_correctly():
    clean, hits = sanitize("第一次是第一，第一步也是第一", {"第一"})
    assert clean == "第一次是，第一步也是"
    assert hits == [{"word": "第一", "count": 2, "pos": 4}]


def test_custom_words_are_matched_literally():
    # Custom entries are user text, not patterns.
    clean, _ = sanitize("价格 100% 划算", {"100%"})
    assert clean == "价格  划算"
    clean, _ = sanitize("a.c 与 abc", {"a.c"})
    assert clean == " 与 abc"


def test_levels_escalate():
    assert "最好" in builtin_words("basic")          # adlaw
    assert "加微信" not in builtin_words("basic")
    assert "加微信" in builtin_words("standard")      # marketing
    assert "抗癌" not in builtin_words("standard")
    assert "抗癌" in builtin_words("strict")          # medical
    assert builtin_words("off") == set()


def test_load_words_merges_custom(tmp_path):
    cfg = Config(data_dir=tmp_path, sensitive_level="basic",
                 sensitive_words="某词, 另一个词\n第三个")
    words = load_words(cfg)
    assert "最好" in words            # builtin basic
    assert "某词" in words and "第三个" in words  # custom
    # off level still applies custom words
    cfg_off = Config(data_dir=tmp_path, sensitive_level="off",
                     sensitive_words="自定义")
    assert load_words(cfg_off) == {"自定义"}


def test_sanitize_draft_cleans_body_and_titles():
    d = Draft(article_id=1,
              title_candidates=["最好的方案", "普通标题"],
              body_md="本产品100%有效，全网最低", topic="AI",
              source_url="", source_name="")
    hits = sanitize_draft(d, {"最好", "100%", "全网最低"})
    assert hits
    assert "100%" not in d.body_md and "全网最低" not in d.body_md
    assert d.title_candidates[0] == "的方案"
    assert d.sensitive_hits  # 'word×count' summaries recorded
