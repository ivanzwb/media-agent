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
