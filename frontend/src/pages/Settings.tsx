import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App as AntApp, Button, Card, Divider, Form, Input, InputNumber, Select,
  Space, Switch, Tag, Typography, List, Upload, Popconfirm, Tabs, Modal,
} from "antd";
import { UploadOutlined, AudioOutlined, StopOutlined, CheckOutlined, CloseOutlined } from "@ant-design/icons";
import { useState, useRef, useEffect } from "react";
import { api, getJson, postForm } from "../api/client";

const { Title, Text, Paragraph } = Typography;

interface MaskField { set: boolean; masked: string; }
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

  if (!data) return null;

  const refetch = () => qc.invalidateQueries({ queryKey: ["settings"] });

  async function onSave() {
    setSaving(true);
    try {
      const v = await form.getFieldsValue();
      const keyFields = ["llm_api_key", "image_api_key", "tts_api_key", "wechat_appsecret"];
      const payload: Record<string, any> = {};
      const boolFields = ["schedule_enabled", "download_images", "download_videos"];
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
        tts_provider: data.tts_provider, tts_api_base: data.tts_api_base, tts_model: data.tts_model, tts_voice: data.tts_voice,
        max_age_days: data.max_age_days, max_per_source: data.max_per_source, download_workers: data.download_workers,
        video_fit: data.video_fit, video_brand_name: data.video_brand_name,
        sensitive_level: data.sensitive_level, sensitive_words: data.sensitive_words,
        promotion_footer: data.promotion_footer,
        wechat_appid: data.wechat_appid, wechat_author: data.wechat_author,
        rewrite_style: data.rewrite_style,
        download_images: data.download_images, download_videos: data.download_videos,
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
                  <Form.Item name="llm_provider" label="Provider"><Input placeholder="mock / openai / anthropic …" /></Form.Item>
                  <Form.Item name="llm_model" label="模型"><Input /></Form.Item>
                  <Form.Item name="llm_api_base" label="API Base"><Input /></Form.Item>
                  <Form.Item name="llm_api_key" label={<>API Key {data.llm_api_key.set && <Tag color="green">已设置 {data.llm_api_key.masked}</Tag>}</>}>
                    <Input.Password placeholder={data.llm_api_key.set ? "留空则保持不变" : "输入 API Key"} />
                  </Form.Item>
                </Card>
                <Card title="图片" size="small" style={{ marginBottom: 16 }}>
                  <Form.Item name="image_provider" label="Provider"><Input placeholder="mock / openai …" /></Form.Item>
                  <Form.Item name="image_model" label="模型"><Input /></Form.Item>
                  <Form.Item name="image_api_base" label="API Base"><Input /></Form.Item>
                  <Form.Item name="image_api_key" label={<>API Key {data.image_api_key.set && <Tag color="green">已设置 {data.image_api_key.masked}</Tag>}</>}>
                    <Input.Password placeholder={data.image_api_key.set ? "留空则保持不变" : "输入 API Key"} />
                  </Form.Item>
                </Card>
                <Card title="Agent（优先于 LLM Provider）" size="small">
                  <Form.Item name="cli_tool" label="CLI Agent">
                    <Select options={["auto", "opencode", "claude", "codex", "copilot", "cursor-agent", "none"].map((v) => ({ value: v }))} />
                  </Form.Item>
                </Card>
              </>
            ),
          },
          {
            key: "voice", label: "语音", forceRender: true, children: (
              <Card title="TTS 与声音库" size="small">
                <Form.Item name="tts_provider" label="Provider"><Input placeholder="kitten / cosyvoice / fishaudio …" /></Form.Item>
                <Form.Item name="tts_api_base" label="API Base"><Input /></Form.Item>
                <Form.Item name="tts_model" label="模型"><Input /></Form.Item>
                <Form.Item name="tts_voice" label="音色">
                  <Select allowClear showSearch options={(data.voices || []).map((v) => ({ value: v.id, label: v.name || v.id }))} />
                </Form.Item>
                <Form.Item name="tts_api_key" label={<>API Key {data.tts_api_key.set && <Tag color="green">已设置 {data.tts_api_key.masked}</Tag>}</>}>
                  <Input.Password placeholder={data.tts_api_key.set ? "留空则保持不变" : "输入 API Key"} />
                </Form.Item>
                <Divider />
                <VoiceManager voices={data.voices || []} onChange={refetch} />
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
                <Card title="推广文案" size="small">
                  <Form.Item name="promotion_footer" label="推广文案（追加到草稿末尾）"><Input.TextArea rows={3} /></Form.Item>
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
                  <Form.Item name="download_workers" label={`并发下载数（默认 ${data.default_workers}）`}><Input /></Form.Item>
                  <Form.Item name="download_images" label="本地化图片" valuePropName="checked"
                    tooltip="归档时把文章图片下载到本地，避免防盗链失效；关闭则保留远程 URL（MEDIA_AGENT_DOWNLOAD_IMAGES）">
                    <Switch />
                  </Form.Item>
                  <Form.Item name="download_videos" label="本地化视频" valuePropName="checked"
                    tooltip="归档时把文章视频下载到本地（需 yt-dlp）；关闭则保留远程 URL（MEDIA_AGENT_DOWNLOAD_VIDEOS）">
                    <Switch />
                  </Form.Item>
                  <Form.Item name="video_fit" label="视频适配">
                    <Select options={["fit", "crop", "blur"].map((v) => ({ value: v }))} />
                  </Form.Item>
                  <Form.Item name="video_brand_name" label="视频品牌名"><Input /></Form.Item>
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
            key: "io", label: "导入导出", forceRender: true, children: (
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
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<StylePub | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

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
          const fd = new FormData(); fd.append("file", file as File);
          try { await api.post("/voices", fd); message.success("已上传"); onChange(); onSuccess?.({}); }
          catch (e) { message.error("上传失败"); onError?.(e as any); }
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
    setOpen(false); setRecording(false); setElapsed(0); setBlob(null); setAudioUrl(null);
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
    setSaving(true);
    try {
      const fd = new FormData();
      fd.append("blob", blob, "recording.webm");
      await api.post("/api/voices/record", fd);
      onSave();
      closePanel();
    } catch { message.error("保存失败"); }
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
          <Button size="small" type="primary" icon={<CheckOutlined />}
            loading={saving} onClick={saveRecording}>保存</Button>
        </>
      )}
      <Button size="small" icon={<CloseOutlined />} onClick={closePanel}>取消</Button>
    </div>
  );
}
