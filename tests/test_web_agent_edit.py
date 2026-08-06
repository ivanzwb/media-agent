"""Tests for _clean_agent_output() and draft agent-edit endpoints."""

from datetime import datetime, timezone
from pathlib import Path
import threading
import time

import frontmatter as _fm
from fastapi.testclient import TestClient

from app.config import Config
from app.db import connect, init_db
from app.models import Article, Draft
from app.store import Store
from app.web.server import create_app
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


# ── End-to-end Agent workflow regression matrix ───────────────────────────


def _agent_client(data_dir: Path):
    cfg = Config(data_dir=data_dir, llm_provider="mock",
                 image_provider="mock", cli_tool="opencode")
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    store = Store(conn, cfg)
    article = store.save_article(Article(
        title="Source", content_md="# source", url="https://example.test/a",
        source_name="Example", source_type="rss",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        images=[], raw_summary=None,
        fetched_at=datetime(2026, 1, 2, tzinfo=timezone.utc), topic="AI"))
    draft = store.save_draft(Draft(
        article_id=article.id, title_candidates=["候选原题"],
        title_cn="固定标题", body_md="## 原正文\n旧内容", topic="AI",
        source_url=article.url, source_name=article.source_name))
    app = create_app(cfg, feeds_path=data_dir / "feeds.yaml")
    app.state.license._dev = True
    return TestClient(app), store, draft


def _fake_cli(monkeypatch, callback):
    instances = []

    class FakeCLI:
        def __init__(self, tool_id="opencode", timeout=500, model="", cwd=None):
            self.tool_id = tool_id
            self.model = model
            self._cwd = str(cwd) if cwd else None
            self.cancelled = False
            instances.append(self)

        def chat(self, messages, **opts):
            return callback(self, messages, opts)

        def cancel(self):
            self.cancelled = True
            return True

    monkeypatch.setattr("app.web.server.cli_provider.CLIProvider", FakeCLI)
    monkeypatch.setattr("app.web.server.cli_provider.detect", lambda _tool: "fake")
    return FakeCLI, instances


def _wait_agent(client, draft_id, terminal=("completed", "error", "cancelled")):
    for _ in range(200):
        status = client.get(
            f"/api/draft/{draft_id}/agent-status").json()
        if status.get("status") in terminal:
            return status
        time.sleep(0.01)
    raise AssertionError("Agent task did not reach a terminal state")


def _edited_document(body="## 新正文\n已修改"):
    # Deliberately tries to rename the draft; the body-only workflow must ignore
    # these model-generated title fields.
    return (
        "---\ntitle_cn: 模型篡改标题\ntitle_candidates:\n"
        "  - 模型候选题\n---\n\n" + body
    )


def test_agent_stdout_apply_preserves_title_and_uses_editor_snapshot(
        tmp_path, monkeypatch):
    seen_prompt = {}

    def respond(_provider, messages, _opts):
        seen_prompt["text"] = messages[-1].content
        return _edited_document("## 新正文\n来自 stdout")

    _fake_cli(monkeypatch, respond)
    client, store, draft = _agent_client(tmp_path / "dev-data")

    response = client.post(f"/api/draft/{draft.id}/agent-edit", data={
        "prompt": "润色正文",
        "body_md": "## 编辑器未保存正文\n必须以此为输入",
        "title_cn": "固定标题",
        "title_candidates": "候选原题",
    })
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    status = _wait_agent(client, draft.id)
    assert status["status"] == "completed"
    assert "编辑器未保存正文" in seen_prompt["text"]
    # Completion alone is non-destructive; Apply is the write boundary.
    assert "旧内容" in store.read_draft_body(draft.id)["body_md"]

    applied = client.post(
        f"/api/draft/{draft.id}/agent-apply", data={"run_id": run_id})
    assert applied.status_code == 200
    assert applied.json()["body_md"] == "## 新正文\n来自 stdout"
    saved = store.read_draft_body(draft.id)
    assert saved["title_cn"] == "固定标题"
    assert saved["title_candidates"] == ["候选原题"]
    assert saved["body_md"] == "## 新正文\n来自 stdout"


def test_agent_edit_cannot_lose_the_reference_list(tmp_path, monkeypatch):
    """The list is provenance the body's [n] markers point into, and nobody
    asking for a polish is offering to gamble it. It is held back from the
    model rather than restored afterwards, so there is nothing to renumber."""
    seen_prompt = {}

    def respond(_provider, messages, _opts):
        seen_prompt["text"] = messages[-1].content
        return _edited_document("## 润色后的正文\n第一段 [1]。")

    _fake_cli(monkeypatch, respond)
    client, store, draft = _agent_client(tmp_path / "data")
    tail = "\n\n---\n\n## 参考文献\n1. 甲文 — 甲媒体\n2. 乙文 — 乙媒体\n"

    response = client.post(f"/api/draft/{draft.id}/agent-edit", data={
        "prompt": "润色正文",
        "body_md": "## 原正文\n第一段 [1]。" + tail,
    })
    run_id = response.json()["run_id"]
    assert _wait_agent(client, draft.id)["status"] == "completed"

    assert "甲文 — 甲媒体" not in seen_prompt["text"]
    client.post(f"/api/draft/{draft.id}/agent-apply", data={"run_id": run_id})
    saved = store.read_draft_body(draft.id)["body_md"]
    assert saved.rstrip() == ("## 润色后的正文\n第一段 [1]。" + tail).rstrip()


def test_the_api_model_name_is_kept_away_from_the_cli_agent(
        tmp_path, monkeypatch):
    """llm_model belongs to the OpenAI-compatible API, not to the CLI tool.

    Handing it over got `opencode run --model agnes-2.0-flash`, which fails
    to resolve and reports the useless "Unexpected server error".
    """
    _fake, instances = _fake_cli(
        monkeypatch, lambda *_args: _edited_document())
    client, store, draft = _agent_client(tmp_path / "data")
    store.set_setting("llm_model", "agnes-2.0-flash")

    client.post(f"/api/draft/{draft.id}/agent-edit", data={"prompt": "润色"})
    assert _wait_agent(client, draft.id)["status"] == "completed"

    assert instances
    assert instances[0].model == ""


def test_agent_file_output_isolated_under_packaged_data_dir(
        tmp_path, monkeypatch):
    install_dir = tmp_path / "portable-install"
    data_dir = install_dir / "data"
    repo_root = tmp_path / "project-root"
    repo_root.mkdir()
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path / "wrong-env-data"))
    workspaces = []

    def respond(provider, _messages, opts):
        assert opts["allow_empty_output"] is True
        cwd = Path(provider._cwd).resolve()
        workspaces.append(cwd)
        (cwd / "output-draft.md").write_text(
            _edited_document("## 文件正文\n来自隔离文件"), encoding="utf-8")
        return "Agent 已完成并写入文件"

    _fake_cli(monkeypatch, respond)
    client, _store, draft = _agent_client(data_dir)
    response = client.post(
        f"/api/draft/{draft.id}/agent-edit", data={"prompt": "从文件返回"})
    status = _wait_agent(client, draft.id)
    assert status["status"] == "completed", status
    assert workspaces
    assert data_dir.resolve() in workspaces[0].parents
    assert workspaces[0].name == response.json()["run_id"]
    assert not (repo_root / "output-draft.md").exists()
    assert not workspaces[0].exists(), "per-run workspace must be cleaned"


def test_agent_missing_output_rejects_stale_shared_file(
        tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.chdir(repo_root)
    legacy = data_dir / "output-draft.md"

    def respond(_provider, _messages, _opts):
        return ""

    _fake_cli(monkeypatch, respond)
    client, store, draft = _agent_client(data_dir)
    legacy.write_text(_edited_document("## 陈旧结果\n不能应用"), encoding="utf-8")
    root_file = repo_root / "output-draft.md"
    root_file.write_text("用户文件", encoding="utf-8")

    client.post(f"/api/draft/{draft.id}/agent-edit", data={"prompt": "无输出"})
    status = _wait_agent(client, draft.id)
    assert status["status"] == "error"
    assert "stdout 为空" in status["error"]
    assert not legacy.exists(), "legacy data-dir scratch must be cleaned"
    assert root_file.read_text(encoding="utf-8") == "用户文件"
    assert "旧内容" in store.read_draft_body(draft.id)["body_md"]


def test_agent_rejects_malformed_summary_file(tmp_path, monkeypatch):
    def respond(provider, _messages, _opts):
        Path(provider._cwd, "output-draft.md").write_text(
            "Agent 已完成修改。", encoding="utf-8")
        return "written"

    _fake_cli(monkeypatch, respond)
    client, store, draft = _agent_client(tmp_path / "data")
    client.post(
        f"/api/draft/{draft.id}/agent-edit", data={"prompt": "malformed"})
    status = _wait_agent(client, draft.id)
    assert status["status"] == "error"
    assert "输出文件" in status["error"]
    assert "执行摘要" in status["error"]
    assert "旧内容" in store.read_draft_body(draft.id)["body_md"]


def test_agent_concurrent_drafts_do_not_leak_results(tmp_path, monkeypatch):
    barrier = threading.Barrier(2)

    def respond(provider, messages, _opts):
        instruction = messages[-1].content
        barrier.wait(timeout=3)
        marker = "ONE" if "ONE" in instruction else "TWO"
        Path(provider._cwd, "output-draft.md").write_text(
            _edited_document(f"## {marker}\n{marker} body"), encoding="utf-8")
        return "written"

    _fake_cli(monkeypatch, respond)
    client, store, first = _agent_client(tmp_path / "data")
    second_article = store.save_article(Article(
        title="Second", content_md="# second", url="https://example.test/b",
        source_name="Example", source_type="rss",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        images=[], raw_summary=None,
        fetched_at=datetime(2026, 1, 2, tzinfo=timezone.utc), topic="AI"))
    second = store.save_draft(Draft(
        article_id=second_article.id, title_candidates=["第二候选"],
        title_cn="第二标题", body_md="## 第二原文", topic="AI",
        source_url=second_article.url, source_name="Example"))

    r1 = client.post(
        f"/api/draft/{first.id}/agent-edit", data={"prompt": "ONE"})
    r2 = client.post(
        f"/api/draft/{second.id}/agent-edit", data={"prompt": "TWO"})
    s1 = _wait_agent(client, first.id)
    s2 = _wait_agent(client, second.id)
    assert s1["body_md"].startswith("## ONE")
    assert s2["body_md"].startswith("## TWO")
    client.post(f"/api/draft/{first.id}/agent-apply",
                data={"run_id": r1.json()["run_id"]})
    client.post(f"/api/draft/{second.id}/agent-apply",
                data={"run_id": r2.json()["run_id"]})
    assert "ONE body" in store.read_draft_body(first.id)["body_md"]
    assert "TWO body" in store.read_draft_body(second.id)["body_md"]


def test_cancelled_stale_run_cannot_replace_rerun(tmp_path, monkeypatch):
    def respond(provider, messages, _opts):
        if "SLOW" in messages[-1].content:
            time.sleep(0.15)
            Path(provider._cwd, "output-draft.md").write_text(
                _edited_document("## STALE\n旧运行"), encoding="utf-8")
            return "written"
        return _edited_document("## FRESH\n新运行")

    _fake_cli(monkeypatch, respond)
    client, store, draft = _agent_client(tmp_path / "data")
    first = client.post(
        f"/api/draft/{draft.id}/agent-edit", data={"prompt": "SLOW"})
    assert first.status_code == 200
    assert client.post(
        f"/api/draft/{draft.id}/agent-cancel").status_code == 200
    second = client.post(
        f"/api/draft/{draft.id}/agent-edit", data={"prompt": "FAST"})
    status = _wait_agent(client, draft.id)
    assert status["status"] == "completed"
    assert "FRESH" in status["body_md"]
    time.sleep(0.2)  # let the cancelled worker attempt its stale publication
    status = client.get(f"/api/draft/{draft.id}/agent-status").json()
    assert status["run_id"] == second.json()["run_id"]
    assert "FRESH" in status["body_md"]
    client.post(f"/api/draft/{draft.id}/agent-apply",
                data={"run_id": second.json()["run_id"]})
    assert "FRESH" in store.read_draft_body(draft.id)["body_md"]


def test_apply_clear_and_rerun_cycle(tmp_path, monkeypatch):
    counter = {"value": 0}

    def respond(_provider, _messages, _opts):
        counter["value"] += 1
        return _edited_document(f"## RUN {counter['value']}\n正文")

    _fake_cli(monkeypatch, respond)
    client, store, draft = _agent_client(tmp_path / "data")
    first = client.post(
        f"/api/draft/{draft.id}/agent-edit", data={"prompt": "first"}).json()
    _wait_agent(client, draft.id)
    assert client.post(f"/api/draft/{draft.id}/agent-apply",
                       data={"run_id": first["run_id"]}).status_code == 200
    assert client.post(
        f"/api/draft/{draft.id}/agent-clear").status_code == 200
    second = client.post(
        f"/api/draft/{draft.id}/agent-edit", data={"prompt": "second"}).json()
    _wait_agent(client, draft.id)
    assert client.post(f"/api/draft/{draft.id}/agent-apply",
                       data={"run_id": second["run_id"]}).status_code == 200
    assert "RUN 2" in store.read_draft_body(draft.id)["body_md"]


def test_template_rewrite_uses_app_data_dir_not_environment(
        tmp_path, monkeypatch):
    data_dir = tmp_path / "packaged" / "data"
    wrong_dir = tmp_path / "cwd-derived-data"
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(wrong_dir))
    used_cwds = []
    seen_source = []

    def respond(provider, messages, _opts):
        used_cwds.append(Path(provider._cwd).resolve())
        seen_source.append(messages[-1].content)
        Path(provider._cwd, "output-draft.md").write_text(
            _edited_document("## 模板正文\n已套用"), encoding="utf-8")
        return "written"

    FakeCLI, _instances = _fake_cli(monkeypatch, respond)
    monkeypatch.setattr(
        "app.web.server.get_rewrite_provider",
        lambda *_args, **_kwargs: FakeCLI("opencode"))
    client, store, draft = _agent_client(data_dir)
    template = client.get("/api/editor/templates").json()["templates"][0]

    started = client.post(
        f"/api/draft/{draft.id}/apply-template",
        data={
            "template_id": template["id"],
            "body_md": "## 模板前未保存正文\n编辑器快照",
            "title_cn": "固定标题",
            "title_candidates": "候选原题",
        })
    assert started.status_code == 200
    status = _wait_agent(client, draft.id)
    assert status["status"] == "completed", status
    assert "模板前未保存正文" in seen_source[0]
    assert used_cwds and data_dir.resolve() in used_cwds[0].parents
    assert wrong_dir.resolve() not in used_cwds[0].parents
    applied = client.post(
        f"/api/draft/{draft.id}/agent-apply",
        data={"run_id": started.json()["run_id"]})
    assert applied.status_code == 200
    assert store.read_draft_body(draft.id)["title_cn"] == "固定标题"
