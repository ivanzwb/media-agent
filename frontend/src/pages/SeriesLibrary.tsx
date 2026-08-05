import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App as AntApp, Button, Card, Empty, Input, Progress, Space, Table,
  Tag, Typography,
} from "antd";
import { BookOutlined, DeleteOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMemo, useState } from "react";
import { Link, useNavigate, useOutletContext } from "react-router-dom";
import { api, getJson } from "../api/client";
import { useLicense } from "../api/hooks";
import {
  CHAPTER_STATUS, SERIES_STATUS, Series, SeriesChapter, exportUrl,
} from "../api/series";
import KnowledgeMap from "../components/KnowledgeMap";

const { Title, Paragraph, Text } = Typography;

function isBusy(series: Series[]) {
  return series.some((item) => item.status === "running");
}

/** Everything that has been planned or written, after the run page forgets it.
 *
 * The creation page only ever shows the run in front of you; a series is a
 * week's worth of reading that people come back to, so the whole shelf —
 * exports, unfinished chapters, the map of each — lives here instead.
 */
export default function SeriesLibrary() {
  const { message, modal } = AntApp.useApp();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { data: license } = useLicense();
  const { openLicenseGuide } = useOutletContext<{ openLicenseGuide: () => void }>();
  const [keyword, setKeyword] = useState("");
  const [retrying, setRetrying] = useState<number | null>(null);
  const [starting, setStarting] = useState<number | null>(null);
  const [deleting, setDeleting] = useState<number | null>(null);

  const isPro = !!(license?.active || license?.dev);
  const listQuery = useQuery({
    queryKey: ["series-list"],
    queryFn: () => getJson<{ series: Series[] }>("/api/series"),
    retry: false,
    refetchInterval: (query) => (
      isBusy(query.state.data?.series || []) ? 5000 : false),
  });
  const all = listQuery.data?.series || [];
  const running = isBusy(all);

  const rows = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    if (!needle) return all;
    return all.filter((item) => (
      item.title.toLowerCase().includes(needle)
      || item.topic.toLowerCase().includes(needle)));
  }, [all, keyword]);

  async function retryChapter(seriesId: number, chapterId: number) {
    if (!isPro) { openLicenseGuide(); return; }
    setRetrying(chapterId);
    try {
      await api.post(`/api/series/${seriesId}/chapters/${chapterId}/retry`, {});
      message.success("已开始重跑这一章");
      await qc.invalidateQueries({ queryKey: ["series-status"] });
      await qc.invalidateQueries({ queryKey: ["series-list"] });
    } catch (e: any) {
      if (e?.response?.status === 403) openLicenseGuide();
      message.error(e?.response?.data?.error || e?.response?.data?.detail || "重跑失败");
    } finally {
      setRetrying(null);
    }
  }

  async function startWriting(series: Series) {
    setStarting(series.id);
    try {
      await api.post(`/api/series/${series.id}/run`, {});
      message.success("已开始逐章写作，可以在系列创作页看进度");
      await qc.invalidateQueries({ queryKey: ["series-status"] });
      await qc.invalidateQueries({ queryKey: ["series-list"] });
      navigate("/series-create");
    } catch (e: any) {
      if (e?.response?.status === 403) openLicenseGuide();
      message.error(e?.response?.data?.error || e?.response?.data?.detail || "启动失败");
    } finally {
      setStarting(null);
    }
  }

  function confirmWriting(series: Series) {
    if (!isPro) { openLicenseGuide(); return; }
    const pending = series.chapters.filter(
      (chapter) => !chapter.draft_id).length;
    modal.confirm({
      title: `开始写《${series.title}》剩下的 ${pending} 章？`,
      icon: null,
      okText: "开始写作",
      cancelText: "再想想",
      content: (
        <Space direction="vertical" size={4} style={{ marginTop: 8 }}>
          <Text>预计耗时 {pending * 3}–{pending * 6} 分钟，期间无法同时进行搜索创作。</Text>
          <Text type="secondary">已写完的章节不会重写。</Text>
        </Space>
      ),
      onOk: () => startWriting(series),
    });
  }

  async function deleteSeries(series: Series) {
    setDeleting(series.id);
    try {
      await api.delete(`/api/series/${series.id}`);
      message.success("已删除系列及其章节草稿");
      await qc.invalidateQueries({ queryKey: ["series-status"] });
      await qc.invalidateQueries({ queryKey: ["series-list"] });
    } catch (e: any) {
      if (e?.response?.status === 403) openLicenseGuide();
      message.error(e?.response?.data?.error || e?.response?.data?.detail || "删除失败");
    } finally {
      setDeleting(null);
    }
  }

  function confirmDelete(series: Series) {
    const draftCount = series.chapters.filter(
      (chapter) => chapter.draft_id).length;
    modal.confirm({
      title: `删除系列《${series.title}》？`,
      icon: null,
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      content: (
        <Space direction="vertical" size={4} style={{ marginTop: 8 }}>
          <Text>该系列及其 {draftCount} 篇章节草稿将被删除，此操作不可恢复。</Text>
          <Text type="secondary">已生成的视频产物文件不受影响。</Text>
        </Space>
      ),
      onOk: () => deleteSeries(series),
    });
  }

  const columns = [
    {
      title: "系列",
      render: (_: unknown, item: Series) => (
        <Space direction="vertical" size={0}>
          <Text strong>{item.title}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{item.topic}</Text>
        </Space>
      ),
    },
    {
      title: "状态", width: 110,
      render: (_: unknown, item: Series) => (
        <Tag color={SERIES_STATUS[item.status]?.color || "default"}>
          {SERIES_STATUS[item.status]?.label || item.status}
        </Tag>
      ),
    },
    {
      title: "进度", width: 180,
      render: (_: unknown, item: Series) => (
        <Space direction="vertical" size={2} style={{ width: "100%" }}>
          <Progress percent={item.parts
            ? Math.round((item.chapters_done / item.parts) * 100) : 0}
            size="small" showInfo={false}
            status={item.chapters_failed > 0 ? "exception" : undefined} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            {item.chapters_done}/{item.parts} 章
            {item.chapters_failed > 0 ? ` · ${item.chapters_failed} 章未完成` : ""}
          </Text>
        </Space>
      ),
    },
    {
      title: "创建时间", width: 140,
      render: (_: unknown, item: Series) => (
        <Text type="secondary">
          {(item.created_at || "").slice(0, 16).replace("T", " ")}
        </Text>
      ),
    },
    {
      title: "操作", width: 320,
      render: (_: unknown, item: Series) => (
        <Space wrap size={4}>
          {item.chapters_done > 0 && (
            <Button size="small" href={exportUrl(item.id, "md")}>Markdown</Button>
          )}
          {item.chapters_done > 0 && (
            <Button size="small" href={exportUrl(item.id, "html")}>网页</Button>
          )}
          {item.chapters.some((chapter) => !chapter.draft_id)
            && item.status !== "running" && (
              <Button size="small" type="link" icon={<BookOutlined />}
                loading={starting === item.id} disabled={running}
                onClick={() => confirmWriting(item)}>
                {item.status === "planned" ? "开始写作" : "写完剩下的"}
              </Button>
            )}
          <Button size="small" danger icon={<DeleteOutlined />}
            loading={deleting === item.id}
            disabled={running || item.status === "running"}
            onClick={() => confirmDelete(item)}>删除</Button>
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <div>
        <Space align="center">
          <Title level={2} style={{ margin: 0 }}>系列管理</Title>
          <Tag color="gold">Pro</Tag>
        </Space>
        <Paragraph type="secondary" style={{ margin: "8px 0 0" }}>
          已经规划或写成的知识系列都在这里：展开可以看这个领域的知识架构、
          逐章的状态，以及把整个系列导出成一份文档。
        </Paragraph>
      </div>

      {!isPro && license && (
        <Alert type="warning" showIcon message="系列创作为 Pro 功能"
          description={<Button type="link" style={{ padding: 0 }}
            onClick={openLicenseGuide}>查看 Pro 功能并升级</Button>} />
      )}

      <Card>
        <Space style={{ marginBottom: 16 }} wrap>
          <Input.Search allowClear style={{ width: 280 }}
            placeholder="按标题或主题筛选" value={keyword}
            onChange={(e) => setKeyword(e.target.value)} />
          <Button icon={<ReloadOutlined />} loading={listQuery.isFetching}
            onClick={() => listQuery.refetch()}>刷新</Button>
          <Link to="/series-create">
            <Button type="primary" icon={<BookOutlined />}>新建系列</Button>
          </Link>
          {running && <Tag color="processing">有系列正在运行</Tag>}
        </Space>

        <Table<Series> rowKey="id" size="small" columns={columns}
          dataSource={rows} loading={listQuery.isLoading}
          pagination={rows.length > 20 ? { pageSize: 20 } : false}
          locale={{
            emptyText: <Empty description={
              all.length ? "没有匹配的系列" : "还没有系列，先去创建一个"} />,
          }}
          expandable={{
            expandedRowRender: (item) => (
              <SeriesDetail series={item} busy={running} retrying={retrying}
                onRetry={(chapterId) => retryChapter(item.id, chapterId)} />
            ),
          }} />
      </Card>
    </Space>
  );
}

function SeriesDetail({ series, busy, retrying, onRetry }: {
  series: Series;
  busy: boolean;
  retrying: number | null;
  onRetry: (chapterId: number) => void;
}) {
  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {series.error && (
        <Alert type="error" showIcon message={series.error} />
      )}
      {series.knowledge_map && (
        <Card size="small" title="知识架构">
          <KnowledgeMap source={series.knowledge_map} />
        </Card>
      )}
      <Card size="small" title="章节">
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          {series.chapters.map((chapter: SeriesChapter) => {
            const meta = CHAPTER_STATUS[chapter.status] || CHAPTER_STATUS.pending;
            return (
              <div key={chapter.id}>
                <Space wrap size={8}>
                  <Text type="secondary">第 {chapter.order} 章</Text>
                  {chapter.draft_id ? (
                    <Link to={`/drafts/${chapter.draft_id}/edit`}>{chapter.title}</Link>
                  ) : <Text>{chapter.title}</Text>}
                  <Tag color={meta.color}>{meta.label}</Tag>
                  {chapter.prerequisites.length > 0 && (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      需先读：{chapter.prerequisites.join("、")}
                    </Text>
                  )}
                  {!chapter.draft_id && series.status !== "planned" && (
                    <Button type="link" size="small" disabled={busy}
                      loading={retrying === chapter.id}
                      onClick={() => onRetry(chapter.id)}>重跑本章</Button>
                  )}
                </Space>
                {(chapter.error || chapter.scope) && (
                  <div>
                    <Text type={chapter.error ? "danger" : "secondary"}
                      style={{ fontSize: 12 }}>
                      {chapter.error || chapter.scope}
                    </Text>
                  </div>
                )}
              </div>
            );
          })}
        </Space>
      </Card>
    </Space>
  );
}
