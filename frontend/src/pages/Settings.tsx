import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App as AntApp, Button, Card, Divider, Form, Input, InputNumber, Progress, Select,
  Space, Switch, Tag, Typography, List, Upload, Popconfirm, Tabs, Modal,
} from "antd";
import { UploadOutlined, AudioOutlined, StopOutlined, CheckOutlined, CloseOutlined, ThunderboltOutlined, DownloadOutlined, ReloadOutlined, PauseCircleOutlined, PlayCircleOutlined } from "@ant-design/icons";
import { useState, useRef, useEffect } from "react";
import { api, getJson, postForm } from "../api/client";

const { Title, Text, Paragraph } = Typography;

interface MaskField { set: boolean; masked: string; }
interface ManagedCapability {
  ready: boolean;
  available?: boolean;
  reason: string;
  supported?: boolean;
  asset_available?: boolean;
  installable?: boolean;
  platform?: string;
  accelerator?: string;
  warning?: string;
  performance_warning?: string;
}
interface Capabilities { cosyvoice: ManagedCapability; sadtalker: ManagedCapability; }
interface StylePub { id: string; name: string; description: string; prompt: string; instruction: string; is_builtin: boolean; is_default: boolean; }
interface Voice { id: string; name?: string; }
interface SettingsData {
  [k: string]: any;
  llm_api_key: MaskField; image_api_key: MaskField; tts_api_key: MaskField; wechat_appsecret: MaskField;
  rewrite_styles: StylePub[]; rewrite_style: string;
  voices: Voice[];
  license: any; license_labels: Record<string, string>;
}

export default function Settings() {
  const qc = useQueryClient();
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const { data } = useQuery({ queryKey: ["settings"], queryFn: () => getJson<SettingsData>("/api/settings") });
  const { data: caps } = useQuery({ queryKey: ["capabilities"], queryFn: () => getJson<Capabilities>("/api/capabilities"), staleTime: 60_000 });

  if (!data) return null;

  const refetch = () => qc.invalidateQueries({ queryKey: ["settings"] });

  async function onSave() {
    setSaving(true);
    try {
      const v = await form.getFieldsValue();
      const keyFields = ["llm_api_key", "image_api_key", "tts_api_key", "wechat_appsecret"];
      const payload: Record<string, any> = {};
      const boolFields = [
        "schedule_enabled", "download_images", "download_videos",
        "relevance_filter", "seo_tags_enabled",
      ];
      for (const [k, val] of Object.entries(v)) {
        if (keyFields.includes(k)) { if (val) payload[k] = val; continue; }
        if (boolFields.includes(k)) { payload[k] = val ? "1" : "0"; continue; }
        payload[k] = val ?? "";
      }
      await postForm("/settings", payload);
      message.success("已保存");
      form.setFieldsValue({ llm_api_key: "", image_api_key: "", tts_api_key: "", wechat_appsecret: "" });
      refetch();
    } finally { setSaving(false); }
  }

  const lic = data.license;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Title level={2} style={{ margin: 0 }}>设置</Title>
      <Paragraph type="secondary">配置可在此覆盖（存数据库，优先级高于环境变量）。数据目录：<code>{data.data_dir}</code></Paragraph>

      <Form form={form} layout="vertical" initialValues={{
        llm_provider: data.llm_provider, llm_model: data.llm_model, llm_api_base: data.llm_api_base,
        image_provider: data.image_provider, image_api_base: data.image_api_base, image_model: data.image_model,
        cli_tool: data.cli_tool,
        rewrite_priority: data.rewrite_priority,
        tts_provider: data.tts_provider || "kitten", tts_api_base: data.tts_api_base, tts_model: data.tts_model, tts_voice: data.tts_voice,
        tts_rate: data.tts_rate, tts_pitch: data.tts_pitch, tts_instruct: data.tts_instruct,
        max_age_days: data.max_age_days, max_per_source: data.max_per_source, max_drafts: data.max_drafts, download_workers: data.download_workers,
        video_fit: data.video_fit, video_brand_name: data.video_brand_name,
        avatar_enabled: data.avatar_enabled, avatar_image: data.avatar_image,
        avatar_position: data.avatar_position, avatar_provider: data.avatar_provider,

        fetch_proxy: data.fetch_proxy,
        github_mirror: data.github_mirror,
        huggingface_mirror: data.huggingface_mirror,
        sensitive_level: data.sensitive_level, sensitive_words: data.sensitive_words,
        promotion_footer: data.promotion_footer,
        seo_tags_enabled: data.seo_tags_enabled,
        wechat_appid: data.wechat_appid, wechat_author: data.wechat_author,
        rewrite_style: data.rewrite_style,
        download_images: data.download_images, download_videos: data.download_videos,
        relevance_filter: data.relevance_filter,
        schedule_cron: data.schedule_cron, schedule_enabled: data.schedule_enabled,
        llm_api_key: "", image_api_key: "", tts_api_key: "", wechat_appsecret: "",
      }}>
        <Tabs defaultActiveKey="model" items={[
          {
            key: "license", label: "授权", forceRender: true, children: (
              <LicenseCard lic={lic} labels={data.license_labels} onChange={refetch} />
            ),
          },
          {
            key: "model", label: "模型", forceRender: true, children: (
              <>
                <Card title="LLM" size="small" style={{ marginBottom: 16 }}>
                  <Form.Item name="llm_provider" label="Provider"><Select options={["openai", "mock"].map((v) => ({ value: v }))} /></Form.Item>
                  <Form.Item name="llm_model" label="模型"><Input /></Form.Item>
                  <Form.Item name="llm_api_base" label="API Base"><Input /></Form.Item>
                  <Form.Item name="llm_api_key" label={<>API Key {data.llm_api_key.set && <Tag color="green">已设置 {data.llm_api_key.masked}</Tag>}</>}>
                    <Input.Password placeholder={data.llm_api_key.set ? "留空则保持不变" : "输入 API Key"} />
                  </Form.Item>
                </Card>
                <Card title="图片" size="small" style={{ marginBottom: 16 }}>
                  <Form.Item name="image_provider" label="Provider"><Select options={["openai", "mock"].map((v) => ({ value: v }))} /></Form.Item>
                  <Form.Item name="image_model" label="模型"><Input /></Form.Item>
                  <Form.Item name="image_api_base" label="API Base"><Input /></Form.Item>
                  <Form.Item name="image_api_key" label={<>API Key {data.image_api_key.set && <Tag color="green">已设置 {data.image_api_key.masked}</Tag>}</>}>
                    <Input.Password placeholder={data.image_api_key.set ? "留空则保持不变" : "输入 API Key"} />
                  </Form.Item>
                </Card>
                <Card title="转写 Agent（CLI）" size="small" style={{ marginBottom: 16 }}>
                  <Space align="end">
                    <Form.Item name="cli_tool" label="CLI Agent" style={{ marginBottom: 0 }}>
                      <Select options={["auto", "opencode", "claude", "codex", "copilot", "cursor-agent", "none"].map((v) => ({ value: v }))} style={{ width: 200 }} />
                    </Form.Item>
                    <CliTestButton form={form} />
                  </Space>
                </Card>
                <Card title="转写优先级" size="small">
                  <Form.Item name="rewrite_priority" label="优先级"
                    tooltip="转写文章时，大模型（LLM）与 CLI Agent 谁优先。选中的不可用时自动回退到另一个。">
                    <Select options={[
                      { value: "agent", label: "Agent 优先" },
                      { value: "llm", label: "大模型优先" },
                    ]} style={{ width: 200 }} />
                  </Form.Item>
                </Card>
              </>
            ),
          },
          {
            key: "voice", label: "语音与视频", forceRender: true, children: (
              <Card title="TTS 与声音库" size="small">
                <Form.Item name="tts_provider" label="Provider">
                  <Select onChange={() => form.setFieldValue("tts_voice", undefined)} options={[
                    { value: "kitten", label: "Kitten（本地 edge-tts，默认）" },
                    { value: "cosyvoice", label: `CosyVoice（本地 ${caps?.cosyvoice.accelerator === "CPU" ? "CPU" : "GPU"} 声音复刻）` },
                    { value: "openai_compatible", label: "OpenAI 兼容（云端 API）" },
                    { value: "mock", label: "Mock（测试）" },
                  ]} />
                </Form.Item>
                <Form.Item noStyle shouldUpdate={(prev, cur) => prev.tts_provider !== cur.tts_provider}>
                  {({ getFieldValue }) => {
                    const provider = getFieldValue("tts_provider") || "kitten";
                    const adjustable = (
                      <Space wrap>
                        <Form.Item name="tts_rate" label="语速" tooltip="语音语速，如 +10% / -10%">
                          <Input placeholder="+0%" style={{ width: 140 }} />
                        </Form.Item>
                        <Form.Item name="tts_pitch" label="音调" tooltip="语音音调，调高更活泼，如 +15Hz / -10Hz">
                          <Input placeholder="+0Hz" style={{ width: 140 }} />
                        </Form.Item>
                      </Space>
                    );

                    if (provider === "kitten") return (
                      <>
                        <Form.Item name="tts_voice" label="音色">
                          <Select allowClear options={[
                            { value: "assistant", label: "助手（默认）" },
                            { value: "female", label: "女声" },
                            { value: "female_warm", label: "温柔女声" },
                            { value: "male", label: "男声" },
                            { value: "male_deep", label: "低沉男声" },
                            { value: "child", label: "儿童声" },
                          ]} />
                        </Form.Item>
                        {adjustable}
                      </>
                    );

                    if (provider === "cosyvoice") return (
                      <>
                        <CosyVoiceSetup onSuccess={refetch} />
                        <Form.Item name="tts_voice" label="复刻声音">
                          <Select allowClear showSearch placeholder="选择声音库中的样本"
                            options={(data.voices || []).map((v) => ({ value: v.id, label: v.name || v.id }))} />
                        </Form.Item>
                        {adjustable}
                        <Form.Item name="tts_instruct" label="语气 / 情感"
                          tooltip="用自然语言描述语气/情感，如「用亲切自然的语气」「热情激昂地讲解」">
                          <Input placeholder="例如：用亲切自然的语气讲解" />
                        </Form.Item>
                        <Divider />
                        <VoiceManager voices={data.voices || []} onChange={refetch} />
                      </>
                    );

                    if (provider === "openai_compatible") return (
                      <>
                        <Form.Item name="tts_api_base" label="API Base"><Input /></Form.Item>
                        <Form.Item name="tts_model" label="模型"><Input /></Form.Item>
                        <Form.Item name="tts_voice" label="音色">
                          <Input placeholder="例如 alloy、nova，或服务商支持的音色名称" />
                        </Form.Item>
                        {adjustable}
                        <Form.Item name="tts_api_key" label={<>API Key {data.tts_api_key.set && <Tag color="green">已设置 {data.tts_api_key.masked}</Tag>}</>}>
                          <Input.Password placeholder={data.tts_api_key.set ? "留空则保持不变" : "输入 API Key"} />
                        </Form.Item>
                      </>
                    );

                    return <Text type="secondary">Mock Provider 用于测试，不需要额外配置。</Text>;
                  }}
                </Form.Item>
                <Divider />
                <Card title="视频设置" size="small" style={{ marginTop: 16 }}>
                  <Form.Item name="video_fit" label="视频适配">
                    <Select options={["fit", "crop", "blur"].map((v) => ({ value: v }))} />
                  </Form.Item>
                  <Form.Item name="video_brand_name" label="视频品牌名"><Input /></Form.Item>
                </Card>
              </Card>
            ),
          },
          {
            key: "avatar", label: "数字人主播", forceRender: true, children: (
              <Card title="数字人主播" size="small">
                <Form.Item name="avatar_enabled" label="启用数字人主播" valuePropName="checked"
                  tooltip="在讲解视频中叠加一个数字人主播（口播）。需要主播头像；口型同步需本地 SadTalker + GPU，未配置时用静态头像。">
                  <Switch />
                </Form.Item>
                <Form.Item label="主播头像">
                  <Space align="start">
                    <Upload showUploadList={false} accept="image/*" customRequest={async ({ file, onSuccess, onError }) => {
                      try {
                        const fd = new FormData(); fd.append("file", file as File);
                        const r = await api.post<{ ok: boolean; avatar_image: string }>("/api/avatar/upload", fd);
                        form.setFieldValue("avatar_image", r.data.avatar_image);
                        message.success("头像已上传"); onSuccess?.({}); refetch();
                      } catch { message.error("上传失败"); onError?.(new Error("upload")); }
                    }}>
                      <Button>上传头像</Button>
                    </Upload>
                    <Form.Item name="avatar_image" noStyle><Input style={{ width: 220 }} placeholder="头像文件名（自动填充）" /></Form.Item>
                    {data.avatar_image && <img src={`/avatar/${data.avatar_image}`} alt="presenter" style={{ height: 64, borderRadius: 8, border: "1px solid #eee" }} />}
                  </Space>
                </Form.Item>
                <Form.Item name="avatar_position" label="默认位置">
                  <Select style={{ width: 200 }} options={[
                    { value: "pip", label: "画中画（角落）" },
                    { value: "full", label: "全屏主播" },
                  ]} />
                </Form.Item>
                <Form.Item name="avatar_provider" label="生成方式"
                  extra={caps && !caps.sadtalker.ready
                    ? `SadTalker 口型同步在本机不可用：${caps.sadtalker.reason}（可先用静态头像）` : undefined}>
                  <Select style={{ width: 280 }} options={[
                    { value: "sadtalker",
                      label: caps && !caps.sadtalker.ready
                        ? `SadTalker 口型同步（本地 ${caps.sadtalker.accelerator || "Runtime"}）— 尚未就绪`
                        : `SadTalker 口型同步（本地 ${caps?.sadtalker.accelerator || "Runtime"}）`,
                      disabled: !!caps && !caps.sadtalker.ready },
                    { value: "still", label: "静态头像（无口型）" },
                  ]} />
                </Form.Item>
                <SadTalkerSetup data={data} onSuccess={refetch} />
              </Card>
            ),
          },
          {
            key: "content", label: "内容与风格", forceRender: true, children: (
              <>
                <Card title="转写风格" size="small" style={{ marginBottom: 16 }}>
                  <Form.Item name="rewrite_style" label="全局默认风格">
                    <Select options={(data.rewrite_styles || []).map((s) => ({ value: s.id, label: s.name + (s.is_builtin ? "" : "（自定义）") }))} />
                  </Form.Item>
                  <RewriteStyleManager styles={data.rewrite_styles || []} onChange={refetch} />
                </Card>
                <Card title="敏感词" size="small" style={{ marginBottom: 16 }}>
                  <Form.Item name="sensitive_level" label="过滤级别">
                    <Select options={["off", "basic", "standard", "strict"].map((v) => ({ value: v }))} />
                  </Form.Item>
                  <Form.Item name="sensitive_words" label="自定义敏感词（逗号/换行分隔）"><Input.TextArea rows={3} /></Form.Item>
                </Card>
                <Card title="SEO 与推广" size="small">
                  <Form.Item name="seo_tags_enabled" label="生成文章 SEO 标签"
                    valuePropName="checked"
                    tooltip="转写时根据文章内容生成 5-8 个搜索关键词，放在推广文案之前（MEDIA_AGENT_SEO_TAGS_ENABLED）">
                    <Switch checkedChildren="启用" unCheckedChildren="关闭" />
                  </Form.Item>
                  <Form.Item name="promotion_footer" label="推广文案参考">
                    <Input.TextArea rows={3} />
                  </Form.Item>
                </Card>
              </>
            ),
          },
          {
            key: "collect", label: "采集与定时", forceRender: true, children: (
              <>
                <Card title="采集" size="small" style={{ marginBottom: 16 }}>
                  <Form.Item name="max_age_days" label="最大天数（留空不限）"><Input /></Form.Item>
                  <Form.Item name="max_per_source" label="每来源最多抓取（留空不限）"><Input /></Form.Item>
                  <Form.Item name="max_drafts" label="每次运行改写篇数（留空默认 10）"
                    tooltip="每次「立即运行」最多改写/转写的文章篇数，留空则默认 10（MEDIA_AGENT_MAX_DRAFTS）">
                    <Input />
                  </Form.Item>
                  <Form.Item name="relevance_filter" label="AI 相关性过滤（只保留新闻/行业动态/研究文章）" valuePropName="checked"
                    tooltip="抓取后用大模型判断每条内容是否为真正的新闻/行业动态/研究文章，丢弃公司主页、关于我们、产品/营销落地页、招聘、导航/列表页等非文章内容（MEDIA_AGENT_RELEVANCE_FILTER）">
                    <Switch />
                  </Form.Item>
                  <Form.Item name="download_workers" label={`并发下载数（默认 ${data.default_workers}）`}><Input /></Form.Item>
                  <Form.Item name="download_images" label="本地化图片" valuePropName="checked"
                    tooltip="归档时把文章图片下载到本地，避免防盗链失效；关闭则保留远程 URL（MEDIA_AGENT_DOWNLOAD_IMAGES）">
                    <Switch />
                  </Form.Item>
                  <Form.Item name="download_videos" label="本地化视频" valuePropName="checked"
                    tooltip="归档时把文章视频下载到本地（需 yt-dlp）；关闭则保留远程 URL（MEDIA_AGENT_DOWNLOAD_VIDEOS）">
                    <Switch />
                  </Form.Item>
                </Card>
                <Card title="定时" size="small">
                  <Form.Item name="schedule_cron" label="Cron 表达式"><Input placeholder="如 0 8 * * *" /></Form.Item>
                  <Form.Item name="schedule_enabled" label="启用定时" valuePropName="checked"><Switch /></Form.Item>
                </Card>
              </>
            ),
          },
          {
            key: "wechat", label: "公众号", forceRender: true, children: (
              <Card title="公众号" size="small">
                <Form.Item name="wechat_appid" label="AppID"><Input /></Form.Item>
                <Form.Item name="wechat_appsecret" label={<>AppSecret {data.wechat_appsecret.set && <Tag color="green">已设置 {data.wechat_appsecret.masked}</Tag>}</>}>
                  <Input.Password placeholder={data.wechat_appsecret.set ? "留空则保持不变" : "输入 AppSecret"} />
                </Form.Item>
                <Form.Item name="wechat_author" label="默认作者"><Input /></Form.Item>
              </Card>
            ),
          },
          {
            key: "io", label: "其它", forceRender: true, children: (
              <>
                <Card title="网络代理" size="small" style={{ marginBottom: 16 }}>
                  <Form.Item name="fetch_proxy" label="HTTP/HTTPS 代理"
                    tooltip="RSS/网页抓取与媒体下载走此代理，可用于绕过 Cloudflare/WAF 或访问受限站点；留空则不使用（MEDIA_AGENT_FETCH_PROXY）">
                    <Input placeholder="如 http://127.0.0.1:7890" />
                  </Form.Item>
                </Card>
                <Card title="GitHub 镜像" size="small" style={{ marginBottom: 16 }}>
                  <Form.Item name="github_mirror" label="GitHub 镜像前缀"
                    tooltip="下载 GitHub Release 资源时使用此镜像前缀加速；留空则直连 GitHub（MEDIA_AGENT_GITHUB_MIRROR）">
                    <Input placeholder="如 https://ghproxy.net/" />
                  </Form.Item>
                </Card>
                <Card title="HuggingFace 镜像" size="small" style={{ marginBottom: 16 }}>
                  <Form.Item name="huggingface_mirror" label="HuggingFace 镜像地址"
                    tooltip="CosyVoice 模型下载使用此镜像加速；留空则直连 HuggingFace（MEDIA_AGENT_HUGGINGFACE_MIRROR / HF_ENDPOINT）">
                    <Input placeholder="如 https://hf-mirror.com" />
                  </Form.Item>
                </Card>
                <Card title="导出 / 导入配置" size="small">
                  <Space>
                    <Button href="/export">导出配置</Button>
                    <Upload accept="application/json,.json" showUploadList={false}
                      customRequest={async ({ file, onSuccess, onError }) => {
                        const fd = new FormData(); fd.append("file", file as File);
                        try { await api.post("/import", fd); message.success("导入成功"); refetch(); onSuccess?.({}); }
                        catch (e) { message.error("导入失败"); onError?.(e as any); }
                      }}>
                      <Button icon={<UploadOutlined />}>导入配置</Button>
                    </Upload>
                  </Space>
                </Card>
              </>
            ),
          },
        ]} />

        <div style={{ position: "sticky", bottom: 0, background: "#f5f6f8", padding: "12px 0", borderTop: "1px solid #eee" }}>
          <Button type="primary" loading={saving} onClick={onSave} size="large">保存设置</Button>
          <Text type="secondary" style={{ marginLeft: 12 }}>保存对所有标签页的更改生效</Text>
        </div>
      </Form>
    </Space>
  );
}

function LicenseCard({ lic, labels, onChange }: { lic: any; labels: Record<string, string>; onChange: () => void; }) {
  const { message } = AntApp.useApp();
  const qc = useQueryClient();
  const [key, setKey] = useState("");
  const refetchAll = () => { qc.invalidateQueries({ queryKey: ["settings"] }); qc.invalidateQueries({ queryKey: ["license"] }); onChange(); };
  return (
    <Card title="License" size="small">
      {lic.dev ? <Tag color="blue">开发模式（DEV）</Tag>
        : lic.active ? <Tag color="green">✓ 已激活 {String(lic.edition || "").toUpperCase()} 版{lic.expires_at ? ` · 有效期至 ${lic.expires_at.slice(0, 10)}` : " · 永久授权"}</Tag>
        : <Tag>未激活 · 免费版（{lic.reason}）</Tag>}
      {lic.machine_code && <Paragraph style={{ marginTop: 8 }}>本机机器码：<code>{lic.machine_code}</code></Paragraph>}
      {lic.active && !lic.dev ? (
        <>
          <Paragraph type="secondary">已解锁：{(lic.features || []).map((f: string) => labels[f] || f).join("、")}</Paragraph>
          <Popconfirm title="确定要取消激活吗？" onConfirm={async () => { await api.post("/api/license/deactivate"); message.success("已取消激活"); refetchAll(); }}>
            <Button danger>取消激活</Button>
          </Popconfirm>
        </>
      ) : !lic.dev && (
        <Space.Compact style={{ width: "100%", maxWidth: 480 }}>
          <Input placeholder="粘贴购买后获得的激活码" value={key} onChange={(e) => setKey(e.target.value)} />
          <Button type="primary" onClick={async () => {
            const r = await postForm<{ ok: boolean; message: string }>("/api/license/activate", { key }).catch((e) => e.response?.data);
            if (r?.ok) { message.success(r.message || "激活成功"); setKey(""); refetchAll(); }
            else message.error(r?.message || r?.error || "激活失败");
          }}>激活</Button>
        </Space.Compact>
      )}
    </Card>
  );
}

function RewriteStyleManager({ styles, onChange }: { styles: StylePub[]; onChange: () => void; }) {
  const { message } = AntApp.useApp();
  const customs = styles.filter((s) => !s.is_builtin);
  const builtins = styles.filter((s) => s.is_builtin);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<StylePub | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

  // Pre-fill the create form from a built-in style so a new custom style can
  // start from (and then tweak) an existing one.
  function applyBase(id: string | undefined) {
    const b = builtins.find((x) => x.id === id);
    if (!b) return;
    form.setFieldsValue({
      name: form.getFieldValue("name") || `${b.name} 副本`,
      description: b.description,
      instruction: b.instruction,
      prompt: b.prompt,
    });
  }

  async function openCreate() {
    setEditing(null); form.resetFields(); setModalOpen(true);
  }
  async function openEdit(s: StylePub) {
    setEditing(s); form.setFieldsValue(s); setModalOpen(true);
  }
  async function del(id: string) {
    await api.delete(`/api/rewrite-styles/${encodeURIComponent(id)}`);
    message.success("已删除"); onChange();
  }
  async function onSubmit() {
    const v = await form.validateFields();
    setSaving(true);
    try {
      const url = editing
        ? `/api/rewrite-styles/${encodeURIComponent(editing.id)}`
        : "/api/rewrite-styles";
      const method = editing ? api.put : api.post;
      const fd = new FormData();
      fd.append("name", v.name);
      fd.append("description", v.description || "");
      fd.append("prompt", v.prompt || "");
      fd.append("instruction", v.instruction);
      await method(url, fd);
      message.success(editing ? "已更新" : "已创建");
      setModalOpen(false); onChange();
    } catch { message.error("操作失败"); }
    finally { setSaving(false); }
  }

  return (
    <div>
      <Space style={{ marginBottom: 8 }}>
        <Text strong>自定义风格</Text>
        <Button size="small" type="primary" onClick={openCreate}>新建风格</Button>
      </Space>
      <List size="small" dataSource={customs} locale={{ emptyText: "暂无自定义风格" }}
        renderItem={(s) => (
          <List.Item actions={[
            <a key="e" onClick={() => openEdit(s)}>编辑</a>,
            <Popconfirm key="d" title="删除该风格？" onConfirm={() => del(s.id)}><a>删除</a></Popconfirm>,
          ]}>
            <List.Item.Meta title={s.name} description={s.description || s.instruction} />
          </List.Item>
        )} />
      <Modal title={editing ? "编辑风格" : "新建风格"} open={modalOpen}
        onCancel={() => setModalOpen(false)} onOk={onSubmit} confirmLoading={saving}>
        <Form form={form} layout="vertical">
          {!editing && (
            <Form.Item name="based_on" label="基于内置风格（可选）"
              tooltip="选择一个内置转写风格作为起点，会把它的改写指令与系统提示词填入下方，你可以再修改">
              <Select allowClear placeholder="从空白开始，或选择一个内置风格作为模板"
                onChange={(val) => applyBase(val as string | undefined)}
                options={builtins.map((b) => ({ value: b.id, label: b.name }))} />
            </Form.Item>
          )}
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请输入风格名称" }]}>
            <Input />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item name="instruction" label="改写指令" rules={[{ required: true, message: "请输入改写指令" }]}>
            <Input.TextArea rows={4} placeholder="例：用口语化、幽默的风格改写，多使用比喻和网络流行语" />
          </Form.Item>
          <Form.Item name="prompt" label="系统提示词（可选）" tooltip="留空则使用默认改写提示词">
            <Input.TextArea rows={6} placeholder="覆盖默认改写 prompt，一般不需要填写" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function VoiceManager({ voices, onChange }: { voices: Voice[]; onChange: () => void; }) {
  const { message } = AntApp.useApp();
  async function del(id: string) { await postForm(`/voices/${id}/delete`); message.success("已删除"); onChange(); }
  return (
    <div>
      <Space style={{ marginBottom: 8 }}>
        <Text strong>声音库</Text>
        <Upload accept="audio/*" showUploadList={false} customRequest={async ({ file, onSuccess, onError }) => {
          const f = file as File;
          const fd = new FormData(); fd.append("file", f);
          // Default the voice name to the file's stem so uploads aren't all
          // saved as "未命名声音" (which then collide on the 2nd upload).
          fd.append("name", (f.name || "").replace(/\.[^.]+$/, ""));
          try { await api.post("/voices", fd); message.success("已上传"); onChange(); onSuccess?.({}); }
          catch (e: any) { message.error(e?.response?.data?.error || "上传失败"); onError?.(e as any); }
        }}><Button size="small" icon={<UploadOutlined />}>上传音频样本</Button></Upload>
        <VoiceRecorder onSave={() => { onChange(); message.success("录音已保存"); }} />
      </Space>
      <List size="small" dataSource={voices} locale={{ emptyText: "暂无声音样本" }}
        renderItem={(v) => (
          <List.Item actions={[
            <a key="p" href={`/voices-audio/${v.id}`} target="_blank" rel="noopener">试听</a>,
            <Popconfirm key="d" title="删除该声音？" onConfirm={() => del(v.id)}><a>删除</a></Popconfirm>,
          ]}>{v.name || v.id}</List.Item>
        )} />
    </div>
  );
}

function VoiceRecorder({ onSave }: { onSave: () => void }) {
  const { message } = AntApp.useApp();
  const [open, setOpen] = useState(false);
  const [recording, setRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [blob, setBlob] = useState<Blob | null>(null);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const mediaRecorder = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  useEffect(() => {
    return () => { // cleanup on unmount
      timerRef.current && clearInterval(timerRef.current);
      streamRef.current?.getTracks().forEach(t => t.stop());
    };
  }, []);

  async function openPanel() {
    setOpen(true); setBlob(null); setAudioUrl(null); setElapsed(0);
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = s;
    } catch (e: any) {
      message.error("无法访问麦克风：" + (e.message || ""));
    }
  }
  function closePanel() {
    setOpen(false); setRecording(false); setElapsed(0); setBlob(null); setAudioUrl(null); setName("");
    timerRef.current && clearInterval(timerRef.current); timerRef.current = null;
    streamRef.current?.getTracks().forEach(t => t.stop()); streamRef.current = null;
  }
  function toggleRecord() {
    if (recording) { stopRecord(); return; }
    const s = streamRef.current; if (!s) return;
    chunksRef.current = [];
    const mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "audio/mp4";
    const mr = new MediaRecorder(s, { mimeType: mime });
    mr.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data); };
    mr.onstop = () => {
      const b = new Blob(chunksRef.current, { type: mr.mimeType });
      setBlob(b); setAudioUrl(URL.createObjectURL(b));
      // Pre-fill a unique default name so saving never lands on a duplicate
      // "未命名声音"; the user can rename it before saving.
      const d = new Date(); const p = (n: number) => String(n).padStart(2, "0");
      setName((cur) => cur || `录音 ${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`);
    };
    mr.start();
    mediaRecorder.current = mr;
    setRecording(true);
    const start = Date.now();
    timerRef.current = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 200);
  }
  function stopRecord() {
    mediaRecorder.current?.stop(); setRecording(false);
    timerRef.current && clearInterval(timerRef.current); timerRef.current = null;
  }
  async function saveRecording() {
    if (!blob) return;
    if (!name.trim()) { message.warning("请先为声音命名"); return; }
    setSaving(true);
    try {
      const fd = new FormData();
      fd.append("blob", blob, "recording.webm");
      fd.append("name", name.trim());
      await api.post("/api/voices/record", fd);
      onSave();
      closePanel();
    } catch (e: any) {
      message.error(e?.response?.data?.error || "保存失败");
    }
    finally { setSaving(false); }
  }

  const fmt = (s: number) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;

  if (!open) return <Button size="small" icon={<AudioOutlined />} onClick={openPanel}>麦克风录音</Button>;

  return (
    <div style={{ display: "inline-flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
      {!blob ? (
        <Button size="small" onClick={toggleRecord}
          icon={recording ? <StopOutlined /> : <AudioOutlined />}
          danger={recording}>
          {recording ? `停止录音 ${fmt(elapsed)}` : "开始录制"}
        </Button>
      ) : (
        <>
          <audio src={audioUrl!} controls style={{ height: 32, maxWidth: 200 }} />
          <Input size="small" placeholder="声音名称" value={name}
            onChange={(e) => setName(e.target.value)} style={{ width: 160 }}
            onPressEnter={saveRecording} />
          <Button size="small" type="primary" icon={<CheckOutlined />}
            loading={saving} onClick={saveRecording}>保存</Button>
        </>
      )}
      <Button size="small" icon={<CloseOutlined />} onClick={closePanel}>取消</Button>
    </div>
  );
}

function CliTestButton({ form }: { form: any }) {
  const { message } = AntApp.useApp();
  const [loading, setLoading] = useState(false);

  async function testCli() {
    const tool = form.getFieldValue("cli_tool") || "auto";
    setLoading(true);
    try {
      const fd = new FormData(); fd.append("tool_id", tool);
      const r = await api.post<{ found: boolean; path?: string; version?: string; label?: string; error?: string }>("/api/test-cli-agent", fd);
      const d = r.data;
      if (d.found) {
        message.success(`${d.label || tool} 已找到：${d.path}${d.version ? ` (${d.version})` : ""}`);
      } else {
        message.warning(d.error || `${tool} 未找到`);
      }
    } catch (e: any) {
      message.error("检测失败：" + (e?.message || e));
    } finally { setLoading(false); }
  }

  return <Button icon={<ThunderboltOutlined />} loading={loading} onClick={testCli}>检测</Button>;
}

type ManagedRuntimeStatus = {
  ready: boolean;
  installed?: boolean;
  state?: "ready" | "unsupported" | "device_unavailable" | "validation_failed" | "validation_pending" | "partially_installed" | "not_installed";
  retry_validation?: boolean;
  supported?: boolean;
  asset_available?: boolean;
  installable?: boolean;
  platform?: string;
  accelerator?: string;
  device_ok?: boolean;
  device_name?: string;
  performance_warning?: string;
  gpu_ok: boolean;
  gpu_name: string;
  runtime_ok: boolean;
  models_ok: boolean;
  smoke_ok: boolean;
  reason: string;
  runtime_dir: string;
  models_dir: string;
};

/** One-click managed CosyVoice runtime/model installation. */
function CosyVoiceSetup({ onSuccess }: { onSuccess: () => void }) {
  const { message } = AntApp.useApp();
  const qc = useQueryClient();
  const [status, setStatus] = useState<ManagedRuntimeStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [installing, setInstalling] = useState(false);
  const [progress, setProgress] = useState<{ message: string; fraction: number } | null>(null);
  const [paused, setPaused] = useState(false);
  const [validationOnly, setValidationOnly] = useState(false);
  const [latestFailure, setLatestFailure] = useState("");

  const refresh = async () => {
    try {
      const value = await getJson<ManagedRuntimeStatus>("/api/cosyvoice/status");
      setStatus(value);
      setStatusError("");
      await qc.refetchQueries({ queryKey: ["capabilities"], type: "active" });
    } catch (error: any) {
      setStatusError(error?.response?.data?.detail || error?.message || "无法获取 runtime 状态");
      throw error;
    }
  };
  useEffect(() => { refresh().catch(() => {}); }, []);

  async function install(retryValidation = false) {
    setInstalling(true);
    setPaused(false);
    setValidationOnly(retryValidation);
    setProgress({
      message: retryValidation ? "准备重新运行实际合成验证…" : "准备安装…",
      fraction: retryValidation ? 0.95 : 0,
    });
    try {
      const body = new FormData();
      body.append("validation_only", retryValidation ? "true" : "false");
      const response = await fetch("/api/cosyvoice/setup", { method: "POST", body });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      if ((response.headers.get("content-type") || "").includes("application/json")) {
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.message || "安装失败");
        setLatestFailure("");
        message.success(payload.message || "CosyVoice 已就绪");
        await refresh(); onSuccess(); return;
      }
      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let finished = false;
      const handle = async (block: string) => {
        const event = block.split("\n").find((line) => line.startsWith("event:"))?.slice(6).trim();
        const raw = block.split("\n").filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart()).join("\n");
        if (!event || !raw || event === "heartbeat") return;
        const payload = JSON.parse(raw);
        if (event === "progress") setProgress(payload);
        if (event === "done" || event === "error") {
          finished = true;
          if (!payload.ok) {
            const reason = payload.message || payload.reason || "安装失败";
            setLatestFailure(reason);
            if (typeof payload.ready === "boolean") setStatus(payload);
            throw new Error(reason);
          }
          setLatestFailure("");
          message.success(payload.message || "CosyVoice 安装完成");
          await refresh(); onSuccess();
        }
      };
      while (!finished) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
        let boundary: number;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          await handle(block);
          if (finished) break;
        }
        if (done) break;
      }
      if (!finished) throw new Error("安装连接中断，请重试（已下载内容会续传）");
    } catch (error: any) {
      const reason = error?.message || String(error);
      setLatestFailure(reason);
      message.error("CosyVoice 安装失败：" + reason);
      await refresh().catch(() => {});
    } finally {
      setInstalling(false);
      setPaused(false);
      setProgress(null);
    }
  }

  return (
    <Card size="small" title="CosyVoice Runtime 与模型" style={{ marginBottom: 16 }}
      extra={status && <Tag color={status.ready ? "green" : status.installed ? "orange" : "default"}>
        {status.ready
          ? "已就绪"
          : status.state === "validation_failed"
            ? "已安装，验证失败"
            : status.installed ? "已安装，待验证" : "未就绪"}
      </Tag>}>
      {installing && progress ? (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Text>{progress.message}</Text>
          <Progress percent={Math.max(1, Math.round(Math.max(0, progress.fraction) * 100))} status={paused ? "normal" : "active"} />
          {!validationOnly && <Space>
            {paused ? (
              <Button icon={<PlayCircleOutlined />} onClick={async () => {
                try { await postForm("/api/cosyvoice/resume", {}); setPaused(false); } catch {}
              }}>继续</Button>
            ) : (
              <Button icon={<PauseCircleOutlined />} onClick={async () => {
                try { await postForm("/api/cosyvoice/pause", {}); setPaused(true); } catch {}
              }}>暂停</Button>
            )}
            <Button danger icon={<StopOutlined />} onClick={async () => {
              try { await postForm("/api/cosyvoice/cancel", {}); } catch {}
              setProgress({ message: "正在取消…", fraction: progress?.fraction ?? 0 });
            }}>取消</Button>
          </Space>}
        </Space>
      ) : (
        <Space direction="vertical">
          {status && (status.supported === false ? (
            <Tag color="red">当前平台无 runtime 资产</Tag>
          ) : (
            <Space wrap>
              <Tag color={(status.device_ok ?? status.gpu_ok) ? "green" : "red"}>
                {status.accelerator === "CPU" ? "CPU" : "NVIDIA GPU"}
              </Tag>
              <Tag color={status.runtime_ok ? "green" : "default"}>
                {status.accelerator === "CPU" ? "CPU Runtime" : "CUDA Runtime"}
              </Tag>
              <Tag color={status.models_ok ? "green" : "default"}>CosyVoice2 模型</Tag>
              <Tag color={status.smoke_ok ? "green" : status.state === "validation_failed" ? "red" : "default"}>实际合成验证</Tag>
            </Space>
          ))}
          {statusError && <Text type="danger">状态读取失败：{statusError}。请刷新页面或检查服务日志。</Text>}
          <Text type="secondary">
            {status?.ready
              ? `CosyVoice 已通过运行验证${status.device_name || status.gpu_name ? `（${status.device_name || status.gpu_name}）` : ""}。`
              : status?.asset_available === false
                ? "当前没有适配此平台的可下载 runtime 资产；模型与 API 字段无需手动配置。"
                : status?.installed
                  ? "Runtime 与模型均已安装；只需重新运行实际合成验证，不会重复下载。"
                  : `下载安装独立${status?.accelerator === "CPU" ? " CPU" : " CUDA"} Runtime 与约 4.9GB 模型，支持断点续传。`}
          </Text>
          {!status?.ready && (latestFailure || status?.reason) && (
            <Text type="danger" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
              {latestFailure || status?.reason}
            </Text>
          )}
          {status?.performance_warning && <Text type="warning">{status.performance_warning}</Text>}
          <Space>
            {!status?.ready && <Button type="primary"
              icon={status?.retry_validation || status?.installed ? <ReloadOutlined /> : <DownloadOutlined />}
              disabled={!status || !(status.installable ?? status.gpu_ok)}
              onClick={() => install(!!(status?.retry_validation || status?.installed))}>
              {status?.retry_validation || status?.installed
                ? "重新运行合成验证"
                : "下载安装 Runtime 与模型"}
            </Button>}
            {status?.ready && (
              <Button icon={<ReloadOutlined />} onClick={async () => {
                await refresh().catch(() => {});
                onSuccess();
              }}>检查更新</Button>
            )}
          </Space>
        </Space>
      )}
    </Card>
  );
}

/** One-click SadTalker setup: status display + download with SSE progress. */
function SadTalkerSetup({ data, onSuccess }: { data: SettingsData; onSuccess: () => void }) {
  const { message } = AntApp.useApp();
  const [downloading, setDownloading] = useState(false);
  const [progress, setProgress] = useState<{ message: string; fraction: number } | null>(null);
  const [paused, setPaused] = useState(false);
  const [validationOnly, setValidationOnly] = useState(false);
  const [latestFailure, setLatestFailure] = useState("");

  type SadTalkerStatus = ManagedRuntimeStatus;

  async function fetchStatus(): Promise<SadTalkerStatus> {
    const r = await getJson<SadTalkerStatus>("/api/sadtalker/status");
    return r;
  }

  async function startSetup(retryValidation = false) {
    setDownloading(true);
    setPaused(false);
    setValidationOnly(retryValidation);
    setProgress({
      message: retryValidation ? "准备重新运行实际推理验证…" : "准备下载…",
      fraction: retryValidation ? 0.97 : 0,
    });

    try {
      const fd = new FormData();
      fd.append("mirror", "true");
      fd.append("validation_only", retryValidation ? "true" : "false");

      // Use fetch + ReadableStream for SSE (axios doesn't support streaming well)
      const resp = await fetch("/api/sadtalker/setup", { method: "POST", body: fd });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

      // The already-installed fast path returns ordinary JSON rather than SSE.
      if ((resp.headers.get("content-type") || "").includes("application/json")) {
        const payload = await resp.json();
        setDownloading(false);
        setPaused(false);
        setProgress(null);
        if (payload.ok) {
          setLatestFailure("");
          message.success(payload.message || "数字人模型已就绪");
          fetchStatus().then(setStatus).catch(() => {});
          onSuccess();
        } else {
          const reason = payload.message || payload.reason || "安装失败";
          setLatestFailure(reason);
          if (typeof payload.ready === "boolean") setStatus(payload);
          message.error(reason);
          fetchStatus().then(setStatus).catch(() => {});
        }
        return;
      }

      const reader = resp.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      function handleEvent(block: string): boolean {
        const lines = block.split("\n");
        const eventType = lines.find((line) => line.startsWith("event:"))
          ?.slice(6).trim() || "";
        const dataText = lines.filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart()).join("\n");
        if (!eventType || !dataText || eventType === "heartbeat") return false;
        try {
          const payload = JSON.parse(dataText);
          if (eventType === "progress") {
            setProgress({ message: payload.message, fraction: payload.fraction });
          } else if (eventType === "done" || eventType === "error") {
            setDownloading(false);
            setPaused(false);
            setProgress(null);
            if (payload.ok) {
              setLatestFailure("");
              message.success(payload.message || "数字人模型安装完成！");
              fetchStatus().then(setStatus).catch(() => {});
              onSuccess();
            } else {
              const reason = payload.message || payload.reason || "安装失败";
              setLatestFailure(reason);
              if (typeof payload.ready === "boolean") setStatus(payload);
              message.error(reason);
              // Refresh filesystem-derived status, while latestFailure remains
              // visible even if an older server omits persisted failure state.
              fetchStatus().then(setStatus).catch(() => {});
            }
            return true;
          }
        } catch { /* ignore malformed/non-JSON event */ }
        return false;
      }

      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        // Parse complete SSE blocks, not individual lines. An `event:` line and
        // its `data:` line may arrive in different network chunks.
        buffer = buffer.replace(/\r\n?/g, "\n");
        let boundary: number;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          if (handleEvent(block)) return;
        }
        if (done) break;
      }
      if (buffer.trim() && handleEvent(buffer.trim())) return;
      // If we exit the loop without a "done" event, something went wrong
      setDownloading(false);
      setPaused(false);
      setProgress(null);
      const reason = "安装连接中断，未收到完成状态，请重试";
      setLatestFailure(reason);
      message.warning(reason);
    } catch (e: any) {
      setDownloading(false);
      setPaused(false);
      setProgress(null);
      const reason = "安装失败：" + (e?.message || e);
      setLatestFailure(reason);
      message.error(reason);
      fetchStatus().then(setStatus).catch(() => {});
    }
  }

  // Auto-fetch status on mount to check models
  const [status, setStatus] = useState<SadTalkerStatus | null>(null);
  useEffect(() => { fetchStatus().then(setStatus).catch(() => {}); }, []);

  return (
    <Card size="small" title="数字人 Runtime 与模型" style={{ marginTop: 8 }}
      extra={status && <Tag color={status.ready ? "green" : status.installed ? "orange" : "default"}>
        {status.ready
          ? "已就绪"
          : status.state === "validation_failed"
            ? "已安装，验证失败"
            : status.installed ? "已安装，待验证" : "未就绪"}
      </Tag>}>
      {downloading && progress ? (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Text>{progress.message}</Text>
          <Progress percent={Math.max(1, Math.round(progress.fraction * 100))} status={paused ? "normal" : "active"} />
          {!validationOnly && <Space>
            {paused ? (
              <Button icon={<PlayCircleOutlined />} onClick={async () => {
                try { await postForm("/api/sadtalker/resume", {}); setPaused(false); } catch {}
              }}>继续</Button>
            ) : (
              <Button icon={<PauseCircleOutlined />} onClick={async () => {
                try { await postForm("/api/sadtalker/pause", {}); setPaused(true); } catch {}
              }}>暂停</Button>
            )}
            <Button danger icon={<StopOutlined />} onClick={async () => {
              try { await postForm("/api/sadtalker/cancel", {}); } catch {}
              setProgress({ message: "正在取消…", fraction: progress?.fraction ?? 0 });
            }}>取消</Button>
          </Space>}
        </Space>
      ) : (
        <Space direction="vertical">
          {status && (status.supported === false ? (
            <Tag color="red">当前平台无 runtime 资产</Tag>
          ) : (
            <Space wrap>
              <Tag color={(status.device_ok ?? status.gpu_ok) ? "green" : "red"}>
                {status.accelerator === "CPU" ? "CPU" : "NVIDIA GPU"}
              </Tag>
              <Tag color={status.runtime_ok ? "green" : "default"}>
                {status.accelerator === "CPU" ? "CPU Runtime" : "CUDA Runtime"}
              </Tag>
              <Tag color={status.models_ok ? "green" : "default"}>模型文件</Tag>
              <Tag color={status.smoke_ok ? "green" : status.state === "validation_failed" ? "red" : "default"}>
                运行验证
              </Tag>
            </Space>
          ))}
          <Text type="secondary">
            {status?.ready
              ? `SadTalker 已通过运行验证${status.device_name || status.gpu_name ? `（${status.device_name || status.gpu_name}）` : ""}，可直接使用口型同步。`
              : status?.supported === false
                ? "当前平台没有适配的 SadTalker runtime。"
                : status?.installed
                  ? "Runtime 与模型均已安装；只需重新运行实际推理验证，不会重复下载。"
                : `安装将下载独立的${status?.accelerator === "CPU" ? " CPU" : " NVIDIA CUDA"} Runtime 与约 1GB 模型，支持断点续传。`}
          </Text>
          {!status?.ready && (latestFailure || status?.reason) && (
            <Text type="danger" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
              {latestFailure || status?.reason}
            </Text>
          )}
          {status?.performance_warning && <Text type="warning">{status.performance_warning}</Text>}
          <Space>
            {!status?.ready && (
              <Button type="primary"
                icon={status?.retry_validation || status?.installed ? <ReloadOutlined /> : <DownloadOutlined />}
                onClick={() => startSetup(!!(status?.retry_validation || status?.installed))}
                disabled={!status || !(status.installable ?? status.gpu_ok)}>
                {status?.retry_validation || status?.installed
                  ? "重新运行推理验证"
                  : "下载安装 Runtime 与模型"}
              </Button>
            )}
            {status?.ready && (
              <Button icon={<ReloadOutlined />} onClick={async () => {
                const s = await fetchStatus().catch(() => null);
                if (s) setStatus(s);
                onSuccess();
              }}>检查更新</Button>
            )}
          </Space>
        </Space>
      )}
    </Card>
  );
}
