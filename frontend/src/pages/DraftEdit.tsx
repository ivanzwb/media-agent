import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App as AntApp, Button, Card, Col, Row, Select, Space, Tabs, Tag, Typography,
  Input, Collapse, Modal, Drawer, Tooltip, Divider, FloatButton,
} from "antd";
import { RobotOutlined } from "@ant-design/icons";
import MDEditor, { commands, type ICommand } from "@uiw/react-md-editor";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, getJson, postForm } from "../api/client";
import { useLocalState } from "../api/hooks";
import SceneEditor from "../components/SceneEditor";

const { Title, Text, Paragraph } = Typography;

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
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [agentOpen, setAgentOpen] = useState(false);
  const [rewriting, setRewriting] = useLocalState<boolean>(`draftedit-rewriting-${data.id}`, false);
  const [rewriteStyle, setRewriteStyle] = useState("");
  const [platform, setPlatform] = useState("");
  const TOUTIAO_URL = "https://mp.toutiao.com/profile_v4/graphic/publish";

  // Track active poll interval so we can clear on unmount
  const rewritePollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    return () => { if (rewritePollRef.current) clearInterval(rewritePollRef.current); };
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

  const { data: styleData } = useQuery({
    queryKey: ["rewrite-styles"],
    queryFn: () => getJson<{ styles: { id: string; name: string; is_builtin: boolean }[] }>("/api/rewrite-styles"),
  });

  // insert text at the markdown textarea cursor
  function insertAtCursor(text: string) {
    const ta = editorRef.current?.querySelector<HTMLTextAreaElement>(".w-md-editor-text-input");
    if (!ta) { setBody(body + text); return; }
    const s = ta.selectionStart ?? body.length;
    const e = ta.selectionEnd ?? body.length;
    const next = body.slice(0, s) + text + body.slice(e);
    setBody(next);
    setTimeout(() => { ta.focus(); ta.selectionStart = ta.selectionEnd = s + text.length; }, 0);
  }
  function wrapSelection(before: string, after: string, placeholder: string) {
    const ta = editorRef.current?.querySelector<HTMLTextAreaElement>(".w-md-editor-text-input");
    if (!ta) return;
    const s = ta.selectionStart ?? 0, e = ta.selectionEnd ?? 0;
    const sel = body.slice(s, e) || placeholder;
    const next = body.slice(0, s) + before + sel + after + body.slice(e);
    setBody(next);
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
  function undo() {
    if (!pastRef.current.length) return;
    futureRef.current.push(body);
    setBody(pastRef.current.pop()!);
    lastPushRef.current = 0;
  }
  function redo() {
    if (!futureRef.current.length) return;
    pastRef.current.push(body);
    setBody(futureRef.current.pop()!);
    lastPushRef.current = 0;
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
    { name: "hl", keyCommand: "hl", buttonProps: { title: "高亮" }, icon: label("高亮"),
      execute: (s, api) => api.replaceSelection(`==${s.selectedText || "高亮"}==`) },
    { name: "color", keyCommand: "color", buttonProps: { title: "彩色字" }, icon: label("彩色字"),
      execute: (s, api) => { const c = prompt("颜色（如 #e67514）", "#e67514"); if (c) api.replaceSelection(`{color:${c}}${s.selectedText || "彩色文字"}{/color}`); } },
    { name: "center", keyCommand: "center", buttonProps: { title: "居中" }, icon: label("居中"),
      execute: (s, api) => api.replaceSelection(`\n:::center\n${s.selectedText || "居中文字"}\n:::\n\n`) },
    { name: "tip", keyCommand: "tip", buttonProps: { title: "提示卡片" }, icon: label("提示卡片"),
      execute: (s, api) => api.replaceSelection(`\n:::tip\n💡 ${s.selectedText || "提示内容"}\n:::\n\n`) },
    { name: "quote2", keyCommand: "quote2", buttonProps: { title: "引用" }, icon: label("引用"),
      execute: (s, api) => api.replaceSelection((s.selectedText || "引用文字").split("\n").map((l) => `> ${l}`).join("\n")) },
  ];
  const templateGroup: ICommand = commands.group(
    (tplData?.templates || []).map((t) => ({
      name: t.id, keyCommand: t.id, buttonProps: { title: t.name }, icon: label(t.name),
      execute: (_s: any, api: any) => api.replaceSelection(`\n${t.markdown}\n`),
    })),
    { name: "template", groupName: "template", buttonProps: { title: "套用模板" }, icon: label("套用模板 ▾") },
  );
  const panelCommand: ICommand = {
    name: "panel", keyCommand: "panel", buttonProps: { title: "组件面板" }, icon: label("组件面板 ▸"),
    execute: () => setDrawerOpen(true),
  };
  const editorCommands: ICommand[] = [
    ...commands.getCommands(), commands.divider,
    ...styleCommands, commands.divider, templateGroup, panelCommand,
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
                preview="live" commands={editorCommands} />
            </div>
          </Card>
        </Col>
      </Row>

      <ComponentDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} onInsert={insertAtCursor} getSelection={getSelection} />
      <AgentModal open={agentOpen} draftId={data.id} onClose={() => setAgentOpen(false)}
        onApplied={(md) => { setBody(md); setAgentOpen(false); }} />
      <FloatButton icon={<RobotOutlined />} type="primary" tooltip="Agent 编辑"
        onClick={() => setAgentOpen(true)} />
    </div>
  );
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
  const [cat, setCat] = useState("");
  const [cc, setCc] = useState({ id: "", name: "", category: "", markdown: "" });
  const filtered = (comps?.components || []).filter((c) =>
    (!cat || c.category === cat) &&
    (!q || (c.name + " " + (c.tags || []).join(" ") + " " + c.category).toLowerCase().includes(q.toLowerCase())));
  const customs = (comps?.components || []).filter((c) => !c.is_builtin);

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
    <Drawer open={open} onClose={onClose} title="组件面板" width={400}>
      <Tabs items={[
        { key: "c", label: "组件库", children: (
          <>
            <Input.Search placeholder="搜索组件…" value={q} onChange={(e) => setQ(e.target.value)} style={{ marginBottom: 8 }} />
            <Space wrap style={{ marginBottom: 8 }}>
              <Tag.CheckableTag checked={!cat} onChange={() => setCat("")}>全部</Tag.CheckableTag>
              {(comps?.categories || []).map((c) => (
                <Tag.CheckableTag key={c} checked={cat === c} onChange={() => setCat(c)}>{c}</Tag.CheckableTag>
              ))}
            </Space>
            <Space direction="vertical" style={{ width: "100%" }}>
              {filtered.map((c) => (
                <Card key={c.id} size="small" styles={{ body: { padding: 8 } }}>
                  <Space style={{ justifyContent: "space-between", width: "100%" }}>
                    <span><b>{c.name}</b> <Tag>{c.category}</Tag></span>
                    <Button size="small" type="primary" onClick={() => onInsert(c.markdown)}>插入</Button>
                  </Space>
                </Card>
              ))}
            </Space>
          </>
        ) },
        { key: "m", label: "素材库", children: (
          <Space wrap>
            {(mats?.materials || []).map((m) => (
              <Tooltip key={m.id} title={m.name}>
                <Button onClick={() => onInsert(m.markdown + " ")} style={{ fontSize: 18 }}>{m.markdown}</Button>
              </Tooltip>
            ))}
          </Space>
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

  function applyResult() {
    if (resultBody) {
      onApplied(resultBody);
      message.success("已应用 Agent 修改");
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
    <div style={{ overflowY: "auto", height: "100%" }}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Paragraph type="secondary">基于正文自动生成口播分镜脚本并逐段配音，再合成 mp4（需本地 ffmpeg）。</Paragraph>
        <Space wrap>
          <Text>TTS 音色：</Text>
          <Select style={{ width: 220 }} placeholder="选择音色" value={voice} onChange={setVoice}
            options={voices.map((v) => ({ value: v.id, label: v.label }))} />
          <Button type="primary" className="pro-feature" loading={narrating} onClick={genNarration}>
            {data.has_narration ? "重新生成讲解脚本+配音" : "生成讲解脚本+配音"}
          </Button>
        </Space>
        <Space wrap>
          <Button type="primary" className="pro-feature" loading={synth} onClick={synthVideo}>
            {hasVideo ? "重新合成讲解视频" : "合成讲解视频（mp4）"}
          </Button>
        </Space>
        {narrating && narrLog.length > 0 && (
          <Card size="small" title="生成进度" style={{ background: "#fafafa" }}>
            <div style={{ maxHeight: 200, overflowY: "auto", fontFamily: "monospace", fontSize: 12 }}>
              {narrLog.map((line, i) => <div key={i}>{line}</div>)}
            </div>
          </Card>
        )}
        {hasVideo && (
          <div>
            <video controls preload="metadata" style={{ maxWidth: "100%", borderRadius: 8 }} src={`/videos/draft-${data.id}/video.mp4`} />
            <div style={{ marginTop: 8 }}>
              <Space wrap>
                <a href={`/videos/draft-${data.id}/video.mp4`} download><Button size="small">下载 mp4</Button></a>
                {data.platforms.filter((p) => p.video_publish_url).map((p) => (
                  <a key={p.id} href={p.video_publish_url!} target="_blank" rel="noopener"><Button size="small">{p.label}</Button></a>
                ))}
                <Button size="small" className="pro-feature" onClick={prepareChannels}>视频号发布（半自动）</Button>
              </Space>
            </div>
          </div>
        )}
        <Divider>分镜编辑</Divider>
        <SceneEditor draftId={data.id} ttsVoice={voice} />
      </Space>
    </div>
  );
}
