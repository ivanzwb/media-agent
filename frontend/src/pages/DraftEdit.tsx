import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App as AntApp, Button, Card, Col, Row, Select, Space, Tabs, Tag, Typography,
  Input, Collapse, Modal, Drawer, Tooltip, Divider, FloatButton, ColorPicker,
  Popover, Segmented, Descriptions, Progress,
} from "antd";
import {
  RobotOutlined, HighlightOutlined, FontColorsOutlined,
  AlignCenterOutlined, BulbOutlined, CloudDownloadOutlined, CloseOutlined,
  ColumnWidthOutlined, BgColorsOutlined,
} from "@ant-design/icons";
import MDEditor, { commands, type ICommand } from "@uiw/react-md-editor";
import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, getJson, postForm } from "../api/client";
import { useLocalState } from "../api/hooks";
import SceneEditor from "../components/SceneEditor";
import { remarkAppDirectives, previewComponents } from "../lib/mdPreview";
import { convertMermaidInMarkdown, dataUrlToFile } from "../lib/mermaidExport";

const { Title, Text, Paragraph } = Typography;

// Phones cut the title here in the feed, and a keyword past the cut was never
// shown to anyone.
const TITLE_CUTOFF = 30;
const TITLE_MIN = 15;

// The slot a component leaves for your own words, e.g. {提示内容}. Directive
// syntax wears the same braces, so a colon ({color:#e67514}) or a leading
// slash ({/color}) rules a match out.
const PLACEHOLDER = /\{[^{}:/][^{}:]*\}/;

interface TitleScore { title: string; score?: number; reason?: string }

// 导流体检：微信号、二维码图片、站外链接，公众号都当引流处理。
interface DiversionFinding {
  kind: string; label: string; text: string; line: number; where: string;
}

// AI 味体检：分数越高越像机器写的，findings 指到具体哪一行。
interface FlavorFinding { key: string; label: string; text: string; line: number }
interface FlavorDimension {
  key: string; label: string; hits: number; points: number; weight: number;
  advice: string; shown: number; note?: string;
}
interface FlavorResult {
  score: number; level: string; chars: number;
  dimensions: FlavorDimension[]; findings: FlavorFinding[];
}

// 分数分四档，颜色跟着档走。
function flavorColor(score: number) {
  if (score < 20) return "#389e0d";
  if (score < 45) return "#7cb305";
  if (score < 70) return "#d46b08";
  return "#cf1322";
}

function diversionSummary(findings: DiversionFinding[]) {
  const counts = new Map<string, number>();
  findings.forEach((f) => counts.set(f.label, (counts.get(f.label) || 0) + 1));
  return [...counts].map(([label, n]) => `${label} ${n}`).join("、");
}

/** The candidate list under the textarea: length, where the feed cuts, and
 *  whatever the model thought of each one. Click a row to make it the title. */
function TitleCandidateList(
  { text, scores, onPick }:
  { text: string; scores: TitleScore[]; onPick: (title: string) => void },
) {
  const titles = text.split("\n").map((t) => t.trim()).filter(Boolean);
  if (!titles.length) return null;
  const byTitle = new Map(scores.map((s) => [s.title, s]));
  return (
    <div className="ma-title-list">
      {titles.map((title, i) => {
        const scored = byTitle.get(title);
        const over = title.length > TITLE_CUTOFF;
        return (
          <div key={`${i}-${title}`} className="ma-title-row"
            onClick={() => onPick(title)} title="点击设为文章标题">
            <Text type="secondary">{i + 1}.</Text>
            <span className="ma-title-text">
              {over ? title.slice(0, TITLE_CUTOFF) : title}
              {over && <span className="ma-title-cut">{title.slice(TITLE_CUTOFF)}</span>}
            </span>
            {scored?.score !== undefined && (
              <Tag color="blue" style={{ marginInlineEnd: 0 }}>{scored.score} 分</Tag>
            )}
            {scored?.reason && (
              <Text className="ma-title-reason" ellipsis style={{ maxWidth: 220 }}>
                {scored.reason}
              </Text>
            )}
            <Tag color={over ? "red" : title.length < TITLE_MIN ? "orange" : "default"}
              style={{ marginInlineEnd: 0 }}>
              {title.length} 字{over ? `，超出 ${title.length - TITLE_CUTOFF}` : ""}
            </Tag>
          </div>
        );
      })}
    </div>
  );
}

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

interface SeriesChapterNav {
  order: number; title: string; status: string; draft_id: number | null;
}

interface SeriesNav {
  id: number; title: string; order: number | null; part_label: string;
  prerequisites: string[]; chapters: SeriesChapterNav[];
}

interface DraftData {
  ok: boolean; id: number; status: string; title_cn: string;
  title_candidates: string[]; title_scores?: TitleScore[];
  digest: string; body_md: string; cover_image: string | null;
  source_url: string; source_name: string; flagged_claims: string[];
  origin?: string;
  sources?: Array<{
    title?: string; name?: string; source_name?: string; url?: string; source_url?: string;
    published_at?: string | null;
  }>;
  citations?: Array<string | {
    claim?: string; text?: string; title?: string;
    source_title?: string; source_name?: string;
    url?: string; source_url?: string; index?: number;
    source_indexes?: number[]; quote?: string;
  }> | Record<string, string | {
    claim?: string; text?: string; title?: string;
    source_title?: string; source_name?: string;
    url?: string; source_url?: string; index?: number;
  }>;
  search_meta?: {
    lang?: string; time_range_days?: number; ref_count?: number;
    style_id?: string | null; engines?: string[]; queries?: string[];
  };
  series?: SeriesNav | null;
  sensitive_hits: string[]; diversion?: DiversionFinding[];
  article_id: number | null;
  article_published_at: string | null; article_title: string;
  has_video: boolean; has_narration: boolean; video_brand_name: string;
  statuses: string[];
  platforms: { id: string; label: string; publish_url: string | null; video_publish_url: string | null }[];
  wechat_themes: { id: string; name: string }[];
}

interface WeChatPublishStatus {
  running: boolean;
  done: boolean;
  ok: boolean;
  mode: "draft" | "publish" | null;
  error: string | null;
  result?: Record<string, unknown> | null;
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
  const [digest, setDigest] = useState("");
  const [status, setStatus] = useState("drafted");
  const [theme, setTheme] = useState("default");
  const [loaded, setLoaded] = useState(false);

  // DraftEdit can stay mounted while the route id changes. Never let the
  // previous draft's one-shot hydration block the next draft from loading.
  useEffect(() => { setLoaded(false); }, [draftId]);

  useEffect(() => {
    if (data && !loaded) {
      setBody(data.body_md);
      setTitleCn(data.title_cn);
      setTitleCands(data.title_candidates.join("\n"));
      setDigest(data.digest || "");
      setStatus(data.status);
      setLoaded(true);
    }
  }, [data]);

  if (!data) return null;

  async function save() {
    await postForm(`/drafts/${draftId}`, {
      title_candidates: titleCands, body_md: body, status,
      title_cn: titleCn, digest, from_page: "drafts",
    });
    message.success("已保存");
    qc.invalidateQueries({ queryKey: ["draft", draftId] });
  }

  return (
    <div className="ma-draft-edit">
      <Space style={{ justifyContent: "space-between", width: "100%", flexShrink: 0 }}>
        <Space>
          <Title level={2} style={{ margin: 0 }}>编辑草稿 #{draftId}</Title>
          {data.origin === "search_create" && <Tag color="green">搜索创作</Tag>}
        </Space>
        <Button onClick={() => navigate(-1)}>返回列表</Button>
      </Space>

      <Tabs items={[
        {
          key: "article", label: "文章内容", children: (
            <ArticleTab data={data} body={body} setBody={setBody} titleCn={titleCn}
              setTitleCn={setTitleCn} titleCands={titleCands} setTitleCands={setTitleCands}
              digest={digest} setDigest={setDigest}
              status={status} setStatus={setStatus} theme={theme} setTheme={setTheme}
              onSave={save} />
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
  digest, setDigest, status, setStatus, theme, setTheme, onSave }: any) {
  const qc = useQueryClient();
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const editorRef = useRef<HTMLDivElement>(null);
  // MDEditor only takes a pixel height, so measure the slot flexbox gives it.
  // Estimating it from the viewport got the editor wrong by hundreds of pixels
  // whenever a warning banner, an extra title candidate or the digest counter
  // changed the height of everything stacked above it.
  const [editorHeight, setEditorHeight] = useState(400);
  useEffect(() => {
    const wrap = editorRef.current;
    if (!wrap) return;
    const measure = () => setEditorHeight(Math.max(300, wrap.clientHeight));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(wrap);
    return () => observer.disconnect();
  }, []);
  // Editor / preview split. Upstream hard-codes 50/50, so the divider below
  // drives both panes off one variable. It lives inside the pane area itself,
  // which keeps it aligned in fullscreen too.
  const [split, setSplit] = useLocalState<number>("draftedit-split", 50);
  const [paneEl, setPaneEl] = useState<HTMLElement | null>(null);
  useEffect(() => {
    setPaneEl(editorRef.current?.querySelector<HTMLElement>(".w-md-editor-content") ?? null);
  }, []);
  function dragSplit(ev: React.PointerEvent) {
    ev.preventDefault();
    if (!paneEl) return;
    const move = (e: PointerEvent) => {
      const r = paneEl.getBoundingClientRect();
      const pct = ((e.clientX - r.left) / r.width) * 100;
      setSplit(Math.min(85, Math.max(15, Math.round(pct))));
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  }
  const colorRef = useRef("#e67514");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [agentOpen, setAgentOpen] = useState(false);
  const [flavor, setFlavor] = useState<FlavorResult | null>(null);
  const [flavorOpen, setFlavorOpen] = useState(false);
  const [flavorBusy, setFlavorBusy] = useState(false);
  const [rewriting, setRewriting] = useLocalState<boolean>(`draftedit-rewriting-${data.id}`, false);
  const [rewriteStyle, setRewriteStyle] = useState("");
  const [platform, setPlatform] = useState("");
  const [localizing, setLocalizing] = useState(false);
  const [locLog, setLocLog] = useState<string[]>([]);
  const [locClosed, setLocClosed] = useState(false);
  const [tplApplying, setTplApplying] = useState(false);
  const [wechatPublishing, setWechatPublishing] = useState<"draft" | "publish" | null>(null);
  const [coverCollapsed, setCoverCollapsed] = useLocalState<boolean>("draftedit-coverCollapsed", false);
  const TOUTIAO_URL = "https://mp.toutiao.com/profile_v4/graphic/publish";
  // Series chapters are written the same way as search drafts, so they carry
  // the same reference, citation and search-parameter panels.
  const isSearchDraft = data.origin === "search_create" || data.origin === "series";
  const searchSources = Array.isArray(data.sources) ? data.sources : [];
  const citations = Array.isArray(data.citations)
    ? data.citations
    : data.citations && typeof data.citations === "object"
      ? Object.entries(data.citations).map(([claim, value]: [string, any]) =>
        typeof value === "string" ? { claim, source_url: value } : { claim, ...value })
      : [];

  // Track active poll interval so we can clear on unmount
  const rewritePollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const locPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const tplPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const wxPollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wxSubmittingRef = useRef(false);
  useEffect(() => {
    return () => {
      if (rewritePollRef.current) clearInterval(rewritePollRef.current);
      if (locPollRef.current) clearInterval(locPollRef.current);
      if (tplPollRef.current) clearInterval(tplPollRef.current);
      if (wxPollRef.current) clearTimeout(wxPollRef.current);
      wxSubmittingRef.current = false;
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

  // Re-attach to a backend publish workflow after a tab switch/page reload.
  useEffect(() => {
    let cancelled = false;
    getJson<WeChatPublishStatus>(
      `/api/draft/${data.id}/publish/wechat/status`)
      .then((s) => {
        if (!cancelled && s.running && (s.mode === "draft" || s.mode === "publish")) {
          startWeChatPolling(s.mode);
        }
      })
      .catch(() => { /* no prior workflow is a normal state */ });
    return () => { cancelled = true; };
  }, [data.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const { data: styleData } = useQuery({
    queryKey: ["rewrite-styles"],
    queryFn: () => getJson<{ styles: { id: string; name: string; is_builtin: boolean }[] }>("/api/rewrite-styles"),
  });

  // Insert a snippet at the markdown textarea cursor. Selected text is never
  // thrown away: it fills the snippet's first placeholder, and a snippet with
  // no placeholder to fill lands after the selection instead of over it.
  function insertAtCursor(text: string) {
    const ta = editorRef.current?.querySelector<HTMLTextAreaElement>(".w-md-editor-text-input");
    const s = ta?.selectionStart ?? body.length;
    const e = ta?.selectionEnd ?? body.length;
    const selected = body.slice(s, e).trim();
    let before = body.slice(0, s);
    const after = body.slice(e);
    if (selected) {
      const slot = text.match(PLACEHOLDER);
      if (slot) text = text.replace(slot[0], selected);
      else before = body.slice(0, e);   // keep the selection, insert after it
    }

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
        const s = await getJson<{ running: boolean; error: string | null; logs: string[]; body_md?: string | null }>(
          `/api/draft/${data.id}/localize-status`);
        if (s.logs) setLocLog(s.logs);
        if (!s.running) {
          clearInterval(poll); locPollRef.current = null; setLocalizing(false);
          if (s.error) message.error("本地化失败：" + s.error);
          else {
            // Reflect the rewritten body (local image/video links) in the
            // editor — otherwise it keeps showing the old remote URLs.
            if (s.body_md && s.body_md !== body) commit(s.body_md);
            if (notify) message.success("媒体已本地化");
            qc.invalidateQueries({ queryKey: ["draft", data.id] });
          }
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

  function finishWeChatPolling() {
    if (wxPollRef.current) clearTimeout(wxPollRef.current);
    wxPollRef.current = null;
    wxSubmittingRef.current = false;
    setWechatPublishing(null);
  }

  function startWeChatPolling(mode: "draft" | "publish") {
    if (wxPollRef.current) clearTimeout(wxPollRef.current);
    wxSubmittingRef.current = true;
    setWechatPublishing(mode);
    message.loading({
      content: mode === "draft" ? "正在上传并创建微信草稿…" : "正在上传并等待微信确认发布…",
      key: "wx", duration: 0,
    });

    let retry = 0;
    const MAX_RETRY = 250; // ~5 minutes at 1.2s intervals
    const poll = async () => {
      try {
        const s = await getJson<WeChatPublishStatus>(
          `/api/draft/${data.id}/publish/wechat/status`);
        if (s.done) {
          finishWeChatPolling();
          if (s.ok) {
            message.success({
              content: mode === "draft" ? "已推送到草稿箱" : "微信已确认发布成功",
              key: "wx",
            });
            qc.invalidateQueries({ queryKey: ["draft", data.id] });
          } else {
            message.error({
              content: s.error || (mode === "draft" ? "推送草稿箱失败" : "微信发布失败"),
              key: "wx", duration: 8,
            });
          }
          return;
        }
        if (++retry > MAX_RETRY) {
          finishWeChatPolling();
          message.error({ content: "微信发布超时，请检查公众号后台确认状态", key: "wx", duration: 8 });
          return;
        }
        wxPollRef.current = setTimeout(poll, 1200);
      } catch (e: any) {
        // The backend workflow may still be running. Keep both actions locked
        // and retry status rather than risking a duplicate submission.
        if (++retry > MAX_RETRY) {
          finishWeChatPolling();
          message.error({ content: "微信发布状态查询超时，请检查公众号后台", key: "wx", duration: 8 });
          return;
        }
        message.warning({
          content: e?.response?.data?.error || e?.message || "暂时无法查询微信发布状态，正在重试",
          key: "wx", duration: 4,
        });
        wxPollRef.current = setTimeout(poll, 2000);
      }
    };
    void poll();
  }

  // Render every ```mermaid fence in `source` to PNG, upload each to the
  // draft's mermaid-image endpoint, and return markdown where the fences are
  // replaced with ![](/media/...) links. The WeChat / styled-html pipelines
  // then carry the images through the normal media path — their
  // markdown_to_html has no code-fence handling, so raw fences would publish
  // as garbage. Throws with a descriptive message on the first failure
  // (fence-free drafts return `count: 0` with no mermaid module loaded).
  async function withMermaidPngs(source: string): Promise<{ markdown: string; count: number }> {
    return convertMermaidInMarkdown(source, async (pngDataUrl) => {
      const file = dataUrlToFile(pngDataUrl);
      const r = await postForm<{ ok: boolean; url?: string; error?: string }>(
        `/api/draft/${data.id}/mermaid-image`,
        { file });
      if (!r.ok || !r.url) throw new Error(r.error || "mermaid 图片上传失败");
      return r.url;
    });
  }

  async function publishWeChat(mode: "draft" | "publish") {
    // A ref closes the same-render double-click window before React state
    // updates; the backend independently coalesces concurrent requests.
    if (wxSubmittingRef.current) return;
    wxSubmittingRef.current = true;
    setWechatPublishing(mode);
    message.loading({ content: "正在保存并准备微信推送…", key: "wx", duration: 0 });
    try {
      await onSave();
      // The saved body is what goes out, so the check runs on that. It only
      // warns — the push carries on either way.
      const saved = await getJson<DraftData>(`/api/draft/${data.id}`);
      if (saved.diversion?.length) {
        message.warning({
          content: `导流体检发现 ${saved.diversion.length} 处（${diversionSummary(saved.diversion)}），已照常推送，详见页面顶部`,
          key: "wx-diversion", duration: 8,
        });
      }
      const { markdown: bodyMd, count } = await withMermaidPngs(body);
      const r = await postForm<{ ok: boolean; running: boolean; mode: "draft" | "publish"; error?: string }>(
        `/drafts/${data.id}/publish/wechat`,
        { mode, kind: "article", theme: "default", ...(count > 0 ? { body_md: bodyMd } : {}) });
      if (!r.ok) throw new Error(r.error || "微信推送启动失败");
      startWeChatPolling(r.mode || mode);
    } catch (e: any) {
      finishWeChatPolling();
      message.error({
        content: e?.response?.data?.error || e?.message || "微信推送请求失败",
        key: "wx", duration: 8,
      });
    }
  }

  // theme selector removed on trunk — the component system replaces it.
  async function copyStyledHtml(plat: "wechat" | "toutiao", openUrl?: string) {
    // Same mermaid → PNG handling as publishWeChat: raw fences would render
    // as garbage in the copied HTML (markdown_to_html has no fence handling).
    const { markdown: bodyMd, count } = await withMermaidPngs(body);
    const r = await postForm<{ ok: boolean; html?: string; error?: string }>(`/drafts/${data.id}/styled-html`, { platform: plat, theme: "default", ...(count > 0 ? { body_md: bodyMd } : {}) });
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
  function platformActionsFor(pid: string): { label: string; primary?: boolean; title?: string; mode?: "draft" | "publish"; onClick: () => void }[] {
    switch (pid) {
      case "wechat": return [
        { label: "推送草稿箱", primary: true, mode: "draft", title: "先保存草稿，再推送图文到公众号草稿箱", onClick: () => publishWeChat("draft") },
        { label: "直接发布", mode: "publish", title: "创建草稿并直接发布（不可撤回）", onClick: () => publishWeChat("publish") },
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
    queryFn: () => getJson<{ templates: { id: string; name: string; markdown: string; category?: string; is_builtin?: boolean }[]; categories?: string[] }>("/api/editor/templates"),
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
  // Apply a template to the article. If the draft has content, the article is
  // REWRITTEN to follow the template's structure/visual style (via LLM or CLI
  // Agent per settings, async with progress). If the draft is empty there's
  // nothing to rewrite, so the template is dropped in as a starter. Undoable.
  async function applyTemplate(t: any) {
    if (tplApplying) return;
    const content = body.trim();
    if (!content) {
      commit(String(t.markdown || ""));
      setDrawerOpen(false);
      refocusEditor();
      message.success(`已套用模板：${t.name}`);
      return;
    }
    setTplApplying(true);
    message.loading({ content: `正在用「${t.name}」模板重写文章…`, key: "tpl", duration: 0 });
    try {
      const r = await postForm<{ ok?: boolean; running?: boolean; error?: string; run_id?: string }>(
        `/api/draft/${data.id}/apply-template`, {
          template_id: t.id,
          body_md: body,
          title_cn: titleCn,
          title_candidates: titleCands,
        });
      if (r.error || r.ok === false) {
        setTplApplying(false);
        message.error({ content: "套用模板失败：" + (r.error || "未知错误"), key: "tpl" });
        return;
      }
      setDrawerOpen(false);
      if (tplPollRef.current) clearInterval(tplPollRef.current);
      const poll = setInterval(async () => {
        try {
          const s = await getJson<{ status: string; run_id?: string; body_md?: string; error?: string }>(
            `/api/draft/${data.id}/agent-status`);
          if (r.run_id && s.run_id && s.run_id !== r.run_id) {
            clearInterval(poll); tplPollRef.current = null; setTplApplying(false);
            message.error({ content: "套用模板失败：运行结果已被另一任务替换", key: "tpl" });
            return;
          }
          if (s.status === "completed") {
            clearInterval(poll); tplPollRef.current = null; setTplApplying(false);
            try {
              const applied = await postForm<{ body_md: string }>(
                `/api/draft/${data.id}/agent-apply`, { run_id: r.run_id || s.run_id || "" });
              if (applied.body_md) commit(applied.body_md);
              message.success({ content: `已用「${t.name}」模板重写`, key: "tpl" });
              qc.invalidateQueries({ queryKey: ["draft", data.id] });
              refocusEditor();
            } catch (e: any) {
              message.error({
                content: "套用模板失败：" + (e?.response?.data?.error || e?.message || "应用结果失败"),
                key: "tpl",
              });
            }
          } else if (s.status === "error") {
            clearInterval(poll); tplPollRef.current = null; setTplApplying(false);
            message.error({ content: "套用模板失败：" + (s.error || "未知错误"), key: "tpl" });
          }
        } catch { /* transient poll error — keep polling */ }
      }, 1200);
      tplPollRef.current = poll;
    } catch (e: any) {
      setTplApplying(false);
      // Surface Pro-gate / rewrite-quota messages (403 body carries .error).
      const msg = e?.response?.data?.error || "请求失败";
      message.error({ content: msg, key: "tpl" });
    }
  }
  const panelCommand: ICommand = {
    name: "panel", keyCommand: "panel",
    buttonProps: { title: tplApplying ? "正在用模板重写文章…" : "组件面板（套用模板 / 组件库 / 素材库）" },
    icon: label(tplApplying ? "组件面板…（重写中）" : "组件面板 ▸"),
    execute: () => setDrawerOpen(true),
  };
  // 体检的是编辑框里此刻的内容，不是存下来的草稿：改一句就想重测一次，
  // 不该逼人先保存。后端是纯正则，点多少次都不花钱。
  async function checkFlavor() {
    if (!body.trim()) { message.info("正文是空的，没什么可检测的"); return; }
    setFlavorBusy(true);
    try {
      setFlavor(await postForm<FlavorResult>("/api/ai-flavor", { text: body }));
      setFlavorOpen(true);
    } catch {
      message.error("AI 味体检失败");
    } finally {
      setFlavorBusy(false);
    }
  }

  // 点命中条目就把光标放到那一行，并让它露在视野中间偏上。
  function jumpToLine(line: number) {
    const ta = editorRef.current?.querySelector<HTMLTextAreaElement>(".w-md-editor-text-input");
    if (!ta) return;
    const lines = body.split("\n");
    const start = lines.slice(0, line - 1)
      .reduce((n: number, l: string) => n + l.length + 1, 0);
    ta.focus();
    ta.setSelectionRange(start, start + (lines[line - 1]?.length ?? 0));
    const lh = parseFloat(getComputedStyle(ta).lineHeight) || 21;
    ta.scrollTop = Math.max(0, (line - 1) * lh - ta.clientHeight / 3);
  }

  const flavorCommand: ICommand = {
    name: "aiflavor", keyCommand: "aiflavor",
    buttonProps: {
      title: flavorBusy ? "正在体检…" : "AI 味体检（检测编辑框里当前的内容）",
      disabled: flavorBusy,
    },
    icon: label(flavorBusy ? "AI 味…" : "AI 味"),
    execute: () => { if (!flavorBusy) checkFlavor(); },
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
    ...styleCommands, styleParamCommand, commands.divider, localizeCommand,
    flavorCommand, commands.divider, panelCommand,
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
      {(data.diversion?.length ?? 0) > 0 && (
        <Collapse size="small" style={{ marginBottom: 12, background: "#fff2f0", borderColor: "#ffccc7", flexShrink: 0 }}
          items={[{
            key: "1",
            label: (
              <span style={{ color: "#a8071a" }}>
                📡 导流体检：{data.diversion!.length} 处可能被判为引流
                （{diversionSummary(data.diversion!)}）
              </span>
            ),
            children: (
              <>
                <ul style={{ margin: 0, paddingLeft: 20 }}>
                  {data.diversion!.map((f: DiversionFinding, i: number) => (
                    <li key={i}>
                      <Tag color="red" style={{ marginInlineEnd: 6 }}>{f.label}</Tag>
                      {f.where}第 {f.line} 行：<Text code>{f.text}</Text>
                    </li>
                  ))}
                </ul>
                <Text type="secondary" style={{ display: "block", marginTop: 8 }}>
                  正文、摘要或推广文案里出现微信号、二维码和站外链接，文章会失去推荐流量。
                  这里只做提示，不会改稿，也不阻止发布。
                </Text>
              </>
            ),
          }]} />
      )}

      <Row gutter={16} style={{ flex: 1, minHeight: 0 }} wrap={false}>
        {coverCollapsed ? (
          <Col flex="0 0 auto">
            <Button size="small" type="text" onClick={() => setCoverCollapsed(false)}
              title="展开封面栏"
              style={{ height: "100%", writingMode: "vertical-rl", padding: "8px 2px" }}>
              封面 »
            </Button>
          </Col>
        ) : (
          <Col flex="0 0 25%" className="ma-draft-left-col" style={{ maxWidth: "25%" }}>
            <div style={{ textAlign: "right", marginBottom: 4 }}>
              <Button size="small" type="text" onClick={() => setCoverCollapsed(true)} title="收起封面栏">« 收起</Button>
            </div>
            <Card size="small" title="封面">
              {data.cover_image
                ? <img src={`/images/${data.cover_image}`} alt="cover" style={{ width: "100%", borderRadius: 6 }} />
                : <div style={{ aspectRatio: "16/9", background: "#f0f0f0", display: "flex", alignItems: "center", justifyContent: "center", borderRadius: 6, color: "#999" }}>暂无封面</div>}
              <Space style={{ marginTop: 8 }}>
                <Button size="small" onClick={() => cover("scrape")}>网上抓取</Button>
                <Button size="small" onClick={() => cover("generate")}>AI 生成</Button>
                {data.cover_image && (
                  <Button size="small" danger onClick={async () => {
                    const r = await postForm<{ ok: boolean; error?: string }>(`/drafts/${data.id}/cover-clear`);
                    if (r.ok) { message.success("封面已清空"); qc.invalidateQueries({ queryKey: ["draft", data.id] }); }
                    else message.error(r.error || "清空失败");
                  }}>清空</Button>
                )}
              </Space>
            </Card>
            {data.series && (
              <Card size="small" style={{ marginTop: 12 }}
                title={`系列：${data.series.title}`}
                extra={<Link to="/series">查看系列</Link>}>
                <Space direction="vertical" size={6} style={{ width: "100%" }}>
                  {data.series.chapters.map((chapter: SeriesChapterNav) => {
                    const current = chapter.order === data.series?.order;
                    const label = `${chapter.order}. ${chapter.title}`;
                    return (
                      <div key={chapter.order} style={{ lineHeight: 1.35 }}>
                        {current ? (
                          <Text strong>{label}（当前）</Text>
                        ) : chapter.draft_id ? (
                          <Link to={`/drafts/${chapter.draft_id}/edit`}>{label}</Link>
                        ) : (
                          <Text type="secondary">{label}（{chapter.status === "failed" ? "未完成" : "待生成"}）</Text>
                        )}
                      </div>
                    );
                  })}
                </Space>
                {data.series.prerequisites.length > 0 && (
                  <div style={{ marginTop: 10 }}>
                    <Text type="secondary">前置阅读：{data.series.prerequisites.join("、")}</Text>
                  </div>
                )}
                <Space style={{ marginTop: 12 }}>
                  {(() => {
                    const chapters: SeriesChapterNav[] = data.series?.chapters || [];
                    const order = data.series?.order ?? 0;
                    const prev = [...chapters].reverse().find(
                      (item) => item.order < order && item.draft_id);
                    const next = chapters.find(
                      (item) => item.order > order && item.draft_id);
                    return (
                      <>
                        <Button size="small" disabled={!prev}
                          onClick={() => prev && navigate(`/drafts/${prev.draft_id}/edit`)}>
                          上一章
                        </Button>
                        <Button size="small" disabled={!next}
                          onClick={() => next && navigate(`/drafts/${next.draft_id}/edit`)}>
                          下一章
                        </Button>
                      </>
                    );
                  })()}
                </Space>
              </Card>
            )}
            <Card size="small" title={isSearchDraft ? `参考来源（${searchSources.length}）` : "来源"}
              style={{ marginTop: 12 }}>
              {isSearchDraft && searchSources.length > 0 ? (
                <Space direction="vertical" size={8} style={{ width: "100%" }}>
                  {searchSources.map((source: any, index: number) => {
                    const url = source.url || source.source_url || "";
                    const label = source.title || source.source_name || source.name || url || `来源 ${index + 1}`;
                    return (
                      <div key={`${url}-${index}`} style={{ lineHeight: 1.35 }}>
                        <Text type="secondary" style={{ marginRight: 5 }}>[{index + 1}]</Text>
                        {url
                          ? <a href={url} target="_blank" rel="noopener noreferrer">{label}</a>
                          : <Text>{label}</Text>}
                        {source.published_at && (
                          <div><Text type="secondary" style={{ fontSize: 11 }}>{source.published_at}</Text></div>
                        )}
                      </div>
                    );
                  })}
                </Space>
              ) : data.source_url ? (
                <a href={data.source_url} target="_blank" rel="noopener noreferrer">
                  {data.source_name || data.source_url}
                </a>
              ) : (
                <Text type="secondary">暂无来源信息</Text>
              )}
            </Card>
            {isSearchDraft && data.search_meta && (
              <Card size="small" title="搜索参数" style={{ marginTop: 12 }}>
                <Descriptions size="small" column={1}>
                  <Descriptions.Item label="语言">
                    {({ zh: "中文", en: "English", bilingual: "中英双语" } as Record<string, string>)[data.search_meta.lang || ""] || data.search_meta.lang || "—"}
                  </Descriptions.Item>
                  <Descriptions.Item label="时间范围">
                    {data.search_meta.time_range_days
                      ? `最近 ${data.search_meta.time_range_days} 天`
                      : "不限"}
                  </Descriptions.Item>
                  <Descriptions.Item label="参考篇数">
                    {data.search_meta.ref_count || searchSources.length}
                  </Descriptions.Item>
                  <Descriptions.Item label="搜索引擎">
                    <Space size={[4, 4]} wrap>
                      {(data.search_meta.engines || ["duckduckgo"]).map((engine: string) => (
                        <Tag key={engine}>{engine}</Tag>
                      ))}
                    </Space>
                  </Descriptions.Item>
                  {data.search_meta.style_id && (
                    <Descriptions.Item label="写作风格">
                      {data.search_meta.style_id}
                    </Descriptions.Item>
                  )}
                </Descriptions>
              </Card>
            )}
            {isSearchDraft && citations.length > 0 && (
              <Card size="small" title={`引用（${citations.length}）`} style={{ marginTop: 12 }}>
                <Space direction="vertical" size={10} style={{ width: "100%" }}>
                  {citations.map((citation: any, index: number) => {
                    if (typeof citation === "string") return <Text key={index}>{citation}</Text>;
                    const url = citation.url || citation.source_url || "";
                    const claim = citation.claim || citation.text || citation.title || `引用 ${index + 1}`;
                    const sourceLabel = citation.source_title || citation.source_name || url;
                    const sourceIndexes = Array.isArray(citation.source_indexes)
                      ? citation.source_indexes.filter((value: unknown) => Number.isInteger(value))
                      : [];
                    return (
                      <div key={`${url}-${index}`} style={{ fontSize: 12, lineHeight: 1.5 }}>
                        <Text>{claim}</Text>
                        {citation.quote && (
                          <div><Text type="secondary">“{citation.quote}”</Text></div>
                        )}
                        {sourceIndexes.length > 0 && (
                          <div>
                            <Text type="secondary">来源：</Text>
                            {sourceIndexes.map((sourceIndex: number, sourcePosition: number) => {
                              const source = searchSources[sourceIndex - 1];
                              const sourceUrl = source?.url || source?.source_url || "";
                              return (
                                <span key={`${sourceIndex}-${sourcePosition}`}>
                                  {sourcePosition > 0 && "、"}
                                  {sourceUrl
                                    ? <a href={sourceUrl} target="_blank" rel="noopener noreferrer">[{sourceIndex}]</a>
                                    : <Text type="secondary">[{sourceIndex}]</Text>}
                                </span>
                              );
                            })}
                          </div>
                        )}
                        {sourceLabel && (
                          <div>
                            <Text type="secondary">来源：</Text>
                            {url
                              ? <a href={url} target="_blank" rel="noopener noreferrer">{sourceLabel}</a>
                              : <Text type="secondary">{sourceLabel}</Text>}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </Space>
              </Card>
            )}
            {data.article_id && (
              <Card size="small" title="操作" style={{ marginTop: 12 }}>
                <Space direction="vertical" style={{ width: "100%" }}>
                  <Select size="small" style={{ width: "100%" }} value={rewriteStyle} onChange={setRewriteStyle} options={styleOptions} />
                  <Button size="small" loading={rewriting} onClick={rewrite}>重写（覆盖主稿）</Button>
                </Space>
              </Card>
            )}
          </Col>
        )}

        <Col flex="auto" className="ma-draft-right-col">
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
                  type={a.primary ? "primary" : "default"} title={a.title}
                  disabled={platform === "wechat" && wechatPublishing !== null}
                  loading={platform === "wechat" && wechatPublishing === a.mode}
                  onClick={a.onClick}>{a.label}</Button>
              ))}
              {platform && <Button size="small" onClick={onCopyHtml} title="复制当前平台美化 HTML 到剪贴板">复制HTML</Button>}
            </Space>

            <Input placeholder="文章中文标题" value={titleCn}
              onChange={(e) => setTitleCn(e.target.value)}
              status={titleCn.length > TITLE_CUTOFF ? "warning" : undefined}
              suffix={titleCn ? (
                <Text type={titleCn.length > TITLE_CUTOFF ? "danger" : "secondary"}>
                  {titleCn.length} / {TITLE_CUTOFF}
                </Text>
              ) : <span />}
              style={{ marginBottom: 8, flexShrink: 0 }} />
            <Input.TextArea placeholder="候选标题（每行一个）" value={titleCands} onChange={(e) => setTitleCands(e.target.value)} rows={2} style={{ marginBottom: 8, flexShrink: 0 }} />
            <TitleCandidateList text={titleCands} scores={data.title_scores || []}
              onPick={setTitleCn} />
            <Input.TextArea placeholder="摘要（订阅号列表、转发卡片和搜一搜里显示的就是这段；留空则自动截取正文开头）"
              value={digest} onChange={(e) => setDigest(e.target.value)} rows={2}
              maxLength={120} showCount={{ formatter: ({ count }) => `${count} / 建议 50-60` }}
              style={{ marginBottom: 16, flexShrink: 0 }} />

            <div ref={editorRef} className="ma-editor-wrap" data-color-mode="light" onKeyDownCapture={onEditorKeyDown}
              style={{ "--ma-split": `${split}%` } as React.CSSProperties}>
              <MDEditor value={body} onChange={handleChange} height={editorHeight}
                preview="live" commands={editorCommands}
                previewOptions={{
                  remarkPlugins: [remarkAppDirectives],
                  components: previewComponents,
                }} />
              {paneEl && createPortal(
                <div className="ma-split-handle" role="separator" onPointerDown={dragSplit}
                  onDoubleClick={() => setSplit(50)}
                  title="拖动调整编辑区 / 预览区宽度（双击恢复各一半）" />,
                paneEl)}
            </div>
          </Card>
        </Col>
      </Row>

      <FlavorDrawer open={flavorOpen} result={flavor} busy={flavorBusy}
        onClose={() => setFlavorOpen(false)} onRecheck={checkFlavor}
        onJump={jumpToLine} />
      <ComponentDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} onInsert={insertAtCursor} getSelection={getSelection}
        templates={tplData?.templates || []} templateCategories={tplData?.categories || []}
        hasContent={!!body.trim()} onApplyTemplate={applyTemplate} tplApplying={tplApplying} />
      <AgentModal open={agentOpen} draftId={data.id} body={body}
        titleCn={titleCn} titleCands={titleCands}
        onClose={() => setAgentOpen(false)}
        onApplied={(result) => {
          commit(result.body_md);
          setTitleCn(result.title_cn);
          setTitleCands(result.title_candidates.join("\n"));
          qc.setQueryData<DraftData>(["draft", data.id], (old) => old ? {
            ...old,
            body_md: result.body_md,
            title_cn: result.title_cn,
            title_candidates: result.title_candidates,
            status: result.status,
          } : old);
          qc.invalidateQueries({ queryKey: ["draft", data.id] });
          setAgentOpen(false);
          refocusEditor();
        }} />
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

/** AI 味体检的结果面板：总分、每个维度扣了多少分为什么扣，以及每一处命中
 *  在第几行。点一条就跳到正文里那一行。 */
function FlavorDrawer(
  { open, result, busy, onClose, onRecheck, onJump }: {
    open: boolean; result: FlavorResult | null; busy: boolean;
    onClose: () => void; onRecheck: () => void; onJump: (line: number) => void;
  },
) {
  const color = result ? flavorColor(result.score) : "#389e0d";
  return (
    <Drawer title="AI 味体检" open={open} onClose={onClose} width={430}
      extra={<Button size="small" loading={busy} onClick={onRecheck}>重新检测</Button>}>
      {result && (
        <>
          <div style={{ textAlign: "center", marginBottom: 20 }}>
            <div style={{ fontSize: 46, fontWeight: 600, lineHeight: 1.1, color }}>
              {result.score}
            </div>
            <Space size={6} style={{ marginTop: 6 }}>
              <Tag color={color} style={{ marginInlineEnd: 0 }}>{result.level}</Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>正文 {result.chars} 字</Text>
            </Space>
          </div>

          {result.dimensions.length === 0 ? (
            <Text type="secondary">没查出成片的套路话，这篇读起来不像模型写的。</Text>
          ) : (
            result.dimensions.map((d) => (
              <div key={d.key} style={{ marginBottom: 14 }}>
                <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                  <Text strong>{d.label}</Text>
                  {d.hits > 0 && (
                    <Text type="secondary" style={{ fontSize: 12 }}>{d.hits} 处</Text>
                  )}
                  <Text style={{ marginLeft: "auto", color, fontSize: 12 }}>
                    +{d.points}
                  </Text>
                </div>
                <Progress percent={Math.round((d.points / d.weight) * 100)}
                  showInfo={false} size="small" strokeColor={color} />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {d.note || d.advice}
                </Text>
              </div>
            ))
          )}

          {result.findings.length > 0 && (
            <>
              <Divider style={{ margin: "16px 0 12px" }}>逐条位置</Divider>
              {result.findings.map((f, i) => (
                <div key={`${f.line}-${i}`} className="ma-flavor-hit"
                  onClick={() => onJump(f.line)} title="点击跳到正文里的这一行">
                  <Tag style={{ marginInlineEnd: 0 }}>{f.label}</Tag>
                  <Text type="secondary" style={{ fontSize: 12 }}>第 {f.line} 行</Text>
                  <Text code ellipsis style={{ flex: 1, minWidth: 0 }}>{f.text}</Text>
                </div>
              ))}
              {result.dimensions.some((d) => d.shown < d.hits) && (
                <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
                  同一类命中太多时只列前 {result.dimensions[0].shown} 处，上面的计数是全的。
                </Text>
              )}
            </>
          )}

          <Divider style={{ margin: "16px 0 12px" }} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            分数看的是套路话扎堆的程度，偶尔冒一句不算数。这里只做提示，
            不会改稿，也不阻止发布。
          </Text>
        </>
      )}
    </Drawer>
  );
}

function ComponentDrawer({ open, onClose, onInsert, getSelection, templates, templateCategories, hasContent, onApplyTemplate, tplApplying }: {
  open: boolean; onClose: () => void; onInsert: (t: string) => void; getSelection: () => string;
  templates: any[]; templateCategories: string[]; hasContent: boolean; onApplyTemplate: (t: any) => void; tplApplying: boolean;
}) {
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
  const [ct, setCt] = useState({ id: "", name: "", markdown: "" });
  const [tcat, setTcat] = useState("");  // template filter: "" all, "__custom__", or a category
  const [mcat, setMcat] = useState("");  // material filter: "" all, or a category
  const filtered = (comps?.components || []).filter((c) =>
    (!cat || c.category === cat) &&
    (!q || (c.name + " " + (c.tags || []).join(" ") + " " + c.category).toLowerCase().includes(q.toLowerCase())));
  const groups = groupByCategory(filtered, comps?.categories || []);
  const customs = (comps?.components || []).filter((c) => !c.is_builtin);
  const customTpls = (templates || []).filter((t: any) => !t.is_builtin);

  // An unfocused textarea draws no highlight, so once this panel is open the
  // selection is invisible. Show what is waiting to be filled in.
  const [selected, setSelected] = useState("");
  useEffect(() => { if (open) setSelected(getSelection().trim()); }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  function pick(c: any) {
    onInsert(c.markdown);
    message.success(selected && PLACEHOLDER.test(c.markdown)
      ? `已把选中的 ${selected.length} 字填进：${c.name}`
      : `已插入：${c.name}`);
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

  async function saveCustomTpl() {
    const fd = new FormData();
    fd.set("name", ct.name); fd.set("markdown", ct.markdown);
    const url = ct.id ? `/api/editor/templates/${encodeURIComponent(ct.id)}` : "/api/editor/templates";
    const r = await fetch(url, { method: ct.id ? "PUT" : "POST", body: fd });
    const d = await r.json();
    if (!r.ok || !d.ok) { message.error(d.error || "保存失败"); return; }
    setCt({ id: "", name: "", markdown: "" });
    qc.invalidateQueries({ queryKey: ["editor-templates"] });
    message.success("已保存模板");
  }
  async function delCustomTpl(id: string) {
    await fetch(`/api/editor/templates/${encodeURIComponent(id)}`, { method: "DELETE" });
    qc.invalidateQueries({ queryKey: ["editor-templates"] });
  }

  return (
    <Drawer open={open} onClose={onClose} title="组件面板" width={560} zIndex={100000}
      className="ma-component-drawer">
      <Tabs items={[
        { key: "t", label: "套用模板", children: (
          <div data-testid="template-gallery">
            <Space wrap style={{ marginBottom: 10 }}>
              <Tag.CheckableTag checked={!tcat} onChange={() => setTcat("")}>全部</Tag.CheckableTag>
              {(templateCategories || []).map((c) => (
                <Tag.CheckableTag key={c} checked={tcat === c} onChange={() => setTcat(tcat === c ? "" : c)}>{c}</Tag.CheckableTag>
              ))}
              <Tag.CheckableTag checked={tcat === "__custom__"}
                onChange={() => setTcat(tcat === "__custom__" ? "" : "__custom__")}>自定义</Tag.CheckableTag>
            </Space>
            {tcat === "__custom__" ? (
              <Space direction="vertical" style={{ width: "100%" }}>
                <Text type="secondary">自定义模板（整篇文章结构，保存后出现在“全部”模板中）</Text>
                <Input placeholder="模板名称" value={ct.name} onChange={(e) => setCt({ ...ct, name: e.target.value })} />
                <Input.TextArea rows={5} placeholder="模板 Markdown 内容（整篇结构，可含 :::tip / ::::columns 等指令）"
                  value={ct.markdown} onChange={(e) => setCt({ ...ct, markdown: e.target.value })} />
                <Space>
                  <Button size="small" onClick={() => setCt({ ...ct, markdown: getSelection() })}>用选中文字填充</Button>
                  <Button size="small" type="primary" onClick={saveCustomTpl}>{ct.id ? "更新模板" : "保存模板"}</Button>
                  <Button size="small" onClick={() => setCt({ id: "", name: "", markdown: "" })}>清空</Button>
                </Space>
                {customTpls.length === 0 && <Text type="secondary">还没有自定义模板。</Text>}
                {customTpls.map((t: any) => (
                  <Card key={t.id} size="small" styles={{ body: { padding: 8 } }}>
                    <Space style={{ justifyContent: "space-between", width: "100%" }}>
                      <b>{t.name}</b>
                      <Space>
                        <Button size="small" onClick={() => onApplyTemplate(t)}>套用</Button>
                        <Button size="small" onClick={() => setCt({ id: t.id, name: t.name, markdown: t.markdown })}>编辑</Button>
                        <Button size="small" danger onClick={() => delCustomTpl(t.id)}>删除</Button>
                      </Space>
                    </Space>
                  </Card>
                ))}
              </Space>
            ) : (
              <>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {hasContent
                    ? "点击模板：用所选模板重写文章（保留事实与信息，套用模板的排版结构与视觉风格，可 Ctrl+Z 撤销）。"
                    : "点击模板：以该模板作为正文起稿（可 Ctrl+Z 撤销）。"}
                </Text>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginTop: 12,
                  opacity: tplApplying ? 0.6 : 1, pointerEvents: tplApplying ? "none" : "auto" }}>
                  {(templates || []).filter((t: any) => !tcat || t.category === tcat).map((t: any) => (
                    <div key={t.id} className="ma-comp-card" role="button"
                      title={hasContent ? `用此模板重写文章：${t.name}` : `套用：${t.name}`}
                      onClick={() => onApplyTemplate(t)}
                      style={{ border: "1px solid #eaeaea", borderRadius: 8, overflow: "hidden",
                        cursor: "pointer", background: "#fff" }}>
                      <div className="ma-comp-thumb" data-color-mode="light"
                        style={{ height: 200, overflow: "hidden", pointerEvents: "none",
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
              </>
            )}
          </div>
        ) },
        { key: "c", label: "组件库", children: (
          <div data-testid="component-gallery">
            {cat !== "__custom__" && (
              <Input.Search placeholder="搜索组件…" value={q} onChange={(e) => setQ(e.target.value)} style={{ marginBottom: 8 }} />
            )}
            <Space wrap style={{ marginBottom: 12 }}>
              <Tag.CheckableTag checked={!cat} onChange={() => setCat("")}>全部</Tag.CheckableTag>
              {(comps?.categories || []).map((c) => (
                <Tag.CheckableTag key={c} checked={cat === c} onChange={() => setCat(cat === c ? "" : c)}>{c}</Tag.CheckableTag>
              ))}
              <Tag.CheckableTag checked={cat === "__custom__"}
                onChange={() => setCat(cat === "__custom__" ? "" : "__custom__")}>自定义</Tag.CheckableTag>
            </Space>
            {cat === "__custom__" ? (
              <Space direction="vertical" style={{ width: "100%" }}>
                <Text type="secondary">自定义组件（把常用片段存为可复用组件）</Text>
                <Input placeholder="组件名称" value={cc.name} onChange={(e) => setCc({ ...cc, name: e.target.value })} />
                <Input placeholder="分类（默认：自定义）" value={cc.category} onChange={(e) => setCc({ ...cc, category: e.target.value })} />
                <Input.TextArea rows={4} placeholder="组件 Markdown 内容（可含 :::tip 等指令）" value={cc.markdown} onChange={(e) => setCc({ ...cc, markdown: e.target.value })} />
                <Space>
                  <Button size="small" onClick={() => setCc({ ...cc, markdown: getSelection() })}>用选中文字填充</Button>
                  <Button size="small" type="primary" onClick={saveCustom}>{cc.id ? "更新组件" : "保存组件"}</Button>
                  <Button size="small" onClick={() => setCc({ id: "", name: "", category: "", markdown: "" })}>清空</Button>
                </Space>
                {customs.length === 0 && <Text type="secondary">还没有自定义组件。</Text>}
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
            ) : (
              <>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {selected
                    ? `已选中 ${selected.length} 字：点击组件会把这段文字填进组件的第一个空位（组件没有空位时，插到选中文字后面，不会覆盖它）。`
                    : "点击组件在光标处插入。先在正文里选中一段文字再点，可以把它填进组件里。"}
                </Text>
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
              </>
            )}
          </div>
        ) },
        { key: "m", label: "素材库", children: (
          <div>
            <Input.Search placeholder="搜索素材…" value={mq} onChange={(e) => setMq(e.target.value)} style={{ marginBottom: 8 }} />
            <Space wrap style={{ marginBottom: 12 }}>
              <Tag.CheckableTag checked={!mcat} onChange={() => setMcat("")}>全部</Tag.CheckableTag>
              {Array.from(new Set((mats?.materials || []).map((m: any) => m.category))).map((c) => (
                <Tag.CheckableTag key={c as string} checked={mcat === c}
                  onChange={() => setMcat(mcat === c ? "" : (c as string))}>{c as string}</Tag.CheckableTag>
              ))}
            </Space>
            {groupByCategory(
              (mats?.materials || []).filter((m: any) =>
                (!mcat || m.category === mcat) &&
                (!mq || (m.name + " " + m.category + " " + m.markdown).toLowerCase().includes(mq.toLowerCase()))),
              [],
            ).map(({ cat: gcat, items }) => (
              <div key={gcat} style={{ marginBottom: 14 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, margin: "4px 0 8px" }}>
                  <Text strong style={{ fontSize: 13 }}>{gcat}</Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>{items.length}</Text>
                </div>
                <Space wrap>
                  {items.map((m: any) => {
                    const md = String(m.markdown || "");
                    const img = md.match(/^!\[[^\]]*\]\(([^)]+)\)/);
                    if (img) {
                      // Image material → thumbnail card; insert as a block.
                      return (
                        <Tooltip key={m.id} title={m.name}>
                          <div role="button" onClick={() => onInsert("\n" + md + "\n")}
                            style={{ cursor: "pointer", border: "1px solid #eaeaea", borderRadius: 6,
                              padding: 4, background: "#fff", width: 128 }}>
                            <img src={img[1]} alt={m.name}
                              style={{ width: "100%", height: 64, objectFit: "contain", display: "block" }} />
                            <div style={{ fontSize: 11, textAlign: "center", color: "#666", marginTop: 2,
                              whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{m.name}</div>
                          </div>
                        </Tooltip>
                      );
                    }
                    return (
                      <Tooltip key={m.id} title={m.name}>
                        <Button onClick={() => onInsert(md + " ")}
                          style={{ fontSize: 18, minWidth: 40 }}>{md}</Button>
                      </Tooltip>
                    );
                  })}
                </Space>
              </div>
            ))}
          </div>
        ) },
      ]} />
    </Drawer>
  );
}

interface AgentApplyResult {
  body_md: string;
  title_cn: string;
  title_candidates: string[];
  status: string;
}

function AgentModal({ open, draftId, body, titleCn, titleCands, onClose, onApplied }: {
  open: boolean;
  draftId: number;
  body: string;
  titleCn: string;
  titleCands: string;
  onClose: () => void;
  onApplied: (result: AgentApplyResult) => void;
}) {
  const { message } = AntApp.useApp();
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [agentStatus, setAgentStatus] = useState<"idle" | "running" | "completed" | "error" | "cancelled">("idle");
  const [elapsed, setElapsed] = useState(0);
  const [errorMsg, setErrorMsg] = useState("");
  const [resultBody, setResultBody] = useState("");
  const runIdRef = useRef("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Check backend status on modal open (survives page refresh)
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      try {
        const s = await getJson<{ ok: boolean; status: string; run_id?: string; elapsed_s?: number; body_md?: string; error?: string; prompt?: string }>(
          `/api/draft/${draftId}/agent-status`);
        if (cancelled) return;
        runIdRef.current = s.run_id || "";
        // Restore the instruction whatever the outcome was: a failed run has
        // to be editable and retryable, not just readable.
        if (s.prompt) setPrompt(s.prompt);
        if (s.status === "running") {
          setAgentStatus("running");
          setElapsed(s.elapsed_s || 0);
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
        const s = await getJson<{ ok: boolean; status: string; run_id?: string; elapsed_s?: number; body_md?: string; error?: string }>(
          `/api/draft/${draftId}/agent-status`);
        if (runIdRef.current && s.run_id && s.run_id !== runIdRef.current) {
          stopPolling();
          setAgentStatus("error");
          setErrorMsg("当前结果已被另一 Agent 任务替换，请重新打开后确认");
          return;
        }
        if (s.run_id) runIdRef.current = s.run_id;
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
    if (!open) {
      stopPolling();
      runIdRef.current = "";
      setAgentStatus("idle");
      setBusy(false);
      setResultBody("");
      setErrorMsg("");
      setElapsed(0);
    }
  }, [open]);

  async function run() {
    if (!prompt.trim()) return;
    setBusy(true);
    setAgentStatus("running");
    setResultBody("");
    setErrorMsg("");
    try {
      const r = await postForm<{ ok?: boolean; running?: boolean; error?: string; run_id?: string }>(
        `/api/draft/${draftId}/agent-edit`, {
          prompt, body_md: body, title_cn: titleCn, title_candidates: titleCands,
        });
      if (r.error) {
        setAgentStatus("error");
        setErrorMsg(r.error);
        setBusy(false);
        message.error(r.error);
      } else if (r.running) {
        runIdRef.current = r.run_id || "";
        startPolling();
        setBusy(false);
      } else {
        // Shouldn't happen with new async backend, but handle fallback
        setBusy(false);
        setAgentStatus("idle");
      }
    } catch (e: any) {
      setBusy(false);
      setAgentStatus("error");
      const detail = e?.response?.data?.error || e?.message || "请求失败";
      setErrorMsg(detail);
      message.error(detail);
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
    if (!resultBody || !runIdRef.current) return;
    setBusy(true);
    try {
      const applied = await postForm<AgentApplyResult>(
        `/api/draft/${draftId}/agent-apply`, { run_id: runIdRef.current });
      onApplied(applied);
      message.success("已应用 Agent 修改");
    } catch (e: any) {
      const detail = e?.response?.data?.error || e?.message || "应用 Agent 修改失败";
      setErrorMsg(detail);
      message.error(detail);
    } finally {
      setBusy(false);
    }
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
        // Without an instruction run() has nothing to send, and silently
        // doing nothing reads as "it failed again".
        disabled: isRunning || (!isCompleted && !prompt.trim()),
        type: isCompleted ? "primary" : undefined,
      }}
      onOk={isCompleted ? applyResult : run}
      cancelText={isRunning ? "取消执行" : "关闭"}
      cancelButtonProps={isRunning ? { danger: true } : undefined}
    >
      {isError && (
        <div style={{ padding: "8px 0 16px" }}>
          <div style={{ fontSize: 16, color: "#ff4d4f", marginBottom: 8 }}>❌ 执行失败</div>
          <Paragraph type="danger" style={{ margin: 0 }}>{errorMsg}</Paragraph>
        </div>
      )}

      {!isRunning && !isCompleted && (
        <>
          <Paragraph type="secondary">
            {isError
              ? "修改指令后可以再次执行，或关闭窗口。"
              : "描述你想让 Agent 做的修改（需在设置里配置 CLI Agent）。"}
          </Paragraph>
          <Input.TextArea rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)}
            placeholder="如：把第三段改得更口语化，并补充一个类比" />
        </>
      )}

      {isRunning && (
        <div style={{ textAlign: "center", padding: "24px 0" }}>
          <div style={{ fontSize: 16, marginBottom: 8 }}>🤖 Agent 正在处理…</div>
          <div style={{ fontSize: 24, fontFamily: "monospace", color: "#1890ff" }}>{formatTime(elapsed)}</div>
          <div style={{ marginTop: 12, color: "#999", fontSize: 13 }}>完成后请点击「应用修改」写入编辑器</div>
        </div>
      )}

      {isCompleted && (
        <div style={{ textAlign: "center", padding: "16px 0" }}>
          <div style={{ fontSize: 16, color: "#52c41a", marginBottom: 8 }}>✅ Agent 修改完成</div>
          <Paragraph type="secondary">点击「应用修改」将结果写入编辑器。</Paragraph>
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
  const [videoCollapsed, setVideoCollapsed] = useLocalState<boolean>("videotab-video-collapsed", false);
  // Keep the player in sync with the persisted state (e.g. after a server
  // restart / page reload the draft API reports has_video from disk).
  useEffect(() => { setHasVideo(data.has_video); }, [data.has_video]);

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
      if (!s.running) { clearInterval(poll); setNarrating(false); setNarrLog([]); s.error ? message.error("生成失败：" + s.error) : message.success("讲解脚本+配音已生成"); qc.invalidateQueries({ queryKey: ["script", data.id] }); qc.invalidateQueries({ queryKey: ["draft", data.id] }); }
    }, 1500);
    narrPollRef.current = poll;
  }
  async function synthVideo() {
    setSynth(true);
    await postForm(`/drafts/${data.id}/video`);
    if (synthPollRef.current) clearInterval(synthPollRef.current);
    const poll = setInterval(async () => {
      const s = await getJson<{ running: boolean; error: string | null }>(`/api/video-status?draft_id=${data.id}`);
      if (!s.running) { clearInterval(poll); setSynth(false); if (s.error) message.error("合成失败：" + s.error); else { message.success("视频已合成"); setHasVideo(true); qc.invalidateQueries({ queryKey: ["draft", data.id] }); } }
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
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <Text strong>已合成视频</Text>
              <Button size="small" type="text" onClick={() => setVideoCollapsed(!videoCollapsed)}
                title={videoCollapsed ? "展开视频预览" : "收起视频预览（腾出空间给分镜）"}>
                {videoCollapsed ? "展开 ▾" : "收起 ▴"}
              </Button>
            </div>
            {!videoCollapsed && (
              // Video preview is capped in height and sits beside its action
              // buttons so it never crowds out the 分镜 editor below.
              <div style={{ display: "flex", gap: 12, alignItems: "flex-start", flexWrap: "wrap" }}>
                <video controls preload="metadata"
                  style={{ maxHeight: "30vh", maxWidth: "min(100%, 520px)", borderRadius: 8, background: "#000" }}
                  src={`/videos/draft-${data.id}/video.mp4`} />
                <Space direction="vertical" size="small">
                  <a href={`/videos/draft-${data.id}/video.mp4`} download><Button size="small">下载 mp4</Button></a>
                  {data.platforms.filter((p) => p.video_publish_url).map((p) => (
                    <a key={p.id} href={p.video_publish_url!} target="_blank" rel="noopener"><Button size="small">{p.label}</Button></a>
                  ))}
                  <Button size="small" className="pro-feature" onClick={prepareChannels}>视频号发布（半自动）</Button>
                </Space>
              </div>
            )}
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
