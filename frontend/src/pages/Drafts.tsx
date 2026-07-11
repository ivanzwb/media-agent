import { useQuery } from "@tanstack/react-query";
import { Table, Tag, Button, Space, Typography } from "antd";
import { Link, useSearchParams } from "react-router-dom";
import { getJson } from "../api/client";

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
  const { data } = useQuery({
    queryKey: ["drafts", status],
    queryFn: () => getJson<{ drafts: Draft[]; statuses: string[] }>(
      "/api/drafts", status ? { status } : undefined),
  });
  const statuses = data?.statuses || [];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Title level={2} style={{ margin: 0 }}>草稿</Title>
      <Space wrap>
        <Tag.CheckableTag checked={!status} onChange={() => setParams({})}>全部</Tag.CheckableTag>
        {statuses.map((s) => (
          <Tag.CheckableTag key={s} checked={status === s} onChange={() => setParams({ status: s })}>{s}</Tag.CheckableTag>
        ))}
      </Space>
      <Table rowKey="id" size="small" dataSource={data?.drafts || []}
        pagination={{ pageSize: 30, hideOnSinglePage: true }}
        locale={{ emptyText: "暂无草稿。" }}
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
