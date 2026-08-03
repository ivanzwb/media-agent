import {
  useInfiniteQuery, useQuery, useQueryClient,
} from "@tanstack/react-query";
import {
  App as AntApp, Button, Collapse, Select, Space, Table, Tag, Typography, Badge,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, getJson, postForm } from "../api/client";
import ArticleViewModal from "../components/ArticleViewModal";

const { Title, Text } = Typography;

interface Article {
  id: number; title: string; url: string; topic: string;
  source_name: string; published_at: string | null; fetched_at: string;
  has_video: boolean;
}
interface Group { date: string; articles: Article[]; }
interface ArchiveData {
  groups: Group[]; topics: string[]; draft_map: Record<string, number>;
  total: number; offset: number; limit: number; has_more: boolean;
}

// One screenful. The rest arrives when the reader asks for it, so opening the
// page costs the same whether the archive holds fifty articles or five thousand.
const PAGE_SIZE = 60;
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
  const [selectedIds, setSelectedIds] = useState<number[]>([]);

  const {
    data: pages, fetchNextPage, hasNextPage, isFetchingNextPage, isLoading,
  } = useInfiniteQuery({
    queryKey: ["archive", topic],
    queryFn: ({ pageParam }) => getJson<ArchiveData>("/api/archive", {
      ...(topic ? { topic } : {}), limit: PAGE_SIZE, offset: pageParam,
    }),
    initialPageParam: 0,
    getNextPageParam: (last: ArchiveData) =>
      (last.has_more ? last.offset + last.limit : undefined),
  });

  // Later pages can land on a date an earlier page already opened, so days are
  // merged rather than repeated.
  const data = useMemo(() => {
    const loaded = pages?.pages || [];
    const byDate = new Map<string, Article[]>();
    const draft_map: Record<string, number> = {};
    for (const page of loaded) {
      for (const group of page.groups) {
        byDate.set(group.date, [
          ...(byDate.get(group.date) || []), ...group.articles]);
      }
      Object.assign(draft_map, page.draft_map);
    }
    const groups = [...byDate.entries()]
      .sort((a, b) => (a[0] < b[0] ? 1 : -1))
      .map(([date, articles]) => ({ date, articles }));
    return {
      groups,
      draft_map,
      topics: loaded[0]?.topics || [],
      total: loaded[0]?.total || 0,
      shown: groups.reduce((sum, group) => sum + group.articles.length, 0),
    };
  }, [pages]);
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
    else if (polling && entries.length > 0 && entries.every((e) => e.done)) {
      setPolling(false);
      qc.invalidateQueries({ queryKey: ["archive"] });
    }
  }, [entries]);

  const draftMap = data.draft_map;

  async function rewrite(article: Article, isRewrite: boolean) {
    if (isRewrite && !confirm("重写会覆盖该文章已有的主稿草稿，确定吗？")) return;
    const id = article.id;
    const optimisticEntry: RewriteEntry = {
      article_id: id, title: article.title, running: true, done: false,
      error: null, draft_id: null, logs: ["正在提交转写任务…"],
    };
    qc.setQueryData<{ entries: RewriteEntry[] }>(["rewrite-all-status"], (current) => ({
      entries: [
        optimisticEntry,
        ...(current?.entries || []).filter((entry) => entry.article_id !== id),
      ],
    }));
    setPolling(true);
    try {
      const r = await postForm<{ started?: boolean; running?: boolean; message?: string }>(
        `/archive/${id}/rewrite`, style ? { style } : {});
      if (r.started === false) { message.info(r.message || "该文章已有转写任务"); }
      await qc.refetchQueries({ queryKey: ["rewrite-all-status"] });
    } catch {
      qc.setQueryData<{ entries: RewriteEntry[] }>(["rewrite-all-status"], (current) => ({
        entries: (current?.entries || []).filter((entry) => entry.article_id !== id),
      }));
      message.error("转写请求失败");
    }
  }

  function batchDelete() {
    if (!selectedIds.length) return;
    modal.confirm({
      title: `确定删除选中的 ${selectedIds.length} 篇文章？`,
      content: "将同时删除归档文件及关联的草稿，且不可恢复。",
      okText: "删除", okType: "danger", cancelText: "取消",
      onOk: async () => {
        try {
          const r = await api.post<{ ok: boolean; deleted: number }>("/api/archive/delete", { ids: selectedIds });
          message.success(`已删除 ${r.data.deleted} 篇`);
          setSelectedIds([]);
          qc.invalidateQueries({ queryKey: ["archive"] });
        } catch { message.error("删除失败"); }
      },
    });
  }

  async function refetch(id: number) {
    message.loading({ content: "重新抓取中…", key: `rf${id}` });
    try {
      const r = await postForm<{ ok: boolean; error?: string; images?: number; videos?: number }>(`/archive/${id}/refetch`);
      if (r.ok) {
        message.success({ content: `抓取成功（图 ${r.images} · 视频 ${r.videos}）`, key: `rf${id}` });
        await Promise.all([
          qc.invalidateQueries({ queryKey: ["archive"] }),
          qc.invalidateQueries({ queryKey: ["article-view", id] }),
        ]);
      }
      else message.error({ content: r.error || "抓取失败", key: `rf${id}` });
    } catch { message.error({ content: "请求失败", key: `rf${id}` }); }
  }

  const columns = [
    { title: "标题", render: (_: any, a: Article) => (
      <Space size={6}>
        <a href={a.url} target="_blank" rel="noopener">{a.title}</a>
        {a.has_video && <Tag color="purple">🎬 视频</Tag>}
      </Space>
    ) },
    { title: "主题", dataIndex: "topic", width: 110 },
    { title: "来源", dataIndex: "source_name", width: 140 },
    { title: "发布时间", width: 110, render: (_: any, a: Article) => a.published_at ? a.published_at.slice(0, 10) : <Text type="secondary">—</Text> },
    { title: "抓取时间", width: 110, render: (_: any, a: Article) => a.fetched_at?.slice(0, 10) },
    { title: "转写", width: 300, render: (_: any, a: Article) => {
      const draftId = draftMap[String(a.id)];
      const rewriteEntry = entries.find((entry) => entry.article_id === a.id);
      const isRewriting = Boolean(rewriteEntry && !rewriteEntry.done);
      return (
        <Space wrap>
          <Button size="small" onClick={() => setViewId(a.id)}>查看原文</Button>
          <Button size="small" onClick={() => refetch(a.id)}>重新抓取</Button>
          {draftId ? <>
            <Tag color="green">✓ 已转写</Tag>
            <Link to={`/drafts/${draftId}/edit?from=archive`}><Button size="small">查看草稿</Button></Link>
            <Button size="small" loading={isRewriting} disabled={isRewriting}
              onClick={() => rewrite(a, true)}>
              {isRewriting ? "重写中…" : "重写"}
            </Button>
          </> : <Button size="small" type="primary" loading={isRewriting}
            disabled={isRewriting} onClick={() => rewrite(a, false)}>
            {isRewriting ? "转写中…" : "转写"}
          </Button>}
        </Space>
      );
    } },
  ];

  const runningCount = entries.filter((e) => e.running).length;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Title level={2} style={{ margin: 0 }}>归档浏览</Title>

      {/* 主题筛选工具栏 */}
      <Space wrap>
        <Text strong>主题</Text>
        <Tag.CheckableTag checked={!topic} onChange={() => setParams({})}>全部</Tag.CheckableTag>
        {data.topics.map((t) => (
          <Tag.CheckableTag key={t} checked={topic === t} onChange={() => setParams({ topic: t })}>{t}</Tag.CheckableTag>
        ))}
      </Space>

      {/* 批量操作工具栏 */}
      <Space wrap style={{
        width: "100%", padding: "8px 12px", background: "#fafafa",
        border: "1px solid #f0f0f0", borderRadius: 8,
      }}>
        {selectedIds.length > 0 && <Tag color="blue">已选 {selectedIds.length}</Tag>}
        <Text>转写风格</Text>
        <Select size="small" style={{ width: 200 }} value={style} onChange={setStyle}
          options={[{ value: "", label: "默认（全局）" },
            ...(styleData?.styles || []).map((s) => ({ value: s.id, label: s.name + (s.is_builtin ? "" : "（自定义）") }))]} />
        <Button danger size="small" disabled={!selectedIds.length} onClick={batchDelete}>
          批量删除{selectedIds.length ? ` (${selectedIds.length})` : ""}
        </Button>
      </Space>

      {data.groups.length ? (
        <>
          {/* 未展开的日期不渲染表格，几千条归档也只画当前这一天。 */}
          <Collapse destroyInactivePanel defaultActiveKey={data.groups[0]?.date}
            items={data.groups.map((g) => ({
              key: g.date,
              label: <Space><b>{g.date}</b><Badge count={g.articles.length} color="#07C160" /></Space>,
              children: <Table rowKey="id" size="small" pagination={false}
                dataSource={g.articles} columns={columns}
                rowSelection={{
                  selectedRowKeys: selectedIds.filter((id) => g.articles.some((a) => a.id === id)),
                  onChange: (keys) => {
                    const groupIds = new Set(g.articles.map((a) => a.id));
                    setSelectedIds((prev) => [...prev.filter((id) => !groupIds.has(id)), ...(keys as number[])]);
                  },
                }} />,
            }))} />
          <Space>
            <Text type="secondary">已加载 {data.shown} / {data.total} 篇</Text>
            {hasNextPage && (
              <Button size="small" loading={isFetchingNextPage}
                onClick={() => fetchNextPage()}>加载更多</Button>
            )}
          </Space>
        </>
      ) : (
        <Text type="secondary">{isLoading ? "正在加载…" : "暂无文章。"}</Text>
      )}

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
