import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { App as AntApp, Table, Tag, Button, Space, Typography } from "antd";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, getJson } from "../api/client";

const { Title } = Typography;

interface Draft {
  id: number; status: string; title_cn: string | null;
  display_title: string | null; score: number | null;
  draft_filename: string; draft_path: string;
  article_published_at: string | null; updated_at: string;
}

const STATUS_COLOR: Record<string, string> = {
  drafted: "default", reviewing: "processing",
  approved: "success", published: "green",
};

export default function Drafts() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") || "";
  const qc = useQueryClient();
  const { message, modal } = AntApp.useApp();
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [page, setPage] = useState(1);
  const pageSize = 30;
  // Reset to page 1 when the status filter changes.
  useEffect(() => { setPage(1); }, [status]);
  // Lazy loading: only the current page is fetched (and read from disk on the
  // backend), so the 草稿 page opens fast even with a very long list.
  const { data, isFetching } = useQuery({
    queryKey: ["drafts", status, page],
    queryFn: () => getJson<{ drafts: Draft[]; total: number; statuses: string[] }>(
      "/api/drafts", {
        ...(status ? { status } : {}),
        limit: pageSize, offset: (page - 1) * pageSize,
      }),
    placeholderData: keepPreviousData,
  });
  const statuses = data?.statuses || [];

  function batchDelete() {
    if (!selectedIds.length) return;
    modal.confirm({
      title: `确定删除选中的 ${selectedIds.length} 篇草稿？`,
      content: "将同时删除草稿文件，且不可恢复。",
      okText: "删除", okType: "danger", cancelText: "取消",
      onOk: async () => {
        try {
          const r = await api.post<{ ok: boolean; deleted: number }>(
            "/api/drafts/delete", { ids: selectedIds });
          message.success(`已删除 ${r.data.deleted} 篇`);
          setSelectedIds([]);
          qc.invalidateQueries({ queryKey: ["drafts"] });
        } catch { message.error("删除失败"); }
      },
    });
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Title level={2} style={{ margin: 0 }}>草稿</Title>
      <Space wrap>
        <Tag.CheckableTag checked={!status} onChange={() => { setSelectedIds([]); setParams({}); }}>全部</Tag.CheckableTag>
        {statuses.map((s) => (
          <Tag.CheckableTag key={s} checked={status === s} onChange={() => { setSelectedIds([]); setParams({ status: s }); }}>{s}</Tag.CheckableTag>
        ))}
      </Space>
      <Space wrap style={{
        width: "100%", padding: "8px 12px", background: "#fafafa",
        border: "1px solid #f0f0f0", borderRadius: 8,
      }}>
        {selectedIds.length > 0 && <Tag color="blue">已选 {selectedIds.length}</Tag>}
        <Button danger size="small" disabled={!selectedIds.length} onClick={batchDelete}>
          批量删除{selectedIds.length ? ` (${selectedIds.length})` : ""}
        </Button>
        {selectedIds.length > 0 && (
          <Button size="small" onClick={() => setSelectedIds([])}>取消选择</Button>
        )}
      </Space>
      <Table rowKey="id" size="small" dataSource={data?.drafts || []}
        loading={isFetching}
        pagination={{
          current: page, pageSize, total: data?.total || 0,
          showSizeChanger: false, hideOnSinglePage: true,
          showTotal: (t) => `共 ${t} 篇`,
          onChange: (p) => setPage(p),
        }}
        locale={{ emptyText: "暂无草稿。" }}
        rowSelection={{
          selectedRowKeys: selectedIds,
          preserveSelectedRowKeys: true,
          onChange: (keys) => setSelectedIds(keys as number[]),
        }}
        columns={[
          { title: "#", dataIndex: "id", width: 60 },
          { title: "状态", dataIndex: "status", width: 100,
            render: (v) => <Tag color={STATUS_COLOR[v] || "default"}>{v}</Tag> },
          { title: "中文标题", render: (_, d) => d.title_cn || d.display_title || "—" },
          { title: "评分", dataIndex: "score", width: 80, render: (v) =>
            v == null ? <span style={{ color: "#999" }}>—</span>
            : <Tag color={v >= 80 ? "green" : v >= 50 ? "gold" : "red"}>{Math.round(v)}</Tag> },
          { title: "文件", dataIndex: "draft_filename", ellipsis: true },
          { title: "发布时间", dataIndex: "article_published_at", width: 120, render: (v) => v || "—" },
          { title: "更新时间", dataIndex: "updated_at", width: 140 },
          { title: "", width: 80, render: (_, d) => (
            <Link to={`/drafts/${d.id}/edit`}><Button size="small">编辑</Button></Link>) },
        ]}
      />
    </Space>
  );
}
