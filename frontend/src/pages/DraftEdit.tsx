import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App as AntApp, Button, Card, Col, Row, Select, Space, Tabs, Tag, Typography,
  Input, Collapse, Modal, Drawer, Tooltip, Divider, FloatButton, ColorPicker,
  Popover, Segmented,
} from "antd";
import {
  RobotOutlined, HighlightOutlined, FontColorsOutlined,
  AlignCenterOutlined, BulbOutlined, CloudDownloadOutlined, CloseOutlined,
  ColumnWidthOutlined, BgColorsOutlined,
} from "@ant-design/icons";
import MDEditor, { commands, type ICommand } from "@uiw/react-md-editor";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, getJson, postForm } from "../api/client";
import { useLocalState } from "../api/hooks";
import SceneEditor from "../components/SceneEditor";
import { remarkAppDirectives, previewComponents } from "../lib/mdPreview";

const { Title, Text, Paragraph } = Typography;

// Directives whose open line accepts color=/align= params (mirror formatter.py).
const _PARAM_TYPES = new Set([
  "tip", "info", "warning", "success", "danger", "highlight", "note",
  "box", "border", "blocktitle", "card",
]);

// Given full markdown text + a cursor offset, return the line index of the
// enclosing param-capable `:::type` directive open line, or -1 if the cursor
// isn't inside one.
function _findDirectiveLine(text: string, cursor: number): number {
  const curLine = text.slice(0, cursor).split("\n").length - 1;
  const lines = text.split("\n");
  for (let i = curLine; i >= 0; i--) {
    const s = lines[i].trim();
    if (i < curLine && /^:::[ \t]*$/.test(s)) return -1; // closed above cursor
    const m = s.match(/^:::([a-zA-Z]+)/);
    if (m) return _PARAM_TYPES.has(m[1].toLowerCase()) ? i : -1;
  }
  return -1;
}

// Rewrite a directive open line's color/align params. Returns new text or null
// (when the cursor isn't in a param directive, or the open line carries a
// non-param one-liner body which we won't clobber).
function _applyDirectiveParams(
  text: string, cursor: number,
  patch: { color?: string; align?: string | null },
): { text: string; ok: boolean; reason?: string } {
  const li = _findDirectiveLine(text, cursor);
  if (li < 0) return { text, ok: false, reason: "把光标放在组件（:::）内再调整样式" };
  const lines = text.split("\n");
  const m = lines[li].match(/^(\s*):::([a-zA-Z]+)[ \t]*(.*)$/);
  if (!m) return { text, ok: false, reason: "未找到组件" };
  const [, indent, type, argStr] = m;
  const tokens = argStr.split(/\s+/).filter(Boolean);
  if (tokens.some((t) => !t.includes("="))) {
    return { text, ok: false, reason: "该组件为单行写法，请改为多行后再调样式" };
  }
  const params: Record<string, string> = {};
  tokens.forEach((t) => { const i = t.indexOf("="); params[t.slice(0, i).toLowerCase()] = t.slice(i + 1); });
  if (patch.color !== undefined) params.color = patch.color;
  if (patch.align !== undefined) { if (patch.align) params.align = patch.align; else delete params.align; }
  const argOut = Object.entries(params).map(([k, v]) => `${k}=${v}`).join(" ");
  lines[li] = `${indent}:::${type}${argOut ? " " + argOut : ""}`;
  return { text: lines.join("\n"), ok: true };
}

interface DraftData {
  ok: boolean; id: number; status: string; title_cn: string;
  title_candidates: string[]; body_md: string; cover_image: string | null;
  source_url: string; source_name: string; flagged_claims: string[];
  sensitive_hits: string[]; article_id: number | null;
  article_published_at: string | null; article_title: string;
  has_video: boolean; has_narration: boolean; video_brand_name: string;
  statuses: string[];
  platforms: { id: string; label: string; publish_url: string | null; video_publish_url: string | null }[];
  wechat_themes: { id: string; name: string }[];
}

export default function DraftEdit() {
  const { id } = useParams();
  const draftId = Number(id);
  const qc = useQueryClient();
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const { data } = useQuery({ queryKey: ["draft", draftId], queryFn: () => getJson<DraftData>(`/api/draft/${draftId}`) });

  const [body, setBody] = useState("");
  const [titleCn, setTitleCn] = useState("");
  const [titleCands, setTitleCands] = useState("");
  const [status, setStatus] = useState("drafted");
  const [theme, setTheme] = useState("default");
  const [loaded, setLoaded] = useState(false);

  // Dynamic editor height: fill remaining viewport
  const [editorHeight, setEditorHeight] = useState(600);
  const draftRef = useRef<HTMLDivElement>(null);
  const calcHeight = useCallback(() => {
    if (!draftRef.current) return;
    // Account for header (~46px), .ma-content padding (48px), title row (~40px),
    // tabs bar (~40px), toolbar/inputs (~120px), warnings (~60px), and gaps (~30px)
    setEditorHeight(Math.max(300, draftRef.current.clientHeight - 310));
  }, []);
  useEffect(() => {
    calcHeight();
    window.addEventListener("resize", calcHeight);
    const observer = new ResizeObserver(calcHeight);
    if (draftRef.current) observer.observe(draftRef.current);
    return () => { window.removeEventListener("resize", calcHeight); observer.disconnect(); };
  }, [data, calcHeight]);

  useEffect(() => {
    if (data && !loaded) {
      setBody(data.body_md);
      setTitleCn(data.title_cn);
      setTitleCands(data.title_candidates.join("\n"));
      setStatus(data.status);
      setLoaded(true);
    }
  }, [data]);

  if (!data) return null;

  async function save() {
    await postForm(`/drafts/${draftId}`, {
      title_candidates: titleCands, body_md: body, status,
      title_cn: titleCn, from_page: "drafts",
    });
    message.success("已保存");
    qc.invalidateQueries({ queryKey: ["draft", draftId] });
  }

  return (
    <div ref={draftRef} className="ma-draft-edit">
      <Space style={{ justifyContent: "space-between", width: "100%", flexShrink: 0 }}>
        <Title level={2} style={{ margin: 0 }}>编辑草稿 #{draftId}</Title>
        <Button onClick={() => navigate(-1)}>返回列表</Button>
      </Space>

      <Tabs items={[
        {
          key: "article", label: "文章内容", children: (
            <ArticleTab data={data} body={body} setBody={setBody} titleCn={titleCn}
              setTitleCn={setTitleCn} titleCands={titleCands} setTitleCands={setTitleCands}
              status={status} setStatus={setStatus} theme={theme} setTheme={setTheme}
              onSave={save} editorHeight={editorHeight} />
          ),
        },
        {
          key: "video", label: "讲解视频", children: <VideoTab data={data} />,
        },
      ]} />
    </div>
  );
}

function ArticleTab({ data, body, setBody, titleCn, setTitleCn, titleCands, setTitleCands,
  status, setStatus, theme, setTheme, onSave, editorHeight }: any) {
  const qc = useQueryClient();
  const { message } = AntApp.useApp();
  const editorRef = useRef<HTMLDivElement>(null);
  const colorRef = useRef("#e67514");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [agentOpen, setAgentOpen] = useState(false);
  const [tplOpen, setTplOpen] = useState(false);
  const [rewriting, setRewriting] = useLocalState<boolean>(`draftedit-rewriting-${data.id}`, false);
  const [rewriteStyle, setRewriteStyle] = useState("");
  const [platform, setPlatform] = useState("");
  const [localizing, setLocalizing] = useState(false);
  const [locLog, setLocLog] = useState<string[]>([]);
  const [locClosed, setLocClosed] = useState(false);
  const TOUTIAO_URL = "https://mp.toutiao.com/profile_v4/graphic/publish";

  // Track active poll interval so we can clear on unmount
  const rewritePollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const locPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    return () => {
      if (rewritePollRef.current) clearInterval(rewritePollRef.current);
      if (locPollRef.current) clearInterval(locPollRef.current);
    };
  }, []);

  // Resume rewrite polling on mount if backend rewrite is still running
  useEffect(() => {
    if (!rewriting || !data.article_id) return;
    let cancelled = false;
    const poll = setInterval(async () => {
      try {
        const s = await getJson<{ running: boolean; done: boolean; error: string | null }>(
          `/api/rewrite-status?article_id=${data.article_id}`);
        if (cancelled) return;
        if (s.done) {
          clearInterval(poll); setRewriting(false);
          if (s.error) message.error("重写失败：" + s.error);
          else { message.success("重写完成"); qc.invalidateQueries({ queryKey: ["draft", data.id] }); location.reload(); }
        }
      } catch { /* ignore poll errors */ }
    }, 1200);
    rewritePollRef.current = poll;
    return () => { cancelled = true; clearInterval(poll); rewritePollRef.current = null; };
  }, [data.article_id]); // intentionally NOT depending on `rewriting` to avoid double-start

  // Resume localize state on mount (survives tab switch / page refresh):
  // the backend keeps per-draft localize status, so re-attach if still running.
  useEffect(() => {
    let cancelled = false;
    getJson<{ running: boolean; error: string | null; logs: string[] }>(
      `/api/draft/${data.id}/localize-status`)
      .then((s) => {
        if (cancelled || !s.running) return;
        setLocalizing(true);
        setLocClosed(false);
        if (s.logs) setLocLog(s.logs);
        pollLocalize(false);
      })
      .catch(() => { /* ignore */ });
    return () => { cancelled = true; };
  }, [data.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const { data: styleData } = useQuery({
    queryKey: ["rewrite-styles"],
    queryFn: () => getJson<{ styles: { id: string; name: string; is_builtin: boolean }[] }>("/api/rewrite-styles"),
  });

  // insert text at the markdown textarea cursor
  function insertAtCursor(text: string) {
    const ta = editorRef.current?.querySelector<HTMLTextAreaElement>(".w-md-editor-text-input");
    const s = ta?.selectionStart ?? body.length;
    const e = ta?.selectionEnd ?? body.length;
    const before = body.slice(0, s);
    const after = body.slice(e);

    // Block-level snippets (directives, headings, tables, images, quotes,
    // dividers, columns) MUST start at the beginning of a line — otherwise
    // "foo:::tip" won't be parsed as a directive. Wrap with blank lines so the
    // snippet is always its own block regardless of where the cursor sits.
    const isBlock = /^\s*(:{3,}|#{1,6}\s|>|!\[|\||-{3,}|\d+\.\s|[-*]\s)/.test(text);
    let ins = text;
    if (isBlock) {
      ins = ins.replace(/^\s+/, "").replace(/\s+$/, ""); // normalize own margins
      const lead = before.length === 0 ? "" : before.endsWith("\n\n") ? "" : before.endsWith("\n") ? "\n" : "\n\n";
      const trail = after.length === 0 ? "\n" : after.startsWith("\n") ? "\n" : "\n\n";
      ins = lead + ins + trail;
    }
    const next = before + ins + after;
    commit(next);
    const caret = before.length + ins.length;
    setTimeout(() => { ta?.focus(); if (ta) { ta.selectionStart = ta.selectionEnd = caret; } }, 0);
  }
  function wrapSelection(before: string, after: string, placeholder: string) {
    const ta = editorRef.current?.querySelector<HTMLTextAreaElement>(".w-md-editor-text-input");
    if (!ta) return;
    const s = ta.selectionStart ?? 0, e = ta.selectionEnd ?? 0;
    const sel = body.slice(s, e) || placeholder;
    const next = body.slice(0, s) + before + sel + after + body.slice(e);
    commit(next);
    setTimeout(() => ta.focus(), 0);
  }
  function getSelection(): string {
    const ta = editorRef.current?.querySelector<HTMLTextAreaElement>(".w-md-editor-text-input");
    if (!ta) return "";
    return body.slice(ta.selectionStart ?? 0, ta.selectionEnd ?? 0);
  }

  // ── Undo / redo history (controlled textarea breaks native Ctrl+Z) ──
  const pastRef = useRef<string[]>([]);
  const futureRef = useRef<string[]>([]);
  const lastPushRef = useRef(0);
  function handleChange(v: string | undefined) {
    const next = v ?? "";
    const now = Date.now();
    // Coalesce rapid keystrokes into one history entry (300ms window).
    if (now - lastPushRef.current > 300) {
      pastRef.current.push(body);
      if (pastRef.current.length > 300) pastRef.current.shift();
      futureRef.current = [];
    }
    lastPushRef.current = now;
    setBody(next);
  }
  // Record one discrete history entry, then set the body. Every *programmatic*
  // edit (toolbar helpers, component panel, 套用模板 via helper, agent apply,
  // 组件样式 panel) must go through this so Ctrl+Z can undo it. Manual typing
  // and MDEditor built-in toolbar commands are recorded via handleChange.
  function commit(next: string) {
    if (next === body) return;
    pastRef.current.push(body);
    if (pastRef.current.length > 500) pastRef.current.shift();
    futureRef.current = [];
    lastPushRef.current = 0;
    setBody(next);
  }
  function refocusEditor() {
    setTimeout(() => {
      editorRef.current?.querySelector<HTMLTextAreaElement>(".w-md-editor-text-input")?.focus();
    }, 0);
  }
  function undo() {
    if (!pastRef.current.length) return;
    futureRef.current.push(body);
    setBody(pastRef.current.pop()!);
    lastPushRef.current = 0;
    refocusEditor();
  }
  function redo() {
    if (!futureRef.current.length) return;
    pastRef.current.push(body);
    setBody(futureRef.current.pop()!);
    lastPushRef.current = 0;
    refocusEditor();
  }
  function onEditorKeyDown(e: ReactKeyboardEvent) {
    if (!(e.ctrlKey || e.metaKey)) return;
    const k = e.key.toLowerCase();
    if (k === "z" && !e.shiftKey) { e.preventDefault(); e.stopPropagation(); undo(); }
    else if (k === "y" || (k === "z" && e.shiftKey)) { e.preventDefault(); e.stopPropagation(); redo(); }
  }

  async function rewrite() {
    if (!data.article_id) { message.warning("该草稿无关联文章，无法重写"); return; }
    if (!confirm("重写会覆盖当前主稿，确定吗？")) return;
    setRewriting(true);
    try {
      await postForm(`/archive/${data.article_id}/rewrite`, rewriteStyle ? { style: rewriteStyle } : {});
      if (rewritePollRef.current) clearInterval(rewritePollRef.current);
      const poll = setInterval(async () => {
        const s = await getJson<{ running: boolean; done: boolean; error: string | null }>(`/api/rewrite-status?article_id=${data.article_id}`);
        if (s.done) {
          clearInterval(poll); setRewriting(false);
          if (s.error) message.error("重写失败：" + s.error);
          else { message.success("重写完成"); qc.invalidateQueries({ queryKey: ["draft", data.id] }); location.reload(); }
        }
      }, 1200);
    } catch { setRewriting(false); message.error("请求失败"); }
  }

  function pollLocalize(notify: boolean) {
    if (locPollRef.current) clearInterval(locPollRef.current);
    const poll = setInterval(async () => {
      try {
        const s = await getJson<{ running: boolean; error: string | null; logs: string[] }>(
          `/api/draft/${data.id}/localize-status`);
        if (s.logs) setLocLog(s.logs);
        if (!s.running) {
          clearInterval(poll); locPollRef.current = null; setLocalizing(false);
          if (s.error) message.error("本地化失败：" + s.error);
          else if (notify) { message.success("媒体已本地化"); qc.invalidateQueries({ queryKey: ["draft", data.id] }); }
          else qc.invalidateQueries({ queryKey: ["draft", data.id] });
        }
      } catch { /* ignore poll errors */ }
    }, 1200);
    locPollRef.current = poll;
  }

  async function localizeMedia() {
    if (localizing) return;
    setLocalizing(true);
    setLocClosed(false);
    setLocLog([]);
    try {
      await postForm(`/api/draft/${data.id}/localize`);
    } catch { setLocalizing(false); message.error("请求失败"); return; }
    pollLocalize(true);
  }

  async function cover(kind: "scrape" | "generate") {
    message.loading({ content: kind === "scrape" ? "抓取封面中…" : "生成封面中…", key: "cover" });
    const r = await postForm<{ ok: boolean; error?: string }>(`/drafts/${data.id}/cover-${kind}`);
    if (r.ok) { message.success({ content: "封面已更新", key: "cover" }); qc.invalidateQueries({ queryKey: ["draft", data.id] }); }
    else message.error({ content: r.error || "失败", key: "cover" });
  }

  async function publishWeChat(mode: "draft" | "publish") {
    await onSave();
    message.loading({ content: "推送公众号中…", key: "wx" });
    const r = await postForm<{ ok: boolean; error?: string }>(`/drafts/${data.id}/publish/wechat`, { mode, kind: "article", theme: "default" });
    if (r.ok) message.success({ content: mode === "draft" ? "已推送到草稿箱" : "已发布", key: "wx" });
    else message.error({ content: r.error || "推送失败", key: "wx" });
  }

  // theme selector removed on trunk — the component system replaces it.
  async function copyStyledHtml(plat: "wechat" | "toutiao", openUrl?: string) {
    const r = await postForm<{ ok: boolean; html?: string; error?: string }>(`/drafts/${data.id}/styled-html`, { platform: plat, theme: "default" });
    if (!r.ok || !r.html) { message.error(r.error || "生成失败"); return; }
    try {
      if ((window as any).ClipboardItem) {
        await navigator.clipboard.write([new ClipboardItem({
          "text/html": new Blob([r.html], { type: "text/html" }),
          "text/plain": new Blob([r.html], { type: "text/plain" }),
        })]);
      } else await navigator.clipboard.writeText(r.html);
      message.success("已复制美化 HTML");
    } catch { message.warning("复制失败，请手动复制"); }
    if (openUrl) window.open(openUrl, "_blank", "noopener");
  }

  // Adapt to a platform's style, copy title+body to clipboard, open its page.
  async function adaptCopy(pid: string) {
    message.loading({ content: "适配中…", key: "adapt" });
    try {
      const d = await postForm<{ title_candidates?: string[]; body_md?: string; publish_url?: string }>(`/drafts/${data.id}/adapt`, { platform: pid });
      const title = (d.title_candidates && d.title_candidates[0]) || "";
      await navigator.clipboard.writeText(`${title}\n\n${d.body_md || ""}`).catch(() => {});
      message.success({ content: "已适配并复制", key: "adapt" });
      if (d.publish_url) window.open(d.publish_url, "_blank", "noopener");
    } catch { message.error({ content: "适配失败", key: "adapt" }); }
  }

  // Per-platform dynamic action buttons (mirrors trunk _platformActions).
  function platformActionsFor(pid: string): { label: string; primary?: boolean; title?: string; onClick: () => void }[] {
    switch (pid) {
      case "wechat": return [
        { label: "推送草稿箱", primary: true, title: "先保存草稿，再推送图文到公众号草稿箱", onClick: () => publishWeChat("draft") },
        { label: "直接发布", title: "创建草稿并直接发布（不可撤回）", onClick: () => publishWeChat("publish") },
      ];
      case "toutiao": return [
        { label: "复制并打开头条", primary: true, title: "复制美化 HTML 并打开头条图文发布页", onClick: () => copyStyledHtml("toutiao", TOUTIAO_URL) },
      ];
      case "xiaohongshu": return [
        { label: "适配并复制", primary: true, title: "适配为小红书风格并复制到剪贴板", onClick: () => adaptCopy("xiaohongshu") },
      ];
      case "zhihu": return [
        { label: "适配并复制", primary: true, title: "适配为知乎风格并复制到剪贴板", onClick: () => adaptCopy("zhihu") },
      ];
      default: return [];
    }
  }
  function onCopyHtml() {
    if (platform === "toutiao") copyStyledHtml("toutiao", TOUTIAO_URL);
    else copyStyledHtml("wechat");
  }
  const platformActions = platformActionsFor(platform);

  const styleOptions = [{ value: "", label: "默认（全局）" },
    ...(styleData?.styles || []).map((s) => ({ value: s.id, label: s.name + (s.is_builtin ? "" : "（自定义）") }))];

  const { data: tplData } = useQuery({
    queryKey: ["editor-templates"],
    queryFn: () => getJson<{ templates: { id: string; name: string; markdown: string }[] }>("/api/editor/templates"),
  });

  // Custom buttons injected into the MDEditor toolbar (mirrors trunk).
  const label = (t: string): ICommand["icon"] => (<span style={{ fontSize: 12, padding: "0 2px" }}>{t}</span>);
  const styleCommands: ICommand[] = [
    { name: "hl", keyCommand: "hl", buttonProps: { title: "高亮" }, icon: <HighlightOutlined />,
      execute: (s, api) => api.replaceSelection(`==${s.selectedText || "高亮"}==`) },
    { name: "color", keyCommand: "color", buttonProps: { title: "彩色字" },
      icon: <FontColorsOutlined />,
      render: (command, _disabled, executeCommand) => (
        <ColorPicker
          value={colorRef.current}
          onChangeComplete={(c) => { colorRef.current = c.toHexString(); executeCommand(command, command.groupName); }}
          presets={[{
            label: "推荐",
            colors: ["#e67514", "#e60000", "#07C160", "#1e6fff", "#8e44ad", "#333333", "#999999"],
          }]}
        >
          <span role="button" title="彩色字（选中文字后选择颜色应用）"
            style={{ padding: "0 4px", cursor: "pointer" }}>
            <FontColorsOutlined style={{ color: colorRef.current }} />
          </span>
        </ColorPicker>
      ),
      execute: (s, api) => api.replaceSelection(`{color:${colorRef.current}}${s.selectedText || "彩色文字"}{/color}`) },
    { name: "center", keyCommand: "center", buttonProps: { title: "居中" }, icon: <AlignCenterOutlined />,
      execute: (s, api) => api.replaceSelection(`\n:::center\n${s.selectedText || "居中文字"}\n:::\n\n`) },
    { name: "tip", keyCommand: "tip", buttonProps: { title: "提示卡片" }, icon: <BulbOutlined />,
      execute: (s, api) => api.replaceSelection(`\n:::tip\n💡 ${s.selectedText || "提示内容"}\n:::\n\n`) },
    { name: "columns", keyCommand: "columns", buttonProps: { title: "双栏（左右并排）" }, icon: <ColumnWidthOutlined />,
      execute: (s, api) => api.replaceSelection(
        `\n::::columns\n:::col\n${s.selectedText || "左栏内容"}\n:::\n:::col\n右栏内容\n:::\n::::\n\n`) },
  ];
  // Apply a template to the article: put the existing body into the template's
  // main content slot (so 点击模板 = 把文章内容套用上模板), preserving the
  // template's title/structure/footer for the user to fill. Undoable via commit.
  function applyTemplate(t: any) {
    const tpl = String(t.markdown || "");
    const content = body.trim();
    let next: string;
    if (!content) {
      next = tpl;
    } else {
      // first "body-like" placeholder → the article content goes there
      const slot = /\{[^}]*(?:正文|段落|内容|钩子|开场|导语|简介|说明|背景|详情)[^}]*\}/;
      next = slot.test(tpl) ? tpl.replace(slot, content) : (tpl.trimEnd() + "\n\n" + content + "\n");
    }
    commit(next);
    setTplOpen(false);
    refocusEditor();
    message.success(`已套用模板：${t.name}`);
  }
  const templateCommand: ICommand = {
    name: "template", keyCommand: "template",
    buttonProps: { title: "套用模板（可视化预览，点击把正文套入模板）" },
    icon: label("套用模板 ▾"),
    execute: () => setTplOpen(true),
  };
  const panelCommand: ICommand = {
    name: "panel", keyCommand: "panel", buttonProps: { title: "组件面板" }, icon: label("组件面板 ▸"),
    execute: () => setDrawerOpen(true),
  };
  const localizeCommand: ICommand = {
    name: "localize", keyCommand: "localize",
    buttonProps: {
      title: localizing ? "正在本地化媒体…" : "本地化媒体（下载正文图片/视频到本地）",
      disabled: localizing,
      style: localizing ? { opacity: 0.4, cursor: "not-allowed" } : undefined,
    },
    icon: <CloudDownloadOutlined />,
    execute: () => { if (!localizing) localizeMedia(); },
  };

  // 14b — insert-time style panel: adjust color/align of the :::directive the
  // cursor is inside, rewriting its open-line params in place.
  function tweakDirective(patch: { color?: string; align?: string | null }) {
    const ta = editorRef.current?.querySelector<HTMLTextAreaElement>(".w-md-editor-text-input");
    const cursor = ta ? ta.selectionStart : body.length;
    const r = _applyDirectiveParams(body, cursor, patch);
    if (!r.ok) { message.info(r.reason || "无法调整样式"); return; }
    commit(r.text);
    refocusEditor();
  }
  const styleParamCommand: ICommand = {
    name: "styleparam", keyCommand: "styleparam",
    buttonProps: { title: "组件样式（把光标放在 ::: 组件内，调整颜色/对齐）" },
    icon: <BgColorsOutlined />,
    render: (_command, _disabled, _exec) => (
      <Popover trigger="click" placement="bottom"
        content={
          <div style={{ width: 200 }}>
            <div style={{ marginBottom: 8, fontSize: 12, color: "#888" }}>
              把光标放在组件（:::）内
            </div>
            <div style={{ marginBottom: 8 }}>
              <Text style={{ fontSize: 12 }}>颜色</Text>{" "}
              <ColorPicker onChangeComplete={(c) => tweakDirective({ color: c.toHexString() })}
                presets={[{ label: "推荐", colors: ["#409eff", "#67c23a", "#e6a23c", "#f56c6c", "#8e44ad", "#2f6fb3", "#fff2e8"] }]} />
            </div>
            <div style={{ marginBottom: 8 }}>
              <Text style={{ fontSize: 12 }}>对齐</Text>
              <Segmented size="small" block options={[
                { label: "左", value: "left" }, { label: "中", value: "center" },
                { label: "右", value: "right" }, { label: "清除", value: "" },
              ]} onChange={(v) => tweakDirective({ align: (v as string) || null })} />
            </div>
          </div>
        }>
        <span role="button" title="组件样式" style={{ padding: "0 4px", cursor: "pointer" }}>
          <BgColorsOutlined />
        </span>
      </Popover>
    ),
    execute: () => {},
  };
  const editorCommands: ICommand[] = [
    ...commands.getCommands(), commands.divider,
    ...styleCommands, styleParamCommand, commands.divider, localizeCommand, commands.divider, templateCommand, panelCommand,
  ];

  return (
    <div className="ma-draft-article-tab">
      {data.flagged_claims?.length > 0 && (
        <Collapse size="small" style={{ marginBottom: 12, background: "#fffbe6", borderColor: "#ffe58f", flexShrink: 0 }}
          items={[{
            key: "1",
            label: <span style={{ color: "#ad6800" }}>⚠ 事实校验存疑（{data.flagged_claims.length} 项）</span>,
            children: <ul style={{ margin: 0, paddingLeft: 20 }}>{data.flagged_claims.map((c: string, i: number) => <li key={i}>{c}</li>)}</ul>,
          }]} />
      )}
      {data.sensitive_hits?.length > 0 && (
        <Collapse size="small" style={{ marginBottom: 12, background: "#fffbe6", borderColor: "#ffe58f", flexShrink: 0 }}
          items={[{
            key: "1",
            label: <span style={{ color: "#ad6800" }}>🛡 已过滤敏感/违禁词（{data.sensitive_hits.length} 项）</span>,
            children: data.sensitive_hits.join("、"),
          }]} />
      )}

      <Row gutter={16} style={{ flex: 1, minHeight: 0 }}>
        <Col span={6}>
          <Card size="small" title="封面">
            {data.cover_image
              ? <img src={`/images/${data.cover_image}`} alt="cover" style={{ width: "100%", borderRadius: 6 }} />
              : <div style={{ aspectRatio: "16/9", background: "#f0f0f0", display: "flex", alignItems: "center", justifyContent: "center", borderRadius: 6, color: "#999" }}>暂无封面</div>}
            <Space style={{ marginTop: 8 }}>
              <Button size="small" onClick={() => cover("scrape")}>网上抓取</Button>
              <Button size="small" onClick={() => cover("generate")}>AI 生成</Button>
            </Space>
          </Card>
          <Card size="small" title="来源" style={{ marginTop: 12 }}>
            <a href={data.source_url} target="_blank" rel="noopener">{data.source_name}</a>
          </Card>
          {data.article_id && (
            <Card size="small" title="操作" style={{ marginTop: 12 }}>
              <Space direction="vertical" style={{ width: "100%" }}>
                <Select size="small" style={{ width: "100%" }} value={rewriteStyle} onChange={setRewriteStyle} options={styleOptions} />
                <Button size="small" loading={rewriting} onClick={rewrite}>重写（覆盖主稿）</Button>
              </Space>
            </Card>
          )}
        </Col>

        <Col span={18} className="ma-draft-right-col">
          <Card size="small" styles={{ body: { paddingBottom: 8 } }}>
            <Space wrap style={{ marginBottom: 12, flexShrink: 0 }}>
              <Select value={status} onChange={setStatus} style={{ width: 120 }}
                options={data.statuses.map((s: string) => ({ value: s }))} />
              <Button type="primary" onClick={onSave}>保存</Button>
              <Divider type="vertical" />
              <Text type="secondary">平台：</Text>
              <Select style={{ width: 130 }} placeholder="选择平台…" allowClear
                value={platform || undefined} onChange={(v) => setPlatform(v || "")}
                options={data.platforms.map((p: any) => ({ value: p.id, label: p.label }))} />
              {platformActions.map((a, i) => (
                <Button key={i} size="small" className="pro-feature"
                  type={a.primary ? "primary" : "default"} title={a.title} onClick={a.onClick}>{a.label}</Button>
              ))}
              {platform && <Button size="small" onClick={onCopyHtml} title="复制当前平台美化 HTML 到剪贴板">复制HTML</Button>}
            </Space>

            <Input placeholder="文章中文标题" value={titleCn} onChange={(e) => setTitleCn(e.target.value)} style={{ marginBottom: 8, flexShrink: 0 }} />
            <Input.TextArea placeholder="候选标题（每行一个）" value={titleCands} onChange={(e) => setTitleCands(e.target.value)} rows={2} style={{ marginBottom: 8, flexShrink: 0 }} />

            <div ref={editorRef} className="ma-editor-wrap" data-color-mode="light" onKeyDownCapture={onEditorKeyDown}>
              <MDEditor value={body} onChange={handleChange} height={editorHeight}
                preview="live" commands={editorCommands}
                previewOptions={{
                  remarkPlugins: [remarkAppDirectives],
                  components: previewComponents,
                }} />
            </div>
          </Card>
        </Col>
      </Row>

      <TemplateGallery open={tplOpen} onClose={() => setTplOpen(false)}
        templates={tplData?.templates || []} hasContent={!!body.trim()} onApply={applyTemplate} />
      <ComponentDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} onInsert={insertAtCursor} getSelection={getSelection} />
      <AgentModal open={agentOpen} draftId={data.id} onClose={() => setAgentOpen(false)}
        onApplied={(md) => { commit(md); setAgentOpen(false); }} />
      <FloatButton icon={<RobotOutlined />} type="primary" tooltip="Agent 编辑"
        onClick={() => setAgentOpen(true)} />

      {localizing && locLog.length > 0 && !locClosed && (
        <div className="ma-run-panel">
          <div className="ma-run-head">
            <span>本地化媒体…</span>
            <Button size="small" type="text" style={{ color: "#ddd" }} title="关闭"
              icon={<CloseOutlined />} onClick={() => setLocClosed(true)} />
          </div>
          <pre className="ma-run-logs">{locLog.join("\n")}</pre>
        </div>
      )}
    </div>
  );
}

// A small, non-interactive WYSIWYG thumbnail of a component's markdown. Reuses
// the SAME remark plugin + component overrides as the editor's live preview
// (mdPreview.tsx) so the thumbnail renders exactly like the published output.
// CSS-scaled down so a full snippet reads as a compact card preview.
function ComponentThumb({ markdown }: { markdown: string }) {
  const SCALE = 0.72;
  return (
    <div className="ma-comp-thumb" data-color-mode="light"
      style={{ height: 96, overflow: "hidden", pointerEvents: "none",
        background: "#fff", borderBottom: "1px solid #f0f0f0" }}>
      <div style={{ transform: `scale(${SCALE})`, transformOrigin: "top left",
        width: `${100 / SCALE}%`, padding: "6px 10px", boxSizing: "border-box" }}>
        <MDEditor.Markdown source={markdown} remarkPlugins={[remarkAppDirectives]}
          components={previewComponents}
          style={{ background: "transparent", fontSize: 13, lineHeight: 1.5 }} />
      </div>
    </div>
  );
}

// WYSIWYG template picker: renders each full-article template as a scaled-down
// preview card so the user sees the 模板样子 before applying. Clicking a card
// applies the template to the article (fills content into it).
function TemplateGallery({ open, onClose, templates, hasContent, onApply }: {
  open: boolean; onClose: () => void; templates: any[]; hasContent: boolean;
  onApply: (t: any) => void;
}) {
  return (
    <Modal open={open} onCancel={onClose} footer={null} width={760} zIndex={100001}
      title="套用模板" styles={{ body: { maxHeight: "72vh", overflow: "auto" } }}>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {hasContent
          ? "点击模板：把当前正文套入所选模板（保留模板标题/结构，可 Ctrl+Z 撤销）。"
          : "点击模板：以该模板作为正文起稿（可 Ctrl+Z 撤销）。"}
      </Text>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 12 }}>
        {templates.map((t) => (
          <div key={t.id} className="ma-comp-card" role="button" title={`套用：${t.name}`}
            onClick={() => onApply(t)}
            style={{ border: "1px solid #eaeaea", borderRadius: 8, overflow: "hidden",
              cursor: "pointer", background: "#fff" }}>
            <div className="ma-comp-thumb" data-color-mode="light"
              style={{ height: 220, overflow: "hidden", pointerEvents: "none",
                background: "#fff", borderBottom: "1px solid #f0f0f0" }}>
              <div style={{ transform: "scale(0.6)", transformOrigin: "top left",
                width: `${100 / 0.6}%`, padding: "8px 12px", boxSizing: "border-box" }}>
                <MDEditor.Markdown source={t.markdown} remarkPlugins={[remarkAppDirectives]}
                  components={previewComponents}
                  style={{ background: "transparent", fontSize: 13, lineHeight: 1.5 }} />
              </div>
            </div>
            <div style={{ padding: "6px 10px", fontSize: 13, fontWeight: 600, color: "#333" }}>{t.name}</div>
          </div>
        ))}
      </div>
    </Modal>
  );
}

// One clickable gallery card: rendered thumbnail + name; click → insert.
function ComponentCard({ c, onPick }: { c: any; onPick: (c: any) => void }) {
  return (
    <div className="ma-comp-card" role="button" title={`插入：${c.name}`}
      data-testid="comp-card" data-name={c.name} data-category={c.category}
      onClick={() => onPick(c)}
      style={{ border: "1px solid #eaeaea", borderRadius: 8, overflow: "hidden",
        cursor: "pointer", background: "#fff", transition: "box-shadow .15s, border-color .15s" }}>
      <ComponentThumb markdown={c.markdown} />
      <div style={{ padding: "5px 8px", fontSize: 12, fontWeight: 500, color: "#333",
        whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{c.name}</div>
    </div>
  );
}

// Group components by category following the backend's display order, with any
// unknown categories appended (stable) at the end.
function groupByCategory(comps: any[], order: string[]) {
  const groups: Record<string, any[]> = {};
  for (const c of comps) (groups[c.category] ||= []).push(c);
  const known = order.filter((cat) => groups[cat]?.length);
  const extra = Object.keys(groups).filter((cat) => !order.includes(cat));
  return [...known, ...extra].map((cat) => ({ cat, items: groups[cat] }));
}

function ComponentDrawer({ open, onClose, onInsert, getSelection }: { open: boolean; onClose: () => void; onInsert: (t: string) => void; getSelection: () => string; }) {
  const qc = useQueryClient();
  const { message } = AntApp.useApp();
  const { data: comps } = useQuery({
    queryKey: ["editor-components"], enabled: open,
    queryFn: () => getJson<{ categories: string[]; components: any[] }>("/api/editor/components"),
  });
  const { data: mats } = useQuery({
    queryKey: ["editor-materials"], enabled: open,
    queryFn: () => getJson<{ materials: any[] }>("/api/editor/materials"),
  });
  const [q, setQ] = useState("");
  const [mq, setMq] = useState("");
  const [cat, setCat] = useState("");
  const [cc, setCc] = useState({ id: "", name: "", category: "", markdown: "" });
  const filtered = (comps?.components || []).filter((c) =>
    (!cat || c.category === cat) &&
    (!q || (c.name + " " + (c.tags || []).join(" ") + " " + c.category).toLowerCase().includes(q.toLowerCase())));
  const groups = groupByCategory(filtered, comps?.categories || []);
  const customs = (comps?.components || []).filter((c) => !c.is_builtin);

  function pick(c: any) {
    onInsert(c.markdown);
    message.success(`已插入：${c.name}`);
  }

  async function saveCustom() {
    const fd = new FormData();
    fd.set("name", cc.name); fd.set("category", cc.category || "自定义"); fd.set("markdown", cc.markdown);
    const url = cc.id ? `/api/editor/components/${encodeURIComponent(cc.id)}` : "/api/editor/components";
    const r = await fetch(url, { method: cc.id ? "PUT" : "POST", body: fd });
    const d = await r.json();
    if (!r.ok || !d.ok) { message.error(d.error || "保存失败"); return; }
    setCc({ id: "", name: "", category: "", markdown: "" });
    qc.invalidateQueries({ queryKey: ["editor-components"] });
    message.success("已保存组件");
  }
  async function delCustom(id: string) {
    await fetch(`/api/editor/components/${encodeURIComponent(id)}`, { method: "DELETE" });
    qc.invalidateQueries({ queryKey: ["editor-components"] });
  }

  return (
    <Drawer open={open} onClose={onClose} title="组件面板" width={560} zIndex={100000}
      className="ma-component-drawer">
      <Tabs items={[
        { key: "c", label: "组件库", children: (
          <div data-testid="component-gallery">
            <Input.Search placeholder="搜索组件…" value={q} onChange={(e) => setQ(e.target.value)} style={{ marginBottom: 8 }} />
            <Space wrap style={{ marginBottom: 12 }}>
              <Tag.CheckableTag checked={!cat} onChange={() => setCat("")}>全部</Tag.CheckableTag>
              {(comps?.categories || []).map((c) => (
                <Tag.CheckableTag key={c} checked={cat === c} onChange={() => setCat(cat === c ? "" : c)}>{c}</Tag.CheckableTag>
              ))}
            </Space>
            {groups.length === 0 && <Text type="secondary">没有匹配的组件。</Text>}
            {groups.map(({ cat: gcat, items }) => (
              <div key={gcat} data-testid="comp-group" data-category={gcat} style={{ marginBottom: 16 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, margin: "4px 0 8px" }}>
                  <Text strong style={{ fontSize: 13 }}>{gcat}</Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>{items.length}</Text>
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                  {items.map((c) => <ComponentCard key={c.id} c={c} onPick={pick} />)}
                </div>
              </div>
            ))}
          </div>
        ) },
        { key: "m", label: "素材库", children: (
          <div>
            <Input.Search placeholder="搜索素材…" value={mq} onChange={(e) => setMq(e.target.value)} style={{ marginBottom: 12 }} />
            {groupByCategory(
              (mats?.materials || []).filter((m: any) =>
                !mq || (m.name + " " + m.category + " " + m.markdown).toLowerCase().includes(mq.toLowerCase())),
              [],
            ).map(({ cat: gcat, items }) => (
              <div key={gcat} style={{ marginBottom: 14 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, margin: "4px 0 8px" }}>
                  <Text strong style={{ fontSize: 13 }}>{gcat}</Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>{items.length}</Text>
                </div>
                <Space wrap>
                  {items.map((m: any) => (
                    <Tooltip key={m.id} title={m.name}>
                      <Button onClick={() => onInsert(m.markdown + " ")}
                        style={{ fontSize: 18, minWidth: 40 }}>{m.markdown}</Button>
                    </Tooltip>
                  ))}
                </Space>
              </div>
            ))}
          </div>
        ) },
        { key: "custom", label: "自定义", children: (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Text type="secondary">把常用片段存为自定义组件。</Text>
            <Input placeholder="组件名称" value={cc.name} onChange={(e) => setCc({ ...cc, name: e.target.value })} />
            <Input placeholder="分类（默认：自定义）" value={cc.category} onChange={(e) => setCc({ ...cc, category: e.target.value })} />
            <Input.TextArea rows={4} placeholder="组件 Markdown 内容（可含 :::tip 等指令）" value={cc.markdown} onChange={(e) => setCc({ ...cc, markdown: e.target.value })} />
            <Space>
              <Button size="small" onClick={() => setCc({ ...cc, markdown: getSelection() })}>用选中文字填充</Button>
              <Button size="small" type="primary" onClick={saveCustom}>保存组件</Button>
              <Button size="small" onClick={() => setCc({ id: "", name: "", category: "", markdown: "" })}>清空</Button>
            </Space>
            <Divider style={{ margin: "8px 0" }} />
            {customs.map((c) => (
              <Card key={c.id} size="small" styles={{ body: { padding: 8 } }}>
                <Space style={{ justifyContent: "space-between", width: "100%" }}>
                  <b>{c.name}</b>
                  <Space>
                    <Button size="small" onClick={() => onInsert(c.markdown)}>插入</Button>
                    <Button size="small" onClick={() => setCc({ id: c.id, name: c.name, category: c.category, markdown: c.markdown })}>编辑</Button>
                    <Button size="small" danger onClick={() => delCustom(c.id)}>删除</Button>
                  </Space>
                </Space>
              </Card>
            ))}
          </Space>
        ) },
      ]} />
    </Drawer>
  );
}

function AgentModal({ open, draftId, onClose, onApplied }: { open: boolean; draftId: number; onClose: () => void; onApplied: (md: string) => void; }) {
  const { message } = AntApp.useApp();
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [agentStatus, setAgentStatus] = useState<"idle" | "running" | "completed" | "error" | "cancelled">("idle");
  const [elapsed, setElapsed] = useState(0);
  const [errorMsg, setErrorMsg] = useState("");
  const [resultBody, setResultBody] = useState("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Check backend status on modal open (survives page refresh)
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      try {
        const s = await getJson<{ ok: boolean; status: string; elapsed_s?: number; body_md?: string; error?: string; prompt?: string }>(
          `/api/draft/${draftId}/agent-status`);
        if (cancelled) return;
        if (s.status === "running") {
          setAgentStatus("running");
          setElapsed(s.elapsed_s || 0);
          setPrompt(s.prompt || "");
          startPolling();
        } else if (s.status === "completed" && s.body_md) {
          setAgentStatus("completed");
          setResultBody(s.body_md);
        } else if (s.status === "error") {
          setAgentStatus("error");
          setErrorMsg(s.error || "未知错误");
        }
      } catch { /* ignore — status endpoint may not exist yet */ }
    })();
    return () => { cancelled = true; };
  }, [open, draftId]);

  function startPolling() {
    if (pollRef.current) return;
    setElapsed(0);
    timerRef.current = setInterval(() => setElapsed((e) => e + 1), 1000);
    pollRef.current = setInterval(async () => {
      try {
        const s = await getJson<{ ok: boolean; status: string; elapsed_s?: number; body_md?: string; error?: string }>(
          `/api/draft/${draftId}/agent-status`);
        if (s.status === "completed") {
          stopPolling();
          setAgentStatus("completed");
          if (s.body_md) setResultBody(s.body_md);
          message.success("Agent 修改完成");
        } else if (s.status === "error") {
          stopPolling();
          setAgentStatus("error");
          setErrorMsg(s.error || "未知错误");
        } else if (s.status === "cancelled") {
          stopPolling();
          setAgentStatus("cancelled");
          message.info("Agent 已取消");
        } else if (s.status === "running") {
          setElapsed(s.elapsed_s || 0);
        }
      } catch { /* ignore poll errors */ }
    }, 2000);
  }

  function stopPolling() {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
  }

  // Cleanup on unmount / close
  useEffect(() => {
    if (!open) { stopPolling(); setAgentStatus("idle"); setBusy(false); setResultBody(""); setErrorMsg(""); setElapsed(0); }
  }, [open]);

  async function run() {
    if (!prompt.trim()) return;
    setBusy(true);
    setAgentStatus("running");
    setResultBody("");
    setErrorMsg("");
    try {
      const r = await postForm<{ ok?: boolean; running?: boolean; error?: string }>(
        `/api/draft/${draftId}/agent-edit`, { prompt });
      if (r.error) {
        setAgentStatus("error");
        setErrorMsg(r.error);
        setBusy(false);
        message.error(r.error);
      } else if (r.running) {
        startPolling();
        setBusy(false);
      } else {
        // Shouldn't happen with new async backend, but handle fallback
        setBusy(false);
        setAgentStatus("idle");
      }
    } catch {
      setBusy(false);
      setAgentStatus("error");
      setErrorMsg("请求失败");
      message.error("请求失败");
    }
  }

  async function cancel() {
    try {
      await postForm(`/api/draft/${draftId}/agent-cancel`);
      stopPolling();
      setAgentStatus("cancelled");
      message.info("已取消");
    } catch { message.error("取消失败"); }
  }

  async function applyResult() {
    if (!resultBody) return;
    try { await postForm(`/api/draft/${draftId}/agent-clear`); } catch { /* ignore */ }
    onApplied(resultBody);
    message.success("已应用 Agent 修改");
  }

  function formatTime(s: number) {
    const m = Math.floor(s / 60);
    const sec = Math.floor(s % 60);
    return m > 0 ? `${m}:${String(sec).padStart(2, "0")}` : `${sec}s`;
  }

  const isRunning = agentStatus === "running";
  const isCompleted = agentStatus === "completed";
  const isError = agentStatus === "error";

  return (
    <Modal
      open={open}
      onCancel={isRunning ? cancel : onClose}
      closable={!isRunning}
      maskClosable={!isRunning}
      keyboard={!isRunning}
      title="用 CLI Agent 修改正文"
      okText={isRunning ? "处理中…" : isCompleted ? "应用修改" : "执行"}
      okButtonProps={{
        loading: busy,
        disabled: isRunning,
        type: isCompleted ? "primary" : undefined,
      }}
      onOk={isCompleted ? applyResult : run}
      cancelText={isRunning ? "取消执行" : "关闭"}
      cancelButtonProps={isRunning ? { danger: true } : undefined}
    >
      {!isRunning && !isCompleted && !isError && (
        <>
          <Paragraph type="secondary">描述你想让 Agent 做的修改（需在设置里配置 CLI Agent）。</Paragraph>
          <Input.TextArea rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)}
            placeholder="如：把第三段改得更口语化，并补充一个类比" />
        </>
      )}

      {isRunning && (
        <div style={{ textAlign: "center", padding: "24px 0" }}>
          <div style={{ fontSize: 16, marginBottom: 8 }}>🤖 Agent 正在处理…</div>
          <div style={{ fontSize: 24, fontFamily: "monospace", color: "#1890ff" }}>{formatTime(elapsed)}</div>
          <div style={{ marginTop: 12, color: "#999", fontSize: 13 }}>完成后将自动应用修改，可关闭此窗口</div>
        </div>
      )}

      {isCompleted && (
        <div style={{ textAlign: "center", padding: "16px 0" }}>
          <div style={{ fontSize: 16, color: "#52c41a", marginBottom: 8 }}>✅ Agent 修改完成</div>
          <Paragraph type="secondary">点击「应用修改」将结果写入编辑器。</Paragraph>
        </div>
      )}

      {isError && (
        <div style={{ padding: "16px 0" }}>
          <div style={{ fontSize: 16, color: "#ff4d4f", marginBottom: 8 }}>❌ 执行失败</div>
          <Paragraph type="danger" style={{ margin: 0 }}>{errorMsg}</Paragraph>
          <Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
            您可以修改指令后重试，或关闭窗口。
          </Paragraph>
        </div>
      )}
    </Modal>
  );
}

function VideoTab({ data }: { data: DraftData }) {
  const { message } = AntApp.useApp();
  const qc = useQueryClient();
  const [narrating, setNarrating] = useLocalState<boolean>(`draftedit-narrating-${data.id}`, false);
  const [synth, setSynth] = useLocalState<boolean>(`draftedit-synth-${data.id}`, false);
  const [hasVideo, setHasVideo] = useState(data.has_video);
  const [voice, setVoice] = useState<string | undefined>(undefined);
  const [narrLog, setNarrLog] = useState<string[]>([]);

  const { data: ttsProvider } = useQuery({ queryKey: ["settings"], queryFn: () => getJson<{ tts_provider?: string }>("/api/settings"), select: (d) => d.tts_provider || "kitten" });
  const { data: voiceData } = useQuery({
    queryKey: ["voices", ttsProvider],
    queryFn: () => getJson<{ voices: {id: string; label: string}[] }>(`/api/voices?provider=${encodeURIComponent(ttsProvider || "kitten")}`),
    enabled: !!ttsProvider,
  });
  const voices = voiceData?.voices || [];

  // Track active poll intervals so we can clear on unmount
  const narrPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const synthPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    return () => { if (narrPollRef.current) clearInterval(narrPollRef.current); if (synthPollRef.current) clearInterval(synthPollRef.current); };
  }, []);

  // Resume narration polling on mount if backend is still running
  useEffect(() => {
    if (!narrating) return;
    let cancelled = false;
    const poll = setInterval(async () => {
      try {
        const s = await getJson<{ running: boolean; error: string | null; logs: string[] }>(`/api/narration-status?draft_id=${data.id}`);
        if (cancelled) return;
        if (s.logs) setNarrLog(s.logs);
        if (!s.running) {
          clearInterval(poll); setNarrating(false); setNarrLog([]);
          s.error ? message.error("生成失败：" + s.error) : message.success("讲解脚本+配音已生成");
          qc.invalidateQueries({ queryKey: ["script", data.id] });
        }
      } catch { /* ignore poll errors */ }
    }, 1500);
    narrPollRef.current = poll;
    return () => { cancelled = true; clearInterval(poll); narrPollRef.current = null; };
  }, [data.id]); // intentionally NOT depending on `narrating`

  // Resume video synth polling on mount if backend is still running
  useEffect(() => {
    if (!synth) return;
    let cancelled = false;
    const poll = setInterval(async () => {
      try {
        const s = await getJson<{ running: boolean; error: string | null }>(`/api/video-status?draft_id=${data.id}`);
        if (cancelled) return;
        if (!s.running) {
          clearInterval(poll); setSynth(false);
          if (s.error) message.error("合成失败：" + s.error);
          else { message.success("视频已合成"); setHasVideo(true); }
        }
      } catch { /* ignore poll errors */ }
    }, 2000);
    synthPollRef.current = poll;
    return () => { cancelled = true; clearInterval(poll); synthPollRef.current = null; };
  }, [data.id]); // intentionally NOT depending on `synth`

  async function genNarration() {
    setNarrating(true);
    setNarrLog([]);
    await postForm(`/drafts/${data.id}/narration`);
    if (narrPollRef.current) clearInterval(narrPollRef.current);
    const poll = setInterval(async () => {
      const s = await getJson<{ running: boolean; error: string | null; logs: string[] }>(`/api/narration-status?draft_id=${data.id}`);
      if (s.logs) setNarrLog(s.logs);
      if (!s.running) { clearInterval(poll); setNarrating(false); setNarrLog([]); s.error ? message.error("生成失败：" + s.error) : message.success("讲解脚本+配音已生成"); qc.invalidateQueries({ queryKey: ["script", data.id] }); }
    }, 1500);
    narrPollRef.current = poll;
  }
  async function synthVideo() {
    setSynth(true);
    await postForm(`/drafts/${data.id}/video`);
    if (synthPollRef.current) clearInterval(synthPollRef.current);
    const poll = setInterval(async () => {
      const s = await getJson<{ running: boolean; error: string | null }>(`/api/video-status?draft_id=${data.id}`);
      if (!s.running) { clearInterval(poll); setSynth(false); if (s.error) message.error("合成失败：" + s.error); else { message.success("视频已合成"); setHasVideo(true); } }
    }, 2000);
    synthPollRef.current = poll;
  }
  async function prepareChannels() {
    const r = await postForm<{ ok?: boolean; caption?: string; url?: string }>(`/drafts/${data.id}/wechat-channels/prepare`);
    if (r.caption) { await navigator.clipboard.writeText(r.caption).catch(() => {}); }
    if (r.url) window.open(r.url, "_blank", "noopener");
    message.success("文案已复制，已打开发布页");
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      <div style={{ flexShrink: 0 }}>
        <Paragraph type="secondary">基于正文自动生成口播分镜脚本并逐段配音，再合成 mp4（需本地 ffmpeg）。</Paragraph>
        <Space wrap size="middle">
          <Text>TTS 音色：</Text>
          <Select style={{ width: 220 }} placeholder="选择音色" value={voice} onChange={setVoice}
            options={voices.map((v) => ({ value: v.id, label: v.label }))} />
          <Button type="primary" className="pro-feature" loading={narrating} onClick={genNarration}>
            {data.has_narration ? "重新生成讲解脚本+配音" : "生成讲解脚本+配音"}
          </Button>
          <Button type="primary" className="pro-feature" loading={synth} onClick={synthVideo}>
            {hasVideo ? "重新合成讲解视频" : "合成讲解视频（mp4）"}
          </Button>
        </Space>
        {narrating && narrLog.length > 0 && (
          <Card size="small" title="生成进度" style={{ background: "#fafafa", marginTop: 8 }}>
            <div style={{ maxHeight: 200, overflowY: "auto", fontFamily: "monospace", fontSize: 12 }}>
              {narrLog.map((line, i) => <div key={i}>{line}</div>)}
            </div>
          </Card>
        )}
        {hasVideo && (
          <div style={{ marginTop: 8 }}>
            <video controls preload="metadata" style={{ maxWidth: "100%", borderRadius: 8 }} src={`/videos/draft-${data.id}/video.mp4`} />
            <div style={{ marginTop: 8 }}>
        <Space wrap size="middle">
                <a href={`/videos/draft-${data.id}/video.mp4`} download><Button size="small">下载 mp4</Button></a>
                {data.platforms.filter((p) => p.video_publish_url).map((p) => (
                  <a key={p.id} href={p.video_publish_url!} target="_blank" rel="noopener"><Button size="small">{p.label}</Button></a>
                ))}
                <Button size="small" className="pro-feature" onClick={prepareChannels}>视频号发布（半自动）</Button>
              </Space>
            </div>
          </div>
        )}
      </div>
      <Divider style={{ margin: "8px 0", flexShrink: 0 }}>分镜编辑</Divider>
      <div style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        <SceneEditor draftId={data.id} ttsVoice={voice} />
      </div>
    </div>
  );
}
