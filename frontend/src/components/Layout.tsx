import { Layout as AntLayout, Menu, Button, Tag, Modal } from "antd";
import { PlayCircleOutlined, SearchOutlined } from "@ant-design/icons";
import { Link, useLocation, useNavigate, Outlet } from "react-router-dom";
import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useLicense, useLocalState } from "../api/hooks";
import { getJson, postForm } from "../api/client";
import RunPanel from "./RunPanel";

const { Header, Content } = AntLayout;

const NAV = [
  { key: "/", label: "仪表盘" },
  { key: "/archive", label: "归档" },
  { key: "/drafts", label: "草稿" },
  { key: "/sources", label: "来源" },
  { key: "/settings", label: "设置" },
];

function selectedKey(pathname: string): string {
  if (pathname === "/") return "/";
  const hit = NAV.find((n) => n.key !== "/" && pathname.startsWith(n.key));
  return hit ? hit.key : "/";
}

export default function Layout() {
  const location = useLocation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { data: lic } = useLicense();
  const [runOpen, setRunOpen] = useLocalState<boolean>("layout-runOpen", false);
  const [runNonce, setRunNonce] = useState(0);
  const [guideOpen, setGuideOpen] = useState(false);

  // On mount, if panel was open (from localStorage), check if run is still active
  useEffect(() => {
    if (!runOpen) return;
    getJson<{ running: boolean; stopped: boolean }>("/api/run-status")
      .then((s) => { if (!s.running && !s.stopped) setRunOpen(false); })
      .catch(() => {});
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const licTag = lic?.dev
    ? <Tag color="blue">DEV</Tag>
    : lic?.active
    ? <Tag color="green">Pro 版</Tag>
    : <Tag>免费版</Tag>;

  async function startRun() {
    setRunOpen(true);
    try {
      await postForm<{ started?: boolean; message?: string }>("/run");
    } catch { /* poll reflects state */ }
    // Restart polling AFTER the run is registered, so RunPanel resumes even if
    // it was already open (e.g. still showing "运行完成" from a previous run),
    // and force an immediate refresh so the running log box appears right away
    // instead of waiting for an incidental refetch.
    setRunNonce((n) => n + 1);
    qc.invalidateQueries({ queryKey: ["run-status"] });
  }

  function openSearchCreate() {
    if (lic?.active || lic?.dev) navigate("/search-create");
    else setGuideOpen(true);
  }

  return (
    <AntLayout style={{ minHeight: "100vh" }}>
      <Header style={{ display: "flex", alignItems: "center", gap: 16,
        background: "#fff", borderBottom: "1px solid #eee", position: "sticky",
        top: 0, zIndex: 10, paddingInline: 20 }}>
        <Link to="/" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <img src="/static/logo.svg" alt="" style={{ height: 26 }} />
          <span style={{ fontWeight: 700, color: "#222", fontSize: 16 }}>Media Agent</span>
        </Link>
        <Menu mode="horizontal" selectedKeys={[selectedKey(location.pathname)]}
          style={{ flex: 1, borderBottom: "none" }}
          onClick={(e) => navigate(e.key)}
          items={NAV.map((n) => ({ key: n.key, label: n.label }))} />
        <Link to="/settings" title="授权状态">{licTag}</Link>
        <Button icon={<SearchOutlined />} onClick={openSearchCreate}>
          搜索创作 <Tag color="gold" bordered={false} style={{ marginInlineEnd: 0 }}>Pro</Tag>
        </Button>
        <Button type="primary" icon={<PlayCircleOutlined />} onClick={startRun}>
          立即运行
        </Button>
      </Header>

      <Content>
        <div className="ma-content">
          <Outlet context={{ openLicenseGuide: () => setGuideOpen(true) }} />
        </div>
      </Content>

      <RunPanel open={runOpen} trigger={runNonce} onClose={() => setRunOpen(false)} />

      <Modal open={guideOpen} onCancel={() => setGuideOpen(false)}
        title="升级 Pro 版，解锁全部功能"
        okText="去激活" cancelText="稍后再说"
        onOk={() => { setGuideOpen(false); navigate("/settings"); }}>
        <p style={{ color: "#888" }}>当前为免费版。免费版可用：RSS/网页抓取、去重归档、
          主题分类、手动添加来源、编辑器排版组件 / 模板、本地化媒体、
          kitten 配音、每日 1 篇 LLM 改写（含套用模板重写）。</p>
        <p>Pro 版额外解锁：</p>
        <ul style={{ lineHeight: 2, color: "#555" }}>
          <li>无限 LLM 改写（含套用模板重写文章）</li>
          <li>搜索多方资料并生成带引用的主题文章</li>
          <li>AI 智能推荐与一键 / 批量发现来源</li>
          <li>讲解视频生成（含数字人主播）</li>
          <li>平台同步 / 一键发布（公众号 / 头条 / 视频号）</li>
          <li>声音复刻配音</li>
          <li>定时调度</li>
        </ul>
      </Modal>
    </AntLayout>
  );
}
