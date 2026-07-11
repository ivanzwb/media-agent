import { Button, Space } from "antd";
import {
  PauseOutlined, CaretRightOutlined, StopOutlined, CloseOutlined,
} from "@ant-design/icons";
import { useRunStatus } from "../api/hooks";
import { postForm } from "../api/client";
import { useEffect, useState } from "react";

export default function RunPanel({
  open, onClose,
}: { open: boolean; onClose: () => void }) {
  const [poll, setPoll] = useState(true);
  const { data } = useRunStatus(open && poll);

  useEffect(() => {
    if (data && !data.running) setPoll(false);
  }, [data]);

  useEffect(() => {
    if (open) setPoll(true);
  }, [open]);

  if (!open || !data) return null;

  const st = data.stats || {};
  const statLine = [
    data.source_total ? `来源 ${data.source_current}/${data.source_total}` : "",
    `抓取 ${st.fetched || 0} · 归档 ${st.archived || 0} · 草稿 ${st.drafted || 0}`,
  ].filter(Boolean).join(" · ");

  const title = data.running
    ? (data.paused ? "⏸ 已暂停" : "运行中…")
    : data.stopped ? "运行已停止"
    : data.error ? `运行失败：${data.error}` : "运行完成";

  return (
    <div className="ma-run-panel">
      <div className="ma-run-head">
        <span>{title}</span>
        <Space size={4}>
          <span style={{ fontSize: 12, opacity: 0.8 }}>{statLine}</span>
          {data.running && !data.paused && (
            <Button size="small" icon={<PauseOutlined />}
              onClick={() => postForm("/run/pause").then(() => setPoll(true))}>暂停</Button>
          )}
          {data.running && data.paused && (
            <Button size="small" icon={<CaretRightOutlined />}
              onClick={() => postForm("/run/resume").then(() => setPoll(true))}>恢复</Button>
          )}
          {data.running && (
            <Button size="small" danger icon={<StopOutlined />}
              onClick={() => {
                if (confirm("确定要停止当前运行吗？")) postForm("/run/stop").then(() => setPoll(true));
              }}>停止</Button>
          )}
          <Button size="small" type="text" style={{ color: "#ddd" }}
            icon={<CloseOutlined />} onClick={onClose} />
        </Space>
      </div>
      <pre className="ma-run-logs">{(data.logs || []).join("\n")}</pre>
    </div>
  );
}
