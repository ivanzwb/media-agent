import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Card, Col, Row, Statistic, Button, Table, List, Typography, Space,
  Progress, Empty, App as AntApp,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { Link } from "react-router-dom";
import { getJson, postForm } from "../api/client";
import { useState } from "react";

interface HotnessItem { name: string; score: number; recent: number; total: number; }
interface DashboardData {
  stats: { articles: number; drafts: number; runs: number };
  runs: { started_at: string; status: string; stats_json: string }[];
  drafts: { id: number; status: string; draft_path: string }[];
  hotness: { topics: HotnessItem[]; sources: HotnessItem[] } | null;
  hotness_updated: string | null;
}

export default function Dashboard() {
  const qc = useQueryClient();
  const { message, modal } = AntApp.useApp();
  const [refreshing, setRefreshing] = useState(false);
  const { data } = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => getJson<DashboardData>("/api/dashboard"),
  });

  async function refreshHotness() {
    setRefreshing(true);
    try {
      await postForm("/api/refresh-hotness");
      await qc.invalidateQueries({ queryKey: ["dashboard"] });
    } finally { setRefreshing(false); }
  }

  function clearData(url: string, label: string) {
    modal.confirm({
      title: `确定要${label}吗？`,
      content: "此操作不可撤销。",
      okType: "danger",
      onOk: async () => {
        await postForm(url);
        await qc.invalidateQueries({ queryKey: ["dashboard"] });
        message.success(`已${label}`);
      },
    });
  }

  const s = data?.stats;
  const hot = data?.hotness;

  const hotCol = (items: HotnessItem[] | undefined, title: string) => (
    <Card size="small" title={title}>
      <List
        size="small"
        dataSource={(items || []).slice(0, 10)}
        renderItem={(it, i) => (
          <List.Item>
            <Space style={{ width: "100%" }} align="center">
              <span style={{ width: 20, color: "#999" }}>{i + 1}</span>
              <span style={{ width: 140, fontWeight: 500 }}>{it.name}</span>
              <Progress percent={Math.min(100, it.score)} showInfo={false}
                style={{ width: 120 }} strokeColor="#07C160" />
              <span style={{ width: 40 }}>{it.score}</span>
              <span style={{ color: "#999" }}>{it.recent}篇/周 · {it.total}篇</span>
            </Space>
          </List.Item>
        )}
      />
    </Card>
  );

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Typography.Title level={2} style={{ margin: 0 }}>仪表盘</Typography.Title>

      <Row gutter={16}>
        <Col span={8}><Card><Statistic title="已归档文章" value={s?.articles ?? 0} /></Card></Col>
        <Col span={8}><Card><Statistic title="草稿" value={s?.drafts ?? 0} /></Card></Col>
        <Col span={8}><Card><Statistic title="运行次数" value={s?.runs ?? 0} /></Card></Col>
      </Row>

      <Space>
        <Button danger onClick={() => clearData("/clear/articles", "清空归档")}>清空归档</Button>
        <Button danger onClick={() => clearData("/clear/drafts", "清空草稿")}>清空草稿</Button>
        <Button danger onClick={() => clearData("/clear/runs", "清空运行记录")}>清空运行记录</Button>
      </Space>

      <div>
        <Space style={{ marginBottom: 12 }}>
          <Typography.Title level={4} style={{ margin: 0 }}>热度排名</Typography.Title>
          <Button type="primary" size="small" icon={<ReloadOutlined />}
            loading={refreshing} onClick={refreshHotness}>刷新热度</Button>
          {data?.hotness_updated && (
            <Typography.Text type="secondary">
              上次更新：{data.hotness_updated.slice(0, 19).replace("T", " ")}
            </Typography.Text>
          )}
        </Space>
        {hot ? (
          <Row gutter={16}>
            <Col span={12}>{hotCol(hot.topics, "🔥 主题热度")}</Col>
            <Col span={12}>{hotCol(hot.sources, "🔥 来源热度")}</Col>
          </Row>
        ) : <Empty description="还没有热度数据，点击「刷新热度」" />}
      </div>

      <div>
        <Typography.Title level={4}>最近运行</Typography.Title>
        <Table
          size="small" rowKey={(_, i) => String(i)}
          pagination={false}
          locale={{ emptyText: '还没有运行记录。点击右上角"立即运行"。' }}
          dataSource={data?.runs || []}
          columns={[
            { title: "开始时间", dataIndex: "started_at" },
            { title: "状态", dataIndex: "status" },
            { title: "统计", dataIndex: "stats_json",
              render: (v) => <code>{v}</code> },
          ]}
        />
      </div>

      <div>
        <Typography.Title level={4}>最新草稿</Typography.Title>
        {data?.drafts?.length ? (
          <List
            size="small"
            dataSource={data.drafts}
            renderItem={(d) => (
              <List.Item>
                <Link to={`/drafts/${d.id}/edit`}>#{d.id} · {d.status} · {d.draft_path}</Link>
              </List.Item>
            )}
          />
        ) : <Typography.Text type="secondary">还没有草稿。</Typography.Text>}
      </div>
    </Space>
  );
}
