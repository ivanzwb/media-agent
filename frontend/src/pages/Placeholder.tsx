import { Card, Typography } from "antd";

export default function Placeholder({ title }: { title: string }) {
  return (
    <Card>
      <Typography.Title level={3}>{title}</Typography.Title>
      <Typography.Paragraph type="secondary">
        此页面正在迁移到新的 React 前端中，即将上线。
      </Typography.Paragraph>
    </Card>
  );
}
