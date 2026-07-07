"""Tests for _clean_agent_output() and draft agent-edit endpoints."""

import frontmatter as _fm

from app.web.server import _clean_agent_output


# ── _clean_agent_output unit tests ────────────────────────────────────────


def test_clean_passthrough():
    """Clean front-matter + body → unchanged."""
    src = "---\ntitle: GPT-5\ncandidates:\n  - 标题A\n  - 标题B\n---\n\n## 钩子\n正文内容"
    result = _clean_agent_output(src)
    # Should round-trip to same body
    assert "## 钩子" in result
    assert "正文内容" in result
    # Front-matter preserved
    assert "GPT-5" in result
    assert "标题A" in result
    assert "标题B" in result


def test_strips_thinking_text_before_frontmatter():
    """Leading thinking/commentary before --- is stripped."""
    src = """I detect the user wants a rewrite. Let me analyze the content.

---
title: GPT-5
---

Body text"""
    result = _clean_agent_output(src)
    assert "I detect" not in result
    assert "Body text" in result
    assert "GPT-5" in result


def test_unwraps_code_block():
    """```markdown … ``` wrapper is unwrapped."""
    src = "```markdown\n---\ntitle: T\n---\n\nbody\n```"
    result = _clean_agent_output(src)
    assert "```" not in result
    assert "body" in result
    assert "title: T" in result


def test_unwraps_code_block_no_lang():
    """``` … ``` without language marker is unwrapped."""
    src = "```\n---\ntitle: T\n---\n\nbody\n```"
    result = _clean_agent_output(src)
    assert "```" not in result
    assert "body" in result


def test_code_block_with_thinking_text():
    """Thinking text before code block wrapper → both cleaned."""
    src = """I'll rewrite this draft as requested.

```markdown
---
title: GPT-5
candidates:
  - 标题A
  - 标题B
---

正文内容
```"""
    result = _clean_agent_output(src)
    assert "I'll rewrite" not in result
    assert "```" not in result
    assert "正文内容" in result
    assert "标题A" in result
    assert "GPT-5" in result


def test_orphaned_closing_fence():
    """Leading text stripped but stray closing fence remains → cleaned."""
    src = """I'm thinking about this.

---\ntitle: T\n---\n\nbody\n```"""
    result = _clean_agent_output(src)
    assert "I'm thinking" not in result
    assert "```" not in result
    assert "body" in result


def test_strips_trailing_modification_notes():
    """Trailing 修改说明 after body is stripped."""
    src = "---\ntitle: T\n---\n\n## 钩子\n正文\n\n修改说明：修正了错别字。"
    result = _clean_agent_output(src)
    assert "修改说明" not in result
    assert "## 钩子" in result
    assert "正文" in result


def test_strips_trailing_備注():
    """Trailing 备注: after body is stripped."""
    src = "---\ntitle: T\n---\n\nbody\n\n备注：改了三处。"
    result = _clean_agent_output(src)
    assert "备注" not in result
    assert "body" in result


def test_strips_trailing_modification_summary():
    """Trailing 修改总结 after body is stripped."""
    src = "---\ntitle: T\n---\n\nbody\n\n修改总结：整体润色。"
    result = _clean_agent_output(src)
    assert "修改总结" not in result
    assert "body" in result


def test_strips_trailing_note_english():
    """Trailing Note: after body is stripped."""
    src = "---\ntitle: T\n---\n\nbody\n\nNote: Fixed typos"
    result = _clean_agent_output(src)
    assert "Note:" not in result
    assert "body" in result


def test_preserves_promotion_footer():
    """Body with --- separated footer that doesn't match agent keywords → preserved."""
    src = "---\ntitle: T\n---\n\nbody text\n---\n如果你喜欢这篇文章，欢迎分享。"
    result = _clean_agent_output(src)
    assert "如果你喜欢" in result
    assert "body text" in result


def test_passthrough_no_frontmatter():
    """Plain body text without any front-matter → preserved."""
    src = "Just some text.\n\n第二行。"
    result = _clean_agent_output(src)
    assert "Just some text" in result
    assert "第二行" in result


def test_empty_input():
    """Empty or whitespace-only input → not crashed, returns empty-ish result."""
    assert _clean_agent_output("").strip() == ""
    assert _clean_agent_output("   ").strip() == ""


def test_only_frontmatter_no_body():
    """Only front-matter without body → preserved."""
    src = "---\ntitle: T\n---"
    result = _clean_agent_output(src)
    assert "title: T" in result
    # frontmatter dumps adds blank line after ---
    assert result.endswith("\n")


def test_body_internal_separator_not_confused():
    """Body containing --- separator is not confused with front-matter close."""
    src = "---\ntitle: T\n---\n\npara 1\n---\npara 2"
    result = _clean_agent_output(src)
    assert "para 1" in result
    assert "para 2" in result
    assert "title: T" in result


def test_footer_with_dash_only():
    """Body ending in trailing --- without keywords → preserved."""
    src = "---\ntitle: T\n---\n\nbody\n---"
    result = _clean_agent_output(src)
    # trailing --- alone without keyword on same line → stripped by line 611
    assert "body" in result
    assert "title: T" in result


def test_inline_note_not_stripped():
    """'备注' inside body text is not erroneously stripped."""
    src = "---\ntitle: T\n---\n\n正文这里有个备注提醒。"
    result = _clean_agent_output(src)
    assert "备注提醒" in result


def test_code_block_opening_only():
    """Opening fence without closing → content still preserved."""
    src = "```markdown\n---\ntitle: T\n---\n\nbody"
    result = _clean_agent_output(src)
    assert "```" not in result
    assert "body" in result


def test_trailing_fence_with_leading_garbage():
    """Code block fence removed after leading garbage stripped."""
    src = "I'll help.\n```markdown\n---\ntitle: T\n---\n\nbody\n```\n修改说明：done."
    result = _clean_agent_output(src)
    assert "I'll help" not in result
    assert "```" not in result
    assert "修改说明" not in result
    assert "body" in result


def test_draft_252_corruption_like():
    """Realistic corrupted output (thinking + code block + tail note)."""
    src = """I'll rewrite this article in a more engaging style.

```markdown
---
title: "1903 — 新 AI 应用层公司"
candidates:
  - 候选标题 1
  - 候选标题 2
---

正文内容第一段。

第二段内容。
```

修改说明：整体润色了语言表达。"""
    result = _clean_agent_output(src)
    assert "I'll rewrite" not in result
    assert "```" not in result
    assert "修改说明" not in result
    assert "正文内容" in result
    assert "候选标题 1" in result
    assert "1903" in result


def test_multiline_meta_preserved():
    """Multi-line metadata values (list, multi-line strings) survive round-trip."""
    src = """---
title: "Test"
candidates:
  - A
  - B
  - C
status: drafted
---

body text"""
    result = _clean_agent_output(src)
    assert "- A" in result
    assert "- B" in result
    assert "- C" in result
    assert "body text" in result


def test_trailing_dashes_no_newline_before():
    """Body ending right before trailing --- works."""
    src = "---\ntitle: T\n---\n\nbody\n---"
    result = _clean_agent_output(src)
    assert "body" in result
    assert "title: T" in result


def test_trailing_note_on_same_line():
    """'备注：xxx' on last line → stripped (re.sub path)."""
    src = "---\ntitle: T\n---\n\nbody\n备注：done"
    result = _clean_agent_output(src)
    assert "备注" not in result
    assert "body" in result


def test_trailing_modification_note_after_dash_sep():
    """修改说明 after a body-internal --- separator → stripped."""
    src = "---\ntitle: T\n---\n\nbody text\n---\n修改说明：调整了语气。"
    result = _clean_agent_output(src)
    assert "修改说明" not in result
    assert "body text" in result


def test_mixed_case_note():
    """'Note:' (capitalized) is stripped."""
    src = "---\ntitle: T\n---\n\nbody\nNote: Just a note"
    result = _clean_agent_output(src)
    assert "Note:" not in result
    assert "body" in result


def test_lowercase_note():
    """'note:' (lowercase) is stripped."""
    src = "---\ntitle: T\n---\n\nbody\nnote: just a note"
    result = _clean_agent_output(src)
    assert "note:" not in result
    assert "body" in result


def test_colon_variants():
    """Chinese full-width colon after modification keywords is handled."""
    src = "---\ntitle: T\n---\n\nbody\n修改说明：改了三处"
    result = _clean_agent_output(src)
    assert "修改说明" not in result
    assert "body" in result
