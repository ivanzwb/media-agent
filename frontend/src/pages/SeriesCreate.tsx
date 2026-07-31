import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App as AntApp, Button, Card, ConfigProvider, Form, Input, List,
  Progress, Select, Space, Tag, Typography,
} from "antd";
import { BookOutlined, CloseCircleOutlined } from "@ant-design/icons";
import { useEffect, useState } from "react";
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
}

interface Series {
  id: number;
  title: string;
  topic: string;
  parts: number;
  status: string;
  error: string | null;
  chapters: Chapter[];
  chapters_done: number;
  chapters_failed: number;
}

interface SeriesStatus {
  running?: boolean;
  status?: string;
  stage?: string;
  detail?: string;
  current?: number;
  total?: number;
  stats?: Record<string, unknown>;
  logs?: string[];
  error?: string | null;
  series_id?: number | null;
  series?: Series | null;
  topic?: string;
  request?: Partial<SeriesForm> & { style_id?: string | null };
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
  outline: "生成系列提纲",
  research: "逐章检索资料",
  write: "逐章写作",
  done: "已完成",
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

  const { data: styleData } = useQuery({
    queryKey: ["rewrite-styles"],
    queryFn: () => getJson<{ styles: RewriteStyle[] }>("/api/rewrite-styles"),
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

  const total = series?.parts || status?.total || 0;
  const finished = series
    ? series.chapters_done + series.chapters_failed
    : status?.current || 0;
  const percent = total ? Math.round((finished / total) * 100) : 0;

  useEffect(() => {
    if (!active) setCancelling(false);
  }, [active]);

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
        <Form<SeriesForm> form={form} layout="vertical" onFinish={confirmAndStart}
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
          <Form.Item style={{ marginBottom: 0 }}>
            <Space>
              <Button type="primary" htmlType="submit" icon={<BookOutlined />}
                loading={submitting} disabled={active}>
                开始系列创作
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

      {(series || (status && state !== "idle")) && (
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
                      : undefined}>
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
    </Space>
  );
}
