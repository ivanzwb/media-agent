import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App as AntApp, Button, Collapse, Select, Space, Table, Tag, Typography, Badge,
} from "antd";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getJson, postForm } from "../api/client";
import ArticleViewModal from "../components/ArticleViewModal";

const { Title, Text } = Typography;

interface Article {
  id: number; title: string; url: string; topic: string;
  source_name: string; published_at: string | null; fetched_at: string;
}
interface Group { date: string; articles: Article[]; }
interface ArchiveData { groups: Group[]; topics: string[]; draft_map: Record<string, number>; }
interface RewriteEntry {
  article_id: number; title: string; running: boolean; done: boolean;
  error: string | null; draft_id: number | null; logs: string[];
}

export default function Archive() {
  const qc = useQueryClient();
  const { message, modal } = AntApp.useApp();
  const [params, setParams] = useSearchParams();
  const topic = params.get("topic") || "";
  const [style, setStyle] = useState("");
  const [viewId, setViewId] = useState<number | null>(null);
  const [polling, setPolling] = useState(false);

  const { data } = useQuery({
    queryKey: ["archive", topic],
    queryFn: () => getJson<ArchiveData>("/api/archive", topic ? { topic } : undefined),
  });
  const { data: styleData } = useQuery({
    queryKey: ["rewrite-styles"],
    queryFn: () => getJson<{ styles: { id: string; name: string; is_builtin: boolean }[] }>("/api/rewrite-styles"),
  });
  const { data: rewriteState } = useQuery({
    queryKey: ["rewrite-all-status"],
    queryFn: () => getJson<{ entries: RewriteEntry[] }>("/api/rewrite-all-status"),
    refetchInterval: polling ? 1200 : false,
  });

  const entries = rewriteState?.entries || [];
  useEffect(() => {
    if (entries.length && entries.some((e) => !e.done)) setPolling(true);
    else if (polling && entries.every((e) => e.done)) {
      setPolling(false);
      qc.invalidateQueries({ queryKey: ["archive"] });
    }
  }, [entries]);

  const draftMap = data?.draft_map || {};

  async function rewrite(id: number, isRewrite: boolean) {
    if (isRewrite && !confirm("重写会覆盖该文章已有的主稿草稿，确定吗？")) return;
    try {
      const r = await postForm<{ started?: boolean; running?: boolean; message?: string }>(
        `/archive/${id}/rewrite`, style ? { style } : {});
      if (r.started === false) { message.info(r.message || "该文章已有转写任务"); }
      setPolling(true);
      qc.invalidateQueries({ queryKey: ["rewrite-all-status"] });
    } catch { message.error("转写请求失败"); }
  }

  async function refetch(id: number) {
    message.loading({ content: "重新抓取中…", key: `rf${id}` });
    try {
      const r = await postForm<{ ok: boolean; error?: string; images?: number; videos?: number }>(`/archive/${id}/refetch`);
      if (r.ok) message.success({ content: `抓取成功（图 ${r.images} · 视频 ${r.videos}）`, key: `rf${id}` });
      else message.error({ content: r.error || "抓取失败", key: `rf${id}` });
    } catch { message.error({ content: "请求失败", key: `rf${id}` }); }
  }

  const columns = [
    { title: "标题", render: (_: any, a: Article) => <a href={a.url} target="_blank" rel="noopener">{a.title}</a> },
    { title: "主题", dataIndex: "topic", width: 110 },
    { title: "来源", dataIndex: "source_name", width: 140 },
    { title: "发布时间", width: 110, render: (_: any, a: Article) => a.published_at ? a.published_at.slice(0, 10) : <Text type="secondary">—</Text> },
    { title: "抓取时间", width: 110, render: (_: any, a: Article) => a.fetched_at?.slice(0, 10) },
    { title: "转写", width: 300, render: (_: any, a: Article) => {
      const draftId = draftMap[String(a.id)];
      return (
        <Space wrap>
          <Button size="small" onClick={() => setViewId(a.id)}>查看原文</Button>
          <Button size="small" onClick={() => refetch(a.id)}>重新抓取</Button>
          {draftId ? <>
            <Tag color="green">✓ 已转写</Tag>
            <Link to={`/drafts/${draftId}/edit?from=archive`}><Button size="small">查看草稿</Button></Link>
            <Button size="small" onClick={() => rewrite(a.id, true)}>重写</Button>
          </> : <Button size="small" type="primary" onClick={() => rewrite(a.id, false)}>转写</Button>}
        </Space>
      );
    } },
  ];

  const runningCount = entries.filter((e) => e.running).length;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Title level={2} style={{ margin: 0 }}>归档浏览</Title>

      <Space wrap>
        <Text strong>主题</Text>
        <Tag.CheckableTag checked={!topic} onChange={() => setParams({})}>全部</Tag.CheckableTag>
        {(data?.topics || []).map((t) => (
          <Tag.CheckableTag key={t} checked={topic === t} onChange={() => setParams({ topic: t })}>{t}</Tag.CheckableTag>
        ))}
        <span style={{ marginLeft: "auto" }} />
        <Text>转写风格</Text>
        <Select size="small" style={{ width: 200 }} value={style} onChange={setStyle}
          options={[{ value: "", label: "默认（全局）" },
            ...(styleData?.styles || []).map((s) => ({ value: s.id, label: s.name + (s.is_builtin ? "" : "（自定义）") }))]} />
      </Space>

      {data?.groups?.length ? (
        <Collapse defaultActiveKey={data.groups[0]?.date} items={data.groups.map((g) => ({
          key: g.date,
          label: <Space><b>{g.date}</b><Badge count={g.articles.length} color="#07C160" /></Space>,
          children: <Table rowKey="id" size="small" pagination={false}
            dataSource={g.articles} columns={columns} />,
        }))} />
      ) : <Text type="secondary">暂无文章。</Text>}

      {entries.length > 0 && (
        <div className="ma-run-panel" style={{ maxHeight: 340, overflowY: "auto" }}>
          <div className="ma-run-head">
            <span>{runningCount > 0 ? `转写中 (${runningCount})` : "转写完成"}</span>
            <Button size="small" type="text" style={{ color: "#ddd" }}
              onClick={() => { setPolling(false); qc.setQueryData(["rewrite-all-status"], { entries: [] }); }}>×</Button>
          </div>
          <div style={{ padding: 8 }}>
            {entries.map((e) => (
              <div key={e.article_id} style={{ marginBottom: 6 }}>
                <Text style={{ color: "#ddd", fontSize: 12 }}>
                  {e.done && e.draft_id ? "✅ " : e.done && e.error ? "❌ " : "⏳ "}
                  {e.title || `#${e.article_id}`}
                </Text>
                <pre className="ma-run-logs" style={{ maxHeight: 80 }}>{(e.logs || []).join("\n")}</pre>
              </div>
            ))}
          </div>
        </div>
      )}

      <ArticleViewModal articleId={viewId} onClose={() => setViewId(null)} />
    </Space>
  );
}
