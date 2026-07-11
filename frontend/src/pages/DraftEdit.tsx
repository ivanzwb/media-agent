import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App as AntApp, Button, Card, Col, Row, Select, Space, Tabs, Tag, Typography,
  Input, Alert, Modal, Drawer, Tooltip, Divider,
} from "antd";
import MDEditor from "@uiw/react-md-editor";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, getJson, postForm } from "../api/client";
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
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Space style={{ justifyContent: "space-between", width: "100%" }}>
        <Title level={2} style={{ margin: 0 }}>编辑草稿 #{draftId}</Title>
        <Button onClick={() => navigate(-1)}>返回列表</Button>
      </Space>

      <Tabs items={[
        {
          key: "article", label: "文章内容", children: (
            <ArticleTab data={data} body={body} setBody={setBody} titleCn={titleCn}
              setTitleCn={setTitleCn} titleCands={titleCands} setTitleCands={setTitleCands}
              status={status} setStatus={setStatus} theme={theme} setTheme={setTheme}
              onSave={save} />
          ),
        },
        {
          key: "video", label: "讲解视频", children: <VideoTab data={data} />,
        },
      ]} />
    </Space>
  );
}

function ArticleTab({ data, body, setBody, titleCn, setTitleCn, titleCands, setTitleCands,
  status, setStatus, theme, setTheme, onSave }: any) {
  const qc = useQueryClient();
  const { message } = AntApp.useApp();
  const editorRef = useRef<HTMLDivElement>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [agentOpen, setAgentOpen] = useState(false);
  const [rewriting, setRewriting] = useState(false);
  const [rewriteStyle, setRewriteStyle] = useState("");

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

  async function rewrite() {
    if (!data.article_id) { message.warning("该草稿无关联文章，无法重写"); return; }
    if (!confirm("重写会覆盖当前主稿，确定吗？")) return;
    setRewriting(true);
    try {
      await postForm(`/archive/${data.article_id}/rewrite`, rewriteStyle ? { style: rewriteStyle } : {});
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
    const r = await postForm<{ ok: boolean; error?: string }>(`/drafts/${data.id}/publish/wechat`, { mode, kind: "article", theme });
    if (r.ok) message.success({ content: mode === "draft" ? "已推送到草稿箱" : "已发布", key: "wx" });
    else message.error({ content: r.error || "推送失败", key: "wx" });
  }

  async function copyStyledHtml(platform: "wechat" | "toutiao", openUrl?: string) {
    const r = await postForm<{ ok: boolean; html?: string; error?: string }>(`/drafts/${data.id}/styled-html`, { platform, theme: platform === "wechat" ? theme : "default" });
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

  async function adapt(platform: string) {
    message.loading({ content: "适配中…", key: "adapt" });
    try { await postForm(`/drafts/${data.id}/adapt`, { platform }); message.success({ content: "已生成适配文案", key: "adapt" }); }
    catch { message.error({ content: "适配失败", key: "adapt" }); }
  }

  const styleOptions = [{ value: "", label: "默认（全局）" },
    ...(styleData?.styles || []).map((s) => ({ value: s.id, label: s.name + (s.is_builtin ? "" : "（自定义）") }))];

  return (
    <>
      {data.flagged_claims?.length > 0 && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }}
          message={`事实校验存疑（${data.flagged_claims.length} 项）`}
          description={<ul style={{ margin: 0 }}>{data.flagged_claims.map((c: string, i: number) => <li key={i}>{c}</li>)}</ul>} />
      )}
      {data.sensitive_hits?.length > 0 && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }}
          message={`已过滤敏感/违禁词（${data.sensitive_hits.length} 项）`}
          description={data.sensitive_hits.join("、")} />
      )}

      <Row gutter={16}>
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

        <Col span={18}>
          <Card size="small" styles={{ body: { paddingBottom: 8 } }}>
            <Space wrap style={{ marginBottom: 12 }}>
              <Select value={status} onChange={setStatus} style={{ width: 120 }}
                options={data.statuses.map((s: string) => ({ value: s }))} />
              <Button type="primary" onClick={onSave}>保存</Button>
              <Divider type="vertical" />
              <Text type="secondary">同步到平台：</Text>
              {data.platforms.map((p: any) => <Button key={p.id} size="small" onClick={() => adapt(p.id)}>{p.label}</Button>)}
              <Divider type="vertical" />
              <Text type="secondary">公众号：</Text>
              <Select size="small" value={theme} onChange={setTheme} style={{ width: 130 }}
                options={data.wechat_themes.map((t: any) => ({ value: t.id, label: t.name }))} />
              <Button size="small" className="pro-feature" onClick={() => publishWeChat("draft")}>推送草稿箱</Button>
              <Button size="small" className="pro-feature" onClick={() => publishWeChat("publish")}>直接发布</Button>
              <Button size="small" className="pro-feature" onClick={() => copyStyledHtml("wechat")}>复制美化HTML</Button>
              <Divider type="vertical" />
              <Button size="small" className="pro-feature" onClick={() => copyStyledHtml("toutiao", "https://mp.toutiao.com/profile_v4/graphic/publish")}>复制并打开头条</Button>
              <Button size="small" onClick={() => setAgentOpen(true)}>Agent 修改</Button>
            </Space>

            <Input placeholder="文章中文标题" value={titleCn} onChange={(e) => setTitleCn(e.target.value)} style={{ marginBottom: 8 }} />
            <Input.TextArea placeholder="候选标题（每行一个）" value={titleCands} onChange={(e) => setTitleCands(e.target.value)} rows={2} style={{ marginBottom: 8 }} />

            <Space wrap style={{ marginBottom: 8 }}>
              <Button size="small" onClick={() => wrapSelection("==", "==", "高亮")}>高亮</Button>
              <Button size="small" onClick={() => { const c = prompt("颜色（如 #e67514）", "#e67514"); if (c) wrapSelection(`{color:${c}}`, "{/color}", "彩色文字"); }}>彩色字</Button>
              <Button size="small" onClick={() => insertAtCursor("\n:::center\n居中文字\n:::\n\n")}>居中</Button>
              <Button size="small" onClick={() => insertAtCursor("\n:::tip\n💡 提示内容\n:::\n\n")}>提示卡片</Button>
              <Button size="small" onClick={() => insertAtCursor("\n> 引用文字\n\n")}>引用</Button>
              <Button size="small" type="primary" onClick={() => setDrawerOpen(true)}>组件面板 ▸</Button>
            </Space>

            <div ref={editorRef} data-color-mode="light">
              <MDEditor value={body} onChange={(v) => setBody(v || "")} height={560} preview="live" />
            </div>
          </Card>
        </Col>
      </Row>

      <ComponentDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} onInsert={insertAtCursor} />
      <AgentModal open={agentOpen} draftId={data.id} onClose={() => setAgentOpen(false)}
        onApplied={(md) => { setBody(md); setAgentOpen(false); }} />
    </>
  );
}

function ComponentDrawer({ open, onClose, onInsert }: { open: boolean; onClose: () => void; onInsert: (t: string) => void; }) {
  const { data: comps } = useQuery({
    queryKey: ["editor-components"], enabled: open,
    queryFn: () => getJson<{ categories: string[]; components: any[] }>("/api/editor/components"),
  });
  const { data: tpls } = useQuery({
    queryKey: ["editor-templates"], enabled: open,
    queryFn: () => getJson<{ templates: any[] }>("/api/editor/templates"),
  });
  const { data: mats } = useQuery({
    queryKey: ["editor-materials"], enabled: open,
    queryFn: () => getJson<{ materials: any[] }>("/api/editor/materials"),
  });
  const [q, setQ] = useState("");
  const filtered = (comps?.components || []).filter((c) =>
    !q || (c.name + " " + (c.tags || []).join(" ") + " " + c.category).toLowerCase().includes(q.toLowerCase()));

  return (
    <Drawer open={open} onClose={onClose} title="组件面板" width={400}>
      <Tabs items={[
        { key: "c", label: "组件库", children: (
          <>
            <Input.Search placeholder="搜索组件…" value={q} onChange={(e) => setQ(e.target.value)} style={{ marginBottom: 8 }} />
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
        { key: "t", label: "模板库", children: (
          <Space direction="vertical" style={{ width: "100%" }}>
            {(tpls?.templates || []).map((t) => (
              <Card key={t.id} size="small" styles={{ body: { padding: 8 } }}>
                <Space style={{ justifyContent: "space-between", width: "100%" }}>
                  <b>{t.name}</b>
                  <Button size="small" type="primary" onClick={() => onInsert("\n" + t.markdown + "\n")}>应用</Button>
                </Space>
              </Card>
            ))}
          </Space>
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
      ]} />
    </Drawer>
  );
}

function AgentModal({ open, draftId, onClose, onApplied }: { open: boolean; draftId: number; onClose: () => void; onApplied: (md: string) => void; }) {
  const { message } = AntApp.useApp();
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  async function run() {
    if (!prompt.trim()) return;
    setBusy(true);
    try {
      const r = await postForm<{ ok?: boolean; body_md?: string; error?: string; running?: boolean }>(`/api/draft/${draftId}/agent-edit`, { prompt });
      if (r.body_md) { onApplied(r.body_md); message.success("已应用 Agent 修改"); }
      else if (r.error) message.error(r.error);
      else message.info("Agent 已开始处理，请稍后刷新查看");
    } catch { message.error("请求失败"); }
    finally { setBusy(false); }
  }
  return (
    <Modal open={open} onCancel={onClose} title="用 CLI Agent 修改正文" okText="执行" confirmLoading={busy} onOk={run}>
      <Paragraph type="secondary">描述你想让 Agent 做的修改（需在设置里配置 CLI Agent）。</Paragraph>
      <Input.TextArea rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)}
        placeholder="如：把第三段改得更口语化，并补充一个类比" />
    </Modal>
  );
}

function VideoTab({ data }: { data: DraftData }) {
  const { message } = AntApp.useApp();
  const [narrating, setNarrating] = useState(false);
  const [synth, setSynth] = useState(false);
  const [hasVideo, setHasVideo] = useState(data.has_video);
  const [voice, setVoice] = useState<string | undefined>(undefined);

  const { data: voices } = useQuery({ queryKey: ["voices"], queryFn: () => getJson<{ voices: any[] }>("/api/voices") });

  async function genNarration() {
    setNarrating(true);
    await postForm(`/drafts/${data.id}/narration`);
    const poll = setInterval(async () => {
      const s = await getJson<{ running: boolean; error: string | null }>(`/api/narration-status?draft_id=${data.id}`);
      if (!s.running) { clearInterval(poll); setNarrating(false); s.error ? message.error("生成失败：" + s.error) : message.success("讲解脚本+配音已生成"); }
    }, 1500);
  }
  async function synthVideo() {
    setSynth(true);
    await postForm(`/drafts/${data.id}/video`);
    const poll = setInterval(async () => {
      const s = await getJson<{ running: boolean; error: string | null }>(`/api/video-status?draft_id=${data.id}`);
      if (!s.running) { clearInterval(poll); setSynth(false); if (s.error) message.error("合成失败：" + s.error); else { message.success("视频已合成"); setHasVideo(true); } }
    }, 2000);
  }
  async function prepareChannels() {
    const r = await postForm<{ ok?: boolean; caption?: string; url?: string }>(`/drafts/${data.id}/wechat-channels/prepare`);
    if (r.caption) { await navigator.clipboard.writeText(r.caption).catch(() => {}); }
    if (r.url) window.open(r.url, "_blank", "noopener");
    message.success("文案已复制，已打开发布页");
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Paragraph type="secondary">基于正文自动生成口播分镜脚本并逐段配音，再合成 mp4（需本地 ffmpeg）。</Paragraph>
      <Space wrap>
        <Text>TTS 音色：</Text>
        <Select style={{ width: 220 }} placeholder="选择音色" value={voice} onChange={setVoice}
          options={(voices?.voices || []).map((v: any) => ({ value: v.id, label: v.name || v.id }))} />
        <Button type="primary" className="pro-feature" loading={narrating} onClick={genNarration}>
          {data.has_narration ? "重新生成讲解脚本+配音" : "生成讲解脚本+配音"}
        </Button>
      </Space>
      <Space wrap>
        <Button type="primary" className="pro-feature" loading={synth} onClick={synthVideo}>
          {hasVideo ? "重新合成讲解视频" : "合成讲解视频（mp4）"}
        </Button>
      </Space>
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
  );
}
