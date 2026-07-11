import { useQuery } from "@tanstack/react-query";
import {
  App as AntApp, Button, Card, Input, Select, Space, Typography, Upload,
} from "antd";
import { useEffect, useState } from "react";
import { api, getJson } from "../api/client";

const { Text } = Typography;

interface Scene {
  narration?: string; visual?: string; media?: string;
  bg_custom?: string; audio?: string; audio_error?: string;
}
interface Script { scenes: Scene[]; images: string[]; videos: string[]; uploads?: string[]; }

export default function SceneEditor({ draftId, ttsVoice }: { draftId: number; ttsVoice?: string }) {
  const { message } = AntApp.useApp();
  const { data } = useQuery({ queryKey: ["script", draftId], queryFn: () => getJson<Script>(`/api/script/${draftId}`) });
  const [scenes, setScenes] = useState<Scene[]>([]);
  const [images, setImages] = useState<string[]>([]);
  const [videos, setVideos] = useState<string[]>([]);
  const [uploads, setUploads] = useState<string[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (data && !loaded) {
      setScenes(data.scenes || []);
      setImages(data.images || []);
      setVideos(data.videos || []);
      const up = new Set<string>(data.uploads || []);
      (data.scenes || []).forEach((s) => { if (s.bg_custom) up.add(s.bg_custom); });
      setUploads([...up]);
      setLoaded(true);
    }
  }, [data]);

  if (!data) return null;
  if (!scenes.length) return <Text type="secondary">还没有分镜脚本，请先「生成讲解脚本+配音」。</Text>;

  const setScene = (i: number, patch: Partial<Scene>) =>
    setScenes((s) => s.map((x, j) => (j === i ? { ...x, ...patch } : x)));

  function mediaOptions(): { value: string; label: string }[] {
    const base = (u: string) => { try { return decodeURIComponent(u.split("/").pop()!.split("?")[0]); } catch { return u; } };
    return [
      { value: "none", label: "none（纯色/自动配图）" },
      ...images.map((u, i) => ({ value: `image:${i + 1}`, label: `🖼 image:${i + 1}（${base(u)}）` })),
      ...videos.map((u, i) => ({ value: `video:${i + 1}`, label: `▶ video:${i + 1}（${base(u)}）` })),
      ...uploads.map((p) => ({ value: `upload:${p}`, label: `☝ ${p.replace(/^bg[-_]/i, "")}` })),
    ];
  }
  function thumb(sc: Scene) {
    let path: string | null = null;
    const m = /^upload:(.+)$/.exec(sc.media || "");
    if (m) path = m[1]; else if (sc.bg_custom) path = sc.bg_custom;
    if (path) return <img src={`/videos/draft-${draftId}/${path}`} style={{ height: 48, borderRadius: 4 }} />;
    const im = /^image:(\d+)$/.exec(sc.media || "");
    if (im && images[+im[1] - 1]) return <img src={images[+im[1] - 1]} style={{ height: 48, borderRadius: 4 }} />;
    return <Text type="secondary" style={{ fontSize: 12 }}>（无背景）</Text>;
  }

  function setMedia(i: number, v: string) {
    const m = /^upload:(.+)$/.exec(v);
    setScene(i, m ? { media: v, bg_custom: m[1] } : { media: v, bg_custom: undefined });
  }

  async function uploadBg(i: number, file: File) {
    const fd = new FormData(); fd.append("file", file);
    const r = await api.post(`/api/script/${draftId}/upload-bg`, fd);
    if (r.data.ok) { setUploads((u) => [...new Set([...u, r.data.path])]); setMedia(i, `upload:${r.data.path}`); message.success("已上传背景"); }
    else message.error("上传失败");
  }
  async function aiBg(i: number) {
    const sc = scenes[i]; const prompt = sc.visual || sc.narration || "";
    if (!prompt) { message.warning("请先填写画面描述或旁白"); return; }
    message.loading({ content: "AI 生成背景中…", key: `ai${i}` });
    const fd = new FormData(); fd.append("prompt", prompt);
    const r = await api.post(`/api/script/${draftId}/ai-bg`, fd);
    if (r.data.ok) { setUploads((u) => [...new Set([...u, r.data.path])]); setMedia(i, `upload:${r.data.path}`); message.success({ content: "已生成背景", key: `ai${i}` }); }
    else message.error({ content: r.data.error || "生成失败", key: `ai${i}` });
  }
  async function scrapeBg(i: number) {
    const sc = scenes[i]; const q = (sc.visual || sc.narration || "").trim();
    if (!q) { message.warning("请先填写画面描述或旁白"); return; }
    message.loading({ content: "联网搜索背景中…", key: `sc${i}` });
    const fd = new FormData(); fd.append("query", q);
    const r = await api.post(`/api/script/${draftId}/scrape-bg`, fd);
    if (r.data.ok) { setUploads((u) => [...new Set([...u, r.data.path])]); setMedia(i, `upload:${r.data.path}`); message.success({ content: "已获取背景", key: `sc${i}` }); }
    else message.error({ content: r.data.error || "搜索失败", key: `sc${i}` });
  }

  function move(i: number, d: number) {
    const j = i + d; if (j < 0 || j >= scenes.length) return;
    const next = [...scenes]; [next[i], next[j]] = [next[j], next[i]]; setScenes(next);
  }
  const del = (i: number) => setScenes((s) => s.filter((_, j) => j !== i));
  const add = () => setScenes((s) => [...s, { narration: "", visual: "", media: "none" }]);

  async function saveScenes(resynth: boolean) {
    setBusy(true);
    try {
      await api.post(`/api/script/${draftId}`, { scenes });
      if (resynth) {
        const indices = scenes.map((_, i) => i);
        await api.post(`/api/script/${draftId}/resynth`, { indices, scenes, tts_voice: ttsVoice });
        message.success("已保存并开始重新配音");
      } else message.success("已保存脚本");
    } finally { setBusy(false); }
  }
  async function resynthOne(i: number) {
    await api.post(`/api/script/${draftId}/resynth`, { indices: [i], scenes, tts_voice: ttsVoice });
    message.success("已开始配音，稍后刷新");
  }

  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      {scenes.map((sc, i) => (
        <Card key={i} size="small" title={`分镜 ${i + 1}`} extra={
          <Space>
            <Button size="small" onClick={() => move(i, -1)}>↑</Button>
            <Button size="small" onClick={() => move(i, 1)}>↓</Button>
            <Button size="small" danger onClick={() => del(i)}>删除</Button>
          </Space>}>
          <Text type="secondary">旁白</Text>
          <Input.TextArea rows={2} value={sc.narration || ""} onChange={(e) => setScene(i, { narration: e.target.value })} />
          <Text type="secondary">画面描述</Text>
          <Input value={sc.visual || ""} onChange={(e) => setScene(i, { visual: e.target.value })} />
          <Text type="secondary">背景素材</Text>
          <Space wrap style={{ width: "100%" }}>
            <Select style={{ width: 260 }} value={sc.media || "none"} onChange={(v) => setMedia(i, v)} options={mediaOptions()} />
            {thumb(sc)}
            <Upload showUploadList={false} customRequest={({ file }) => uploadBg(i, file as File)}>
              <Button size="small">上传</Button>
            </Upload>
            <Button size="small" onClick={() => scrapeBg(i)}>网上抓取</Button>
            <Button size="small" onClick={() => aiBg(i)}>AI生成</Button>
            {sc.bg_custom && <Button size="small" danger onClick={() => setScene(i, { bg_custom: undefined, media: "none" })}>清除</Button>}
          </Space>
          <div style={{ marginTop: 8 }}>
            {sc.audio ? (
              <Space>
                <audio controls preload="none" src={`/videos/draft-${draftId}/${sc.audio}`} />
                <Button size="small" onClick={() => resynthOne(i)}>重新配音</Button>
              </Space>
            ) : (
              <Space>
                <Text type="secondary">（无配音{sc.audio_error ? `：${sc.audio_error}` : ""}）</Text>
                <Button size="small" onClick={() => resynthOne(i)}>生成配音</Button>
              </Space>
            )}
          </div>
        </Card>
      ))}
      <Space wrap>
        <Button size="small" onClick={add}>＋ 新增分镜</Button>
        <Button type="primary" loading={busy} onClick={() => saveScenes(false)}>保存脚本</Button>
        <Button type="primary" loading={busy} onClick={() => saveScenes(true)}>保存并重配改动的配音</Button>
      </Space>
    </Space>
  );
}
