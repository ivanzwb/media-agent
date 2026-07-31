import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App as AntApp, Button, Card, Collapse, ConfigProvider, Form, Input,
  List, Progress, Select, Space, Tag, Typography,
} from "antd";
import {
  ArrowDownOutlined, ArrowUpOutlined, BookOutlined, CloseCircleOutlined,
  DeleteOutlined, PlusOutlined,
} from "@ant-design/icons";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { useEffect, useMemo, useState } from "react";
import { Link, useOutletContext } from "react-router-dom";
import { api, getJson } from "../api/client";
import { useLicense } from "../api/hooks";

const { Title, Paragraph, Text } = Typography;

interface RewriteStyle {
  id: string;
  name: string;
  is_builtin?: boolean;
}

interface Chapter {
  id: number;
  order: number;
  title: string;
  scope: string;
  status: string;
  error: string | null;
  draft_id: number | null;
  prerequisites: string[];
  search_queries: string[];
  preflight_hits: number | null;
}

interface Series {
  id: number;
  title: string;
  topic: string;
  parts: number;
  ref_count: number;
  status: string;
  error: string | null;
  knowledge_map: string;
  chapters: Chapter[];
  chapters_done: number;
  chapters_failed: number;
  created_at: string;
}

interface SeriesStatus {
  running?: boolean;
  status?: string;
  stage?: string;
  detail?: string;
  current?: number;
  total?: number;
  stats?: { preflight_thin?: string[];[key: string]: unknown };
  logs?: string[];
  error?: string | null;
  series_id?: number | null;
  series?: Series | null;
  topic?: string;
  request?: Partial<SeriesForm> & { style_id?: string | null };
}

interface EditableChapter {
  title: string;
  scope: string;
  search_queries: string[];
  prerequisites: string[];
  preflight_hits: number | null;
}

interface SeriesForm {
  topic: string;
  lang: "zh" | "en" | "bilingual";
  depth: "beginner" | "intermediate" | "advanced";
  parts: number;
  ref_count: 5 | 10 | 20;
  style: string;
  engines: Array<"bing" | "baidu" | "duckduckgo" | "google" | "brave">;
}

const CHAPTER_STATUS: Record<string, { label: string; color: string }> = {
  pending: { label: "待开始", color: "default" },
  researching: { label: "检索资料", color: "processing" },
  writing: { label: "写作中", color: "processing" },
  done: { label: "已完成", color: "success" },
  failed: { label: "未完成", color: "error" },
  cancelled: { label: "已取消", color: "warning" },
};

const STAGE_LABEL: Record<string, string> = {
  probe: "检索该主题的公开资料",
  map: "梳理知识脉络",
  outline: "生成系列提纲",
  preflight: "预检各章资料量",
  research: "逐章检索资料",
  write: "逐章写作",
  done: "已完成",
};

const SERIES_STATUS: Record<string, { label: string; color: string }> = {
  planned: { label: "待写作", color: "blue" },
  running: { label: "运行中", color: "processing" },
  done: { label: "已完成", color: "success" },
  partial: { label: "部分完成", color: "warning" },
  cancelled: { label: "已取消", color: "warning" },
  failed: { label: "失败", color: "error" },
};

function isActive(data?: SeriesStatus) {
  return data?.running === true;
}

export default function SeriesCreate() {
  const { message, modal } = AntApp.useApp();
  const qc = useQueryClient();
  const { data: license } = useLicense();
  const { openLicenseGuide } = useOutletContext<{ openLicenseGuide: () => void }>();
  const [form] = Form.useForm<SeriesForm>();
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [retrying, setRetrying] = useState<number | null>(null);
  const [planning, setPlanning] = useState(false);
  const [starting, setStarting] = useState(false);
  const [outline, setOutline] = useState<EditableChapter[]>([]);

  const { data: styleData } = useQuery({
    queryKey: ["rewrite-styles"],
    queryFn: () => getJson<{ styles: RewriteStyle[] }>("/api/rewrite-styles"),
  });
  const historyQuery = useQuery({
    queryKey: ["series-list"],
    queryFn: () => getJson<{ series: Series[] }>("/api/series"),
    retry: false,
  });
  const statusQuery = useQuery({
    queryKey: ["series-status"],
    queryFn: () => getJson<SeriesStatus>("/api/series/status"),
    retry: false,
    refetchInterval: (query) => (isActive(query.state.data) ? 3000 : false),
  });
  const status = statusQuery.data;
  const active = isActive(status);
  const state = (status?.status || "idle").toLowerCase();
  const series = status?.series || null;
  const isPro = !!(license?.active || license?.dev);

  const knowledgeMapHtml = useMemo(() => {
    const source = series?.knowledge_map?.trim();
    if (!source) return "";
    return DOMPurify.sanitize(marked.parse(source, { async: false }) as string);
  }, [series?.knowledge_map]);
  const thinChapters = status?.stats?.preflight_thin || [];
  const history = historyQuery.data?.series || [];

  const total = series?.parts || status?.total || 0;
  const finished = series
    ? series.chapters_done + series.chapters_failed
    : status?.current || 0;
  const percent = total ? Math.round((finished / total) * 100) : 0;

  const reviewing = !active && series?.status === "planned";

  useEffect(() => {
    if (!active) setCancelling(false);
  }, [active]);

  // The editor works on a copy: a half-finished edit must survive the polling
  // that keeps the rest of the page current.
  useEffect(() => {
    if (!reviewing || !series) { setOutline([]); return; }
    setOutline(series.chapters.map((chapter) => ({
      title: chapter.title,
      scope: chapter.scope,
      search_queries: chapter.search_queries,
      prerequisites: chapter.prerequisites,
      preflight_hits: chapter.preflight_hits,
    })));
  }, [reviewing, series?.id]);

  // A finished run changes the history, and the history is what the user comes
  // back to days later.
  useEffect(() => {
    if (!active) qc.invalidateQueries({ queryKey: ["series-list"] });
  }, [active, qc]);

  useEffect(() => {
    const saved = status?.request;
    if (!saved) return;
    const fill = (field: keyof SeriesForm, value: unknown) => {
      if (value !== undefined && value !== null && !form.isFieldTouched(field)) {
        form.setFieldValue(field, value);
      }
    };
    fill("topic", saved.topic);
    fill("lang", saved.lang);
    fill("depth", saved.depth);
    fill("parts", saved.parts);
    fill("ref_count", saved.ref_count);
    fill("engines", saved.engines?.length ? saved.engines : undefined);
    if (saved.style_id !== undefined && !form.isFieldTouched("style")) {
      form.setFieldValue("style", saved.style_id || "");
    }
  }, [form, status?.request]);

  async function plan(values: SeriesForm) {
    if (!isPro) { openLicenseGuide(); return; }
    setPlanning(true);
    try {
      await api.post("/api/series/plan", {
        topic: values.topic.trim(),
        lang: values.lang,
        depth: values.depth,
        parts: values.parts,
        ref_count: values.ref_count,
        style_id: values.style || null,
        engines: values.engines,
      });
      message.success("正在生成提纲，稍后可以逐章调整再开跑");
      await qc.invalidateQueries({ queryKey: ["series-status"] });
    } catch (e: any) {
      const detail = e?.response?.data?.error || e?.response?.data?.detail || "启动失败";
      if (e?.response?.status === 403) openLicenseGuide();
      message.error(detail);
    } finally {
      setPlanning(false);
    }
  }

  async function submit(values: SeriesForm) {
    setSubmitting(true);
    try {
      await api.post("/api/series/create", {
        topic: values.topic.trim(),
        lang: values.lang,
        depth: values.depth,
        parts: values.parts,
        ref_count: values.ref_count,
        style_id: values.style || null,
        engines: values.engines,
      });
      message.success("系列创作已开始，可以离开本页，进度会持续保存");
      await qc.invalidateQueries({ queryKey: ["series-status"] });
    } catch (e: any) {
      const detail = e?.response?.data?.error || e?.response?.data?.detail || e?.message || "启动失败";
      if (e?.response?.status === 403) openLicenseGuide();
      message.error(detail);
    } finally {
      setSubmitting(false);
    }
  }

  function confirmAndStart(values: SeriesForm) {
    if (!isPro) { openLicenseGuide(); return; }
    // A series is minutes of work and a real amount of model spend, so the
    // scale is shown before the run rather than discovered during it.
    const calls = 1 + values.parts * 4;
    const fetches = values.parts * values.ref_count * 2;
    modal.confirm({
      title: `确认开始 ${values.parts} 章的系列创作？`,
      icon: null,
      okText: "开始创作",
      cancelText: "再改改",
      content: (
        <Space direction="vertical" size={4} style={{ marginTop: 8 }}>
          <Text>预计大模型调用约 {calls} 次，网页抓取约 {fetches} 个页面。</Text>
          <Text>预计耗时 {values.parts * 3}–{values.parts * 6} 分钟，期间无法同时进行搜索创作。</Text>
          <Text type="secondary">中途可以取消，已写完的章节会保留为草稿。</Text>
        </Space>
      ),
      onOk: () => submit(values),
    });
  }

  function editChapter(index: number, patch: Partial<EditableChapter>) {
    setOutline((rows) => rows.map(
      (row, position) => (position === index ? { ...row, ...patch } : row)));
  }

  function moveChapter(index: number, delta: number) {
    setOutline((rows) => {
      const next = [...rows];
      const target = index + delta;
      if (target < 0 || target >= next.length) return rows;
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  }

  async function saveOutline(seriesId: number, rows: EditableChapter[]) {
    await api.put(`/api/series/${seriesId}/outline`, {
      chapters: rows.map((row) => ({
        title: row.title,
        scope: row.scope,
        search_queries: row.search_queries,
        prerequisites: row.prerequisites,
      })),
    });
  }

  // The outline decides what every chapter can find, so it is worth a minute
  // of the user's attention before it turns into twenty minutes of writing.
  async function startWriting() {
    if (!series) return;
    const invalid = outline.find((row) => !row.title.trim());
    if (invalid) { message.error("章节标题不能为空"); return; }
    setStarting(true);
    try {
      await saveOutline(series.id, outline);
      await api.post(`/api/series/${series.id}/run`, {});
      message.success("已开始逐章写作，可以离开本页");
      await qc.invalidateQueries({ queryKey: ["series-status"] });
    } catch (e: any) {
      if (e?.response?.status === 403) openLicenseGuide();
      message.error(e?.response?.data?.error || e?.response?.data?.detail || "启动失败");
    } finally {
      setStarting(false);
    }
  }

  function confirmWriting() {
    if (!series) return;
    const refs = series.ref_count || 5;
    modal.confirm({
      title: `确认按这份提纲写 ${outline.length} 章？`,
      icon: null,
      okText: "开始写作",
      cancelText: "再改改",
      content: (
        <Space direction="vertical" size={4} style={{ marginTop: 8 }}>
          <Text>预计大模型调用约 {outline.length * 3} 次，网页抓取约 {outline.length * refs * 2} 个页面。</Text>
          <Text>预计耗时 {outline.length * 3}–{outline.length * 6} 分钟，期间无法同时进行搜索创作。</Text>
          <Text type="secondary">中途可以取消，已写完的章节会保留为草稿。</Text>
        </Space>
      ),
      onOk: startWriting,
    });
  }

  // A chapter that came up short is worth another try on its own: rerunning
  // the whole series to rescue one chapter would redo everything that worked.
  async function retryChapter(seriesId: number, chapterId: number) {
    if (!isPro) { openLicenseGuide(); return; }
    setRetrying(chapterId);
    try {
      await api.post(`/api/series/${seriesId}/chapters/${chapterId}/retry`, {});
      message.success("已开始重跑这一章");
      await qc.invalidateQueries({ queryKey: ["series-status"] });
    } catch (e: any) {
      if (e?.response?.status === 403) openLicenseGuide();
      message.error(e?.response?.data?.error || e?.response?.data?.detail || "重跑失败");
    } finally {
      setRetrying(null);
    }
  }

  async function cancel() {
    setCancelling(true);
    try {
      await api.post("/api/series/cancel", {});
      message.info("已请求取消，正在等待当前章节结束…");
      await qc.invalidateQueries({ queryKey: ["series-status"] });
    } catch (e: any) {
      setCancelling(false);
      message.error(e?.response?.data?.error || e?.response?.data?.detail || "取消失败");
    }
  }

  const resultAlert = (() => {
    if (status?.error) return { type: "error" as const, text: status.error };
    if (state === "cancelled") return { type: "warning" as const, text: "系列创作已取消，已完成的章节已保存为草稿" };
    if (state === "partial") {
      return {
        type: "warning" as const,
        text: `系列部分完成：${series?.chapters_done ?? 0} 章已生成，${series?.chapters_failed ?? 0} 章资料不足`,
      };
    }
    if (state === "done") return { type: "success" as const, text: `系列已完成，共 ${series?.chapters_done ?? 0} 章草稿` };
    if (state === "failed") return { type: "error" as const, text: "所有章节都未能找到足够资料，请调整主题后重试" };
    return null;
  })();

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <div>
        <Space align="center">
          <Title level={2} style={{ margin: 0 }}>系列创作</Title>
          <Tag color="gold">Pro</Tag>
        </Space>
        <Paragraph type="secondary" style={{ margin: "8px 0 0" }}>
          输入一个知识主题，先摸清这个领域公开资料的样子，再拆成有递进关系的章节，
          逐章检索资料并写成一组草稿。
        </Paragraph>
      </div>

      {!isPro && license && (
        <Alert type="warning" showIcon message="系列创作为 Pro 功能"
          description={<Button type="link" style={{ padding: 0 }} onClick={openLicenseGuide}>查看 Pro 功能并升级</Button>} />
      )}

      <Card title="系列选项" style={{ borderColor: "#d6e4ff" }}>
        <Form<SeriesForm> form={form} layout="vertical" onFinish={plan}
          initialValues={{
            lang: "zh", depth: "intermediate", parts: 5, ref_count: 5, style: "",
            engines: ["bing", "baidu", "duckduckgo", "google", "brave"],
          }}
          disabled={active || (!isPro && !!license)}>
          <Form.Item name="topic" label="系列主题"
            rules={[
              { required: true, whitespace: true, message: "请输入系列主题" },
              { max: 200, message: "系列主题不能超过 200 个字符" },
            ]}>
            <Input.TextArea rows={3} maxLength={200} showCount
              placeholder="例如：从零理解强化学习" />
          </Form.Item>
          <Space wrap size="large" align="start">
            <Form.Item name="parts" label="章节数" style={{ minWidth: 160 }}>
              <Select options={[3, 4, 5, 6, 7, 8, 9, 10].map((value) => ({
                value, label: `${value} 章`,
              }))} />
            </Form.Item>
            <Form.Item name="depth" label="深度定位" style={{ minWidth: 220 }}>
              <Select options={[
                { value: "beginner", label: "入门（零基础，重解释）" },
                { value: "intermediate", label: "进阶（有基础，重原理）" },
                { value: "advanced", label: "深入（从业者，重细节）" },
              ]} />
            </Form.Item>
            <Form.Item name="lang" label="文章语言" style={{ minWidth: 160 }}>
              <Select options={[
                { value: "zh", label: "中文" },
                { value: "en", label: "English" },
                { value: "bilingual", label: "中英双语" },
              ]} />
            </Form.Item>
            <Form.Item name="ref_count" label="每章参考来源" style={{ minWidth: 180 }}>
              <Select options={[5, 10, 20].map((value) => ({ value, label: `${value} 个来源` }))} />
            </Form.Item>
            <Form.Item name="style" label="写作风格" style={{ minWidth: 260 }}
              extra="留空时跟随「设置 → 内容与风格」。">
              <Select loading={!styleData} options={[
                { value: "", label: "默认（跟随设置）" },
                ...(styleData?.styles || []).map((item) => ({
                  value: item.id,
                  label: item.name + (item.is_builtin === false ? "（自定义）" : ""),
                })),
              ]} />
            </Form.Item>
            <Form.Item name="engines" label="搜索引擎" style={{ minWidth: 300 }}
              extra="系列不限制资料时间，百度中文资料最全。"
              rules={[{ required: true, message: "请至少选择一个搜索引擎" }]}>
              <Select mode="multiple" maxTagCount="responsive" options={[
                { value: "bing", label: "Bing（国内优先）" },
                { value: "baidu", label: "百度（中文优先）" },
                { value: "duckduckgo", label: "DuckDuckGo" },
                { value: "google", label: "Google" },
                { value: "brave", label: "Brave" },
              ]} />
            </Form.Item>
          </Space>
          <Form.Item style={{ marginBottom: 0 }}
            extra="提纲会先做一次资料预检，确认后才进入逐章写作。">
            <Space>
              <Button type="primary" htmlType="submit" icon={<BookOutlined />}
                loading={planning} disabled={active}>
                生成提纲
              </Button>
              <Button loading={submitting} disabled={active}
                onClick={() => form.validateFields().then(confirmAndStart)}>
                直接开跑（不审提纲）
              </Button>
              {active && (
                // Form 的 disabled 会经 context 连带禁用内部按钮，取消按钮必须跳出该 context
                <ConfigProvider componentDisabled={false}>
                  <Button danger icon={<CloseCircleOutlined />}
                    loading={cancelling} onClick={cancel}>取消</Button>
                </ConfigProvider>
              )}
            </Space>
          </Form.Item>
        </Form>
      </Card>

      {reviewing && series && (
        <Card title={`提纲待确认：《${series.title}》`}
          style={{ borderColor: "#adc6ff" }}
          extra={<Text type="secondary">{outline.length} 章</Text>}>
          <Paragraph type="secondary">
            检索词决定每章能找到什么资料，标题和范围决定这一章写什么。
            资料偏少的章节现在改一改，比写到那一章再失败划算。
          </Paragraph>
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            {outline.map((chapter, index) => {
              const thin = chapter.preflight_hits !== null
                && chapter.preflight_hits < 4;
              return (
                <Card key={index} size="small" type="inner"
                  title={
                    <Space>
                      <Text type="secondary">第 {index + 1} 章</Text>
                      {chapter.preflight_hits !== null && (
                        <Tag color={thin ? "warning" : "success"}>
                          预检资料 {chapter.preflight_hits} 条
                        </Tag>
                      )}
                    </Space>
                  }
                  extra={
                    <Space size={0}>
                      <Button type="text" size="small" icon={<ArrowUpOutlined />}
                        disabled={index === 0}
                        onClick={() => moveChapter(index, -1)} />
                      <Button type="text" size="small" icon={<ArrowDownOutlined />}
                        disabled={index === outline.length - 1}
                        onClick={() => moveChapter(index, 1)} />
                      <Button type="text" size="small" danger
                        icon={<DeleteOutlined />} disabled={outline.length <= 3}
                        onClick={() => setOutline(
                          outline.filter((_, position) => position !== index))} />
                    </Space>
                  }>
                  <Space direction="vertical" size={8} style={{ width: "100%" }}>
                    <Input value={chapter.title} maxLength={120}
                      placeholder="章节标题"
                      onChange={(e) => editChapter(index, { title: e.target.value })} />
                    <Input value={chapter.scope} maxLength={200}
                      placeholder="这一章讲什么（可留空）"
                      onChange={(e) => editChapter(index, { scope: e.target.value })} />
                    <Select mode="tags" value={chapter.search_queries}
                      style={{ width: "100%" }} tokenSeparators={[","]}
                      placeholder="检索词，回车添加"
                      onChange={(value) => editChapter(
                        index, { search_queries: value, preflight_hits: null })} />
                  </Space>
                </Card>
              );
            })}
          </Space>
          <Space style={{ marginTop: 16 }} wrap>
            <Button type="primary" icon={<BookOutlined />} loading={starting}
              onClick={confirmWriting}>开始逐章写作</Button>
            <Button disabled={outline.length >= 10}
              icon={<PlusOutlined />}
              onClick={() => setOutline([...outline, {
                title: "", scope: "", search_queries: [],
                prerequisites: [], preflight_hits: null,
              }])}>添加章节</Button>
          </Space>
        </Card>
      )}

      {!reviewing && (series || (status && state !== "idle")) && (
        <Card
          title={
            <Space>
              <span>{series ? `《${series.title}》` : "系列进度"}</span>
              {active && <Tag color="processing">运行中</Tag>}
              {total > 0 && <Text type="secondary">{finished}/{total} 章</Text>}
            </Space>
          }
          style={{ borderColor: active ? "#adc6ff" : undefined }}>
          <Progress percent={Math.max(0, Math.min(100, percent))}
            status={state === "failed" || status?.error ? "exception"
              : state === "done" ? "success" : "active"} />
          {active && status?.stage && (
            <Text type="secondary">
              当前阶段：{STAGE_LABEL[status.stage] || status.stage}
              {status.detail ? ` — ${status.detail}` : ""}
            </Text>
          )}

          {resultAlert && (
            <Alert style={{ marginTop: 16 }} type={resultAlert.type} showIcon
              message={resultAlert.text} />
          )}

          {thinChapters.length > 0 && active && (
            <Alert style={{ marginTop: 16 }} type="warning" showIcon
              message={`预检发现 ${thinChapters.join("、")} 公开资料偏少，可能写不成`}
              description="现在取消并换个说法重来，比等它跑到那一章更省时间。" />
          )}

          {knowledgeMapHtml && (
            <Collapse style={{ marginTop: 16 }} size="small" items={[{
              key: "map",
              label: "知识脉络（用于校准章节划分）",
              children: <div className="ma-knowledge-map"
                dangerouslySetInnerHTML={{ __html: knowledgeMapHtml }} />,
            }]} />
          )}

          {series && series.chapters.length > 0 && (
            <List style={{ marginTop: 16 }} size="small"
              dataSource={series.chapters}
              renderItem={(chapter) => {
                const meta = CHAPTER_STATUS[chapter.status] || CHAPTER_STATUS.pending;
                return (
                  <List.Item
                    actions={chapter.draft_id
                      ? [<Link key="open" to={`/drafts/${chapter.draft_id}/edit`}>
                          <Button type="link" size="small">打开草稿</Button>
                        </Link>]
                      : [<Button key="retry" type="link" size="small"
                          loading={retrying === chapter.id}
                          disabled={active}
                          onClick={() => retryChapter(series.id, chapter.id)}>
                          重跑本章
                        </Button>]}>
                    <List.Item.Meta
                      title={
                        <Space>
                          <Text type="secondary">第 {chapter.order} 章</Text>
                          <Text strong>{chapter.title}</Text>
                          <Tag color={meta.color}>{meta.label}</Tag>
                        </Space>
                      }
                      description={
                        <Text type={chapter.error ? "danger" : "secondary"}>
                          {chapter.error || chapter.scope || "—"}
                        </Text>
                      } />
                  </List.Item>
                );
              }} />
          )}

          {(status?.logs?.length || status?.detail) && (
            <div style={{ marginTop: 20 }}>
              <Text strong>运行日志</Text>
              <pre style={{
                margin: "8px 0 0", padding: 14, maxHeight: 280, overflow: "auto",
                borderRadius: 8, background: "#f0f5ff", border: "1px solid #d6e4ff",
                color: "#1d39c4", whiteSpace: "pre-wrap", fontSize: 12, lineHeight: 1.7,
              }}>{(status?.logs || []).join("\n") || status?.detail}</pre>
            </div>
          )}
        </Card>
      )}

      {history.length > 0 && (
        <Card title={`历史系列（${history.length}）`}>
          <Collapse accordion size="small" items={history.map((item) => ({
            key: String(item.id),
            label: (
              <Space wrap>
                <Text strong>{item.title}</Text>
                <Tag color={SERIES_STATUS[item.status]?.color || "default"}>
                  {SERIES_STATUS[item.status]?.label || item.status}
                </Tag>
                <Text type="secondary">
                  {item.chapters_done}/{item.parts} 章
                  {item.chapters_failed > 0 ? ` · ${item.chapters_failed} 章未完成` : ""}
                </Text>
              </Space>
            ),
            children: (
              <Space direction="vertical" size={4} style={{ width: "100%" }}>
                {item.chapters.map((chapter) => (
                  <div key={chapter.id}>
                    <Text type="secondary">第 {chapter.order} 章　</Text>
                    {chapter.draft_id ? (
                      <Link to={`/drafts/${chapter.draft_id}/edit`}>{chapter.title}</Link>
                    ) : (
                      <>
                        <Text type="secondary">
                          {chapter.title}（{chapter.error || "未生成"}）
                        </Text>
                        {item.status !== "planned" && (
                          <Button type="link" size="small" disabled={active}
                            loading={retrying === chapter.id}
                            onClick={() => retryChapter(item.id, chapter.id)}>
                            重跑本章
                          </Button>
                        )}
                      </>
                    )}
                  </div>
                ))}
              </Space>
            ),
          }))} />
        </Card>
      )}
    </Space>
  );
}
