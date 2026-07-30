import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App as AntApp, Button, Card, Form, Input, Progress, Select, Space,
  Steps, Tag, Typography,
} from "antd";
import { CloseCircleOutlined, SearchOutlined } from "@ant-design/icons";
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

interface SearchCreateStatus {
  ok?: boolean;
  running?: boolean;
  done?: boolean;
  status?: string;
  stage?: string;
  stage_index?: number;
  total_stages?: number;
  progress?: number;
  detail?: string;
  current?: number;
  total?: number;
  stats?: { draft_id?: number | null; [key: string]: unknown };
  logs?: string[];
  error?: string | null;
  draft_id?: number | null;
  result?: { draft_id?: number | null } | null;
  topic?: string;
  request?: Partial<SearchCreateForm> & { style_id?: string | null };
}

interface SearchCreateForm {
  topic: string;
  lang: "zh" | "en" | "bilingual";
  time_range_days: 7 | 30 | 90 | 0;
  ref_count: 5 | 10 | 20;
  style: string;
  engines: Array<"bing" | "baidu" | "duckduckgo" | "google" | "brave">;
}

const STAGES = [
  { key: "expand", title: "扩展检索词" },
  { key: "search", title: "搜索资料" },
  { key: "scrape", title: "抓取页面" },
  { key: "filter", title: "筛选排序" },
  { key: "synthesize", title: "综合写作" },
  { key: "fact_check", title: "事实校验" },
  { key: "done", title: "保存草稿" },
];

const STAGE_ALIASES: Record<string, number> = {
  expand: 0,
  search: 1,
  scrape: 2,
  filter: 3,
  synthesize: 4,
  fact_check: 5,
  done: 6,
};

function normalizedStatus(data?: SearchCreateStatus) {
  return (data?.status || (data?.running ? "running" : data?.done ? "completed" : "idle")).toLowerCase();
}

function isActive(data?: SearchCreateStatus) {
  return data?.running === true || ["running", "queued", "starting"].includes(normalizedStatus(data));
}

function stageIndex(data?: SearchCreateStatus) {
  if (!data) return 0;
  if (typeof data.stage_index === "number") {
    const oneBased = data.stage_index >= 1 && data.stage_index <= (data.total_stages || STAGES.length);
    return Math.max(0, Math.min(STAGES.length - 1, oneBased ? data.stage_index - 1 : data.stage_index));
  }
  const stage = (data.stage || "").toLowerCase().trim();
  return STAGE_ALIASES[stage] ?? 0;
}

export default function SearchCreate() {
  const { message } = AntApp.useApp();
  const qc = useQueryClient();
  const { data: license } = useLicense();
  const { openLicenseGuide } = useOutletContext<{ openLicenseGuide: () => void }>();
  const [form] = Form.useForm<SearchCreateForm>();
  const [submitting, setSubmitting] = useState(false);

  const { data: styleData } = useQuery({
    queryKey: ["rewrite-styles"],
    queryFn: () => getJson<{ styles: RewriteStyle[] }>("/api/rewrite-styles"),
  });
  const statusQuery = useQuery({
    queryKey: ["search-create-status"],
    queryFn: () => getJson<SearchCreateStatus>("/api/search-create/status"),
    retry: false,
    refetchInterval: (query) => isActive(query.state.data) ? 2000 : false,
  });
  const status = statusQuery.data;
  const active = isActive(status);
  const state = normalizedStatus(status);
  const draftId = status?.draft_id || status?.result?.draft_id || status?.stats?.draft_id;
  const isPro = !!(license?.active || license?.dev);
  const currentStage = stageIndex(status);
  const progress = useMemo(() => {
    if (typeof status?.progress === "number") {
      return status.progress <= 1 ? Math.round(status.progress * 100) : Math.round(status.progress);
    }
    if (state === "completed" || state === "done" || draftId) return 100;
    const stageFraction = status?.total
      ? Math.max(0, Math.min(1, (status.current || 0) / status.total))
      : active ? 0.35 : 0;
    return Math.round(((currentStage + stageFraction) / STAGES.length) * 100);
  }, [active, currentStage, draftId, state, status?.current, status?.progress, status?.total]);

  useEffect(() => {
    const saved = status?.request;
    if (!saved) {
      if (status?.topic && !form.isFieldTouched("topic")) form.setFieldValue("topic", status.topic);
      return;
    }
    if (saved.topic && !form.isFieldTouched("topic")) form.setFieldValue("topic", saved.topic);
    if (saved.lang && !form.isFieldTouched("lang")) form.setFieldValue("lang", saved.lang);
    if (saved.time_range_days !== undefined && !form.isFieldTouched("time_range_days")) {
      form.setFieldValue("time_range_days", saved.time_range_days);
    }
    if (saved.ref_count && !form.isFieldTouched("ref_count")) form.setFieldValue("ref_count", saved.ref_count);
    if (saved.style_id !== undefined && !form.isFieldTouched("style")) {
      form.setFieldValue("style", saved.style_id || "");
    }
    if (saved.engines?.length && !form.isFieldTouched("engines")) {
      form.setFieldValue("engines", saved.engines);
    }
  }, [form, status?.request, status?.topic]);

  async function start(values: SearchCreateForm) {
    if (!isPro) { openLicenseGuide(); return; }
    setSubmitting(true);
    try {
      await api.post("/api/search-create", {
        topic: values.topic.trim(),
        lang: values.lang,
        time_range_days: values.time_range_days,
        ref_count: values.ref_count,
        style_id: values.style || null,
        engines: values.engines,
      });
      message.success("搜索创作已开始");
      await qc.invalidateQueries({ queryKey: ["search-create-status"] });
    } catch (e: any) {
      const detail = e?.response?.data?.error || e?.response?.data?.detail || e?.message || "启动失败";
      if (e?.response?.status === 403) openLicenseGuide();
      message.error(detail);
    } finally {
      setSubmitting(false);
    }
  }

  async function cancel() {
    try {
      await api.post("/api/search-create/cancel", {});
      message.info("已请求取消搜索创作");
      await qc.invalidateQueries({ queryKey: ["search-create-status"] });
    } catch (e: any) {
      message.error(e?.response?.data?.error || e?.response?.data?.detail || "取消失败");
    }
  }

  const resultType = state === "error" || status?.error
    ? "error"
    : state === "cancelled" || state === "canceled"
      ? "warning"
      : draftId
        ? "success"
        : "info";

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <div>
        <Space align="center">
          <Title level={2} style={{ margin: 0 }}>搜索创作</Title>
          <Tag color="gold">Pro</Tag>
        </Space>
        <Paragraph type="secondary" style={{ margin: "8px 0 0" }}>
          围绕一个主题搜索多方资料，核对来源并生成带引用的可编辑草稿。
        </Paragraph>
      </div>

      {!isPro && license && (
        <Alert type="warning" showIcon message="搜索创作为 Pro 功能"
          description={<Button type="link" style={{ padding: 0 }} onClick={openLicenseGuide}>查看 Pro 功能并升级</Button>} />
      )}

      <Card title="创作选项" style={{ borderColor: "#d9f7be" }}>
        <Form<SearchCreateForm> form={form} layout="vertical" onFinish={start}
          initialValues={{
            lang: "zh", time_range_days: 30, ref_count: 10, style: "",
            engines: ["bing", "baidu", "duckduckgo", "google", "brave"],
          }}
          disabled={active || (!isPro && !!license)}>
          <Form.Item name="topic" label="创作主题"
            rules={[
              { required: true, whitespace: true, message: "请输入创作主题" },
              { max: 200, message: "搜索创作主题不能超过 200 个字符" },
            ]}>
            <Input.TextArea rows={3} maxLength={200} showCount
              placeholder="例如：生成式 AI 如何改变中小企业的内容营销" />
          </Form.Item>
          <Space wrap size="large" align="start">
            <Form.Item name="lang" label="文章语言" style={{ minWidth: 180 }}>
              <Select options={[
                { value: "zh", label: "中文" },
                { value: "en", label: "English" },
                { value: "bilingual", label: "中英双语" },
              ]} />
            </Form.Item>
            <Form.Item name="time_range_days" label="资料时间范围" style={{ minWidth: 180 }}>
              <Select options={[
                { value: 7, label: "最近 7 天" },
                { value: 30, label: "最近 30 天" },
                { value: 90, label: "最近 90 天" },
                { value: 0, label: "不限时间" },
              ]} />
            </Form.Item>
            <Form.Item name="ref_count" label="参考来源数量" style={{ minWidth: 180 }}>
              <Select options={[5, 10, 20].map((value) => ({ value, label: `${value} 个来源` }))} />
            </Form.Item>
            <Form.Item name="style" label="写作风格" style={{ minWidth: 260 }}
              extra="留空时跟随「设置 → 内容与风格」；标签、推广和敏感词也使用该页设置。">
              <Select loading={!styleData} options={[
                { value: "", label: "默认（跟随设置）" },
                ...(styleData?.styles || []).map((item) => ({
                  value: item.id,
                  label: item.name + (item.is_builtin === false ? "（自定义）" : ""),
                })),
              ]} />
            </Form.Item>
            <Form.Item name="engines" label="搜索引擎" style={{ minWidth: 300 }}
              extra="百度中文资料最全，但不支持按时间范围筛选。"
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
              <Button type="primary" htmlType="submit" icon={<SearchOutlined />}
                loading={submitting} disabled={active}>
                开始搜索创作
              </Button>
              {active && (
                <Button danger icon={<CloseCircleOutlined />} onClick={cancel}>取消</Button>
              )}
            </Space>
          </Form.Item>
        </Form>
      </Card>

      {(status && state !== "idle") && (
        <Card title={<Space><span>创作进度</span>{active && <Tag color="processing">运行中</Tag>}</Space>}
          style={{ borderColor: active ? "#b7eb8f" : undefined }}>
          <Progress percent={Math.max(0, Math.min(100, progress))}
            status={resultType === "error" ? "exception" : draftId ? "success" : "active"} />
          <Steps current={currentStage} size="small" responsive
            status={resultType === "error" ? "error" : draftId ? "finish" : "process"}
            items={STAGES.map((item) => ({ title: item.title }))} />

          {(status.error || draftId || state.includes("cancel")) && (
            <Alert style={{ marginTop: 20 }} type={resultType} showIcon
              message={status.error || (draftId ? "草稿已生成" : "搜索创作已取消")}
              action={draftId
                ? <Link to={`/drafts/${draftId}/edit`}><Button type="primary" size="small">打开草稿</Button></Link>
                : undefined} />
          )}

          <div style={{ marginTop: 20 }}>
            <Text strong>运行日志</Text>
            <pre style={{
              margin: "8px 0 0", padding: 14, maxHeight: 280, overflow: "auto",
              borderRadius: 8, background: "#f6ffed", border: "1px solid #d9f7be",
              color: "#3f6600", whiteSpace: "pre-wrap", fontSize: 12, lineHeight: 1.7,
            }}>{(status.logs || []).join("\n") || status.detail || "等待进度更新…"}</pre>
          </div>
        </Card>
      )}
    </Space>
  );
}
