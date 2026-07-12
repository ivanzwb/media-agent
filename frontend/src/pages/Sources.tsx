import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App as AntApp, Button, Card, Checkbox, Input, Modal, Select, Space, Table,
  Tag, Typography, Form, InputNumber, Divider, Tooltip,
} from "antd";
import { ReloadOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { useState } from "react";
import { api, getJson, postForm } from "../api/client";

const { Title, Text, Paragraph } = Typography;

interface Topic { name: string; keywords: string[]; }
interface Source {
  name: string; type: string; url: string; topics: string[]; mode: string;
  include_pattern: string | null; exclude_pattern: string | null;
  max_pages: number; render_js: boolean; enabled: boolean;
}
interface Feeds { topics: Topic[]; sources: Source[]; }
interface Candidate { name: string; url: string; }

export default function Sources() {
  const qc = useQueryClient();
  const { message } = AntApp.useApp();
  const { data } = useQuery({ queryKey: ["feeds"], queryFn: () => getJson<Feeds>("/api/feeds") });
  const refetch = () => qc.invalidateQueries({ queryKey: ["feeds"] });

  // smart recommendation state
  const [themes, setThemes] = useState("");
  const [recoStatus, setRecoStatus] = useState("");
  const [subtopics, setSubtopics] = useState<string[]>([]);
  const [groups, setGroups] = useState<{ name: string; keywords: { kw: string; on: boolean }[] }[]>([]);
  const [autoBusy, setAutoBusy] = useState(false);

  // topic discover state: topicName -> {candidates, selected}
  const [discover, setDiscover] = useState<Record<string, { cands: Candidate[]; sel: Set<string>; status: string }>>({});

  // selections for batch
  const [selTopics, setSelTopics] = useState<string[]>([]);
  const [selSources, setSelSources] = useState<string[]>([]);

  // edit modals
  const [topicEdit, setTopicEdit] = useState<Topic | null>(null);
  const [sourceEdit, setSourceEdit] = useState<Source | null>(null);

  const topics = data?.topics || [];
  const sources = data?.sources || [];

  // ── smart recommendation ──
  async function recoSubtopics() {
    if (!themes.trim()) { setRecoStatus("请先输入主题"); return; }
    setRecoStatus("推荐中…");
    try {
      const r = await postForm<{ subtopics: string[] }>("/sources/topics/suggest", { themes });
      setSubtopics(r.subtopics || []);
      setGroups([]);
      setRecoStatus(r.subtopics?.length ? "" : "没有推荐结果");
    } catch { setRecoStatus("推荐失败"); }
  }

  async function toggleSubtopic(st: string) {
    const exists = groups.find((g) => g.name === st);
    if (exists) { setGroups(groups.filter((g) => g.name !== st)); return; }
    setGroups((g) => [...g, { name: st, keywords: [] }]);
    try {
      const r = await postForm<{ keywords: string[] }>("/sources/topics/keywords", { subtopic: st });
      setGroups((g) => g.map((x) => x.name === st
        ? { ...x, keywords: (r.keywords || []).map((kw) => ({ kw, on: true })) } : x));
    } catch { /* ignore */ }
  }

  async function addTopic(name: string, keywords: string) {
    await postForm("/sources/topics/add", { name, keywords });
    message.success(`已添加主题：${name}`);
    refetch();
  }

  async function batchAddTopics() {
    for (const g of groups) {
      const kws = g.keywords.filter((k) => k.on).map((k) => k.kw).join(", ");
      try { await postForm("/sources/topics/add", { name: g.name, keywords: kws }); } catch { /* skip */ }
    }
    message.success("批量添加完成");
    setGroups([]); setSubtopics([]);
    refetch();
  }

  async function autoDiscoverAll() {
    if (!themes.trim()) { setRecoStatus("请先输入主题"); return; }
    setAutoBusy(true);
    const set = (m: string) => setRecoStatus(m);
    try {
      set("① 推荐子主题…");
      const sub = await postForm<{ subtopics: string[] }>("/sources/topics/suggest", { themes });
      const sts = sub.subtopics || [];
      if (!sts.length) { set("没有推荐出子主题"); return; }
      set("② 获取关键词…");
      const kwRes = await Promise.all(sts.map(async (st) => {
        const d = await postForm<{ keywords: string[] }>("/sources/topics/keywords", { subtopic: st });
        return { name: st, keywords: d.keywords || [] };
      }));
      set("③ 批量加为主题…");
      const added: string[] = [];
      for (const it of kwRes) {
        try { await postForm("/sources/topics/add", { name: it.name, keywords: it.keywords.join(", ") }); added.push(it.name); } catch { /* skip */ }
      }
      if (!added.length) { set("没有新增主题"); refetch(); return; }
      set(`④ 发现来源…`);
      const allCands: { name: string; url: string; topics: string[] }[] = [];
      const seen = new Set<string>();
      for (const t of added) {
        try {
          const d = await postForm<{ candidates: Candidate[] }>("/sources/suggest-sources", { topic: t });
          (d.candidates || []).forEach((c) => { if (!seen.has(c.url)) { seen.add(c.url); allCands.push({ name: c.name, url: c.url, topics: [t] }); } });
        } catch { /* skip */ }
      }
      if (!allCands.length) { set("④ 没发现来源，已完成主题添加"); refetch(); return; }
      set("⑤ 添加来源…");
      const r = await api.post("/sources/batch-discover-add", { items: allCands });
      set(`✓ 完成：新增 ${r.data.added}/${r.data.total} 个主题+来源`);
    } catch (e: any) {
      set("✗ 流程出错：" + (e?.message || "未知错误"));
    } finally {
      setAutoBusy(false);
      refetch();
    }
  }

  // ── topic discover sources ──
  async function topicDiscover(topic: string) {
    setDiscover((d) => ({ ...d, [topic]: { cands: [], sel: new Set(), status: "推荐中…" } }));
    try {
      const r = await postForm<{ candidates: Candidate[] }>("/sources/suggest-sources", { topic });
      const cands = r.candidates || [];
      setDiscover((d) => ({ ...d, [topic]: { cands, sel: new Set(cands.map((c) => c.url)), status: cands.length ? "" : "没有推荐结果" } }));
    } catch {
      setDiscover((d) => ({ ...d, [topic]: { cands: [], sel: new Set(), status: "推荐失败" } }));
    }
  }
  async function topicAddSources(topic: string) {
    const st = discover[topic];
    if (!st) return;
    const urls = [...st.sel];
    if (!urls.length) { message.warning("请先选择来源"); return; }
    const items = st.cands.filter((c) => st.sel.has(c.url)).map((c) => ({ name: c.name, url: c.url, topics: [topic] }));
    const r = await api.post("/sources/batch-discover-add", { items });
    message.success(`新增 ${r.data.added}/${r.data.total} 个来源`);
    setDiscover((d) => { const n = { ...d }; delete n[topic]; return n; });
    refetch();
  }

  // ── manual discover ──
  const [discUrl, setDiscUrl] = useState(""); const [discTopic, setDiscTopic] = useState("");
  const [searchKw, setSearchKw] = useState(""); const [searchTopic, setSearchTopic] = useState("");
  const [batchText, setBatchText] = useState("");

  async function doDiscover() {
    if (!discUrl.trim()) return;
    await postForm("/sources/discover", { url: discUrl, topics: discTopic });
    message.success("已从网址发现"); setDiscUrl(""); refetch();
  }
  async function doSearch() {
    if (!searchKw.trim()) return;
    await postForm("/sources/search", { keyword: searchKw, topics: searchTopic });
    message.success("已按关键词发现"); setSearchKw(""); refetch();
  }
  async function doBatchAdd() {
    const items = batchText.split("\n").filter(Boolean).map((line) => {
      const p = line.split("|").map((s) => s.trim());
      if (!p[1]) return null;
      return { name: p[0] || "", url: p[1], type: (p[2] || "rss").toLowerCase(),
        topics: p[3] ? p[3].split(",").map((s) => s.trim()).filter(Boolean) : [],
        mode: (p[4] || "single").toLowerCase() };
    }).filter(Boolean);
    if (!items.length) { message.warning("没有解析出有效来源"); return; }
    const r = await api.post("/sources/batch-add", { items });
    message.success(`新增 ${r.data.added}/${r.data.total} 个来源`);
    setBatchText(""); refetch();
  }

  // ── row actions ──
  async function toggleSource(url: string) { await postForm("/sources/toggle", { url }); refetch(); }
  async function deleteSource(url: string, name: string) {
    Modal.confirm({ title: `确定删除来源「${name}」？`, okType: "danger",
      onOk: async () => { await postForm("/sources/delete", { url }); refetch(); } });
  }
  async function deleteTopic(name: string) {
    Modal.confirm({ title: `确定删除主题「${name}」？`, okType: "danger",
      onOk: async () => { await postForm("/sources/topics/delete", { name }); refetch(); } });
  }
  async function batchDeleteTopics() {
    if (!selTopics.length) return;
    Modal.confirm({ title: `确定删除选中的 ${selTopics.length} 个主题？`, okType: "danger",
      onOk: async () => { await api.post("/sources/topics/delete-batch", { items: selTopics }); setSelTopics([]); refetch(); } });
  }
  async function batchDeleteSources() {
    if (!selSources.length) return;
    Modal.confirm({ title: `确定删除选中的 ${selSources.length} 个来源？`, okType: "danger",
      onOk: async () => { await api.post("/sources/delete-batch", { items: selSources }); setSelSources([]); refetch(); } });
  }
  async function batchToggleSources(enabled: boolean) {
    if (!selSources.length) return;
    const r = await api.post("/sources/toggle-batch", { items: selSources, enabled });
    message.success(`已${enabled ? "启用" : "禁用"} ${r.data.updated} 个来源`);
    setSelSources([]); refetch();
  }
  const [checking, setChecking] = useState(false);
  async function checkAllReachability() {
    setChecking(true);
    try {
      const r = await api.post("/sources/check-reachability");
      const { total, disabled } = r.data;
      if (disabled === 0) {
        message.success(`全部 ${total} 个来源可达`);
      } else {
        message.warning(`${total} 个来源中 ${disabled} 个不可达，已禁用`);
      }
      refetch();
    } catch {
      message.error("检测失败");
    } finally {
      setChecking(false);
    }
  }
  const [fixing, setFixing] = useState(false);
  async function fixDisabledSources() {
    setFixing(true);
    try {
      const r = await api.post("/sources/fix-disabled");
      const { total, fixed, removed } = r.data;
      if (total === 0) {
        message.info("没有禁用的来源需要修复");
      } else {
        message.success(`修复 ${fixed} 个，移除 ${removed} 个（共 ${total} 个禁用来源）`);
      }
      refetch();
    } catch {
      message.error("修复失败");
    } finally {
      setFixing(false);
    }
  }
  async function batchDiscoverSelected() {
    for (const t of selTopics) await topicDiscover(t);
  }

  const topicOptions = topics.map((t) => ({ value: t.name, label: t.name }));

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Title level={2} style={{ margin: 0 }}>来源与主题</Title>

      {/* Smart recommendation */}
      <Card title="智能推荐主题">
        <Paragraph type="secondary">输入一个或多个大主题（逗号分隔）→ 推荐子主题（可多选）→ 每个子主题分别推荐关键词 → 逐个或批量加为主题。</Paragraph>
        <Space wrap>
          <Input placeholder="如：科技, 财经" value={themes} onChange={(e) => setThemes(e.target.value)} style={{ width: 320 }} />
          <Button type="primary" onClick={recoSubtopics}>推荐子主题</Button>
          <Button icon={<ThunderboltOutlined />} loading={autoBusy} onClick={autoDiscoverAll}>自动一键发现</Button>
          <Text type="secondary">{recoStatus}</Text>
        </Space>
        {subtopics.length > 0 && (
          <div style={{ marginTop: 12 }}>
            <Text>① 选择子主题（可多选，选中即推荐其关键词）：</Text>
            <div style={{ marginTop: 8 }}>
              {subtopics.map((st) => (
                <Tag.CheckableTag key={st} checked={!!groups.find((g) => g.name === st)}
                  onChange={() => toggleSubtopic(st)} style={{ marginBottom: 6 }}>{st}</Tag.CheckableTag>
              ))}
            </div>
          </div>
        )}
        {groups.map((g, gi) => (
          <Card key={g.name} size="small" style={{ marginTop: 10 }}
            title={<Input defaultValue={g.name} onChange={(e) => {
              const v = e.target.value; setGroups((gs) => gs.map((x, i) => i === gi ? { ...x, name: v } : x));
            }} style={{ width: 200 }} />}
            extra={<Button type="primary" size="small" onClick={() => addTopic(g.name, g.keywords.filter((k) => k.on).map((k) => k.kw).join(", "))}>加为主题</Button>}>
            {g.keywords.length ? g.keywords.map((k, ki) => (
              <Tag.CheckableTag key={k.kw} checked={k.on} onChange={(on) =>
                setGroups((gs) => gs.map((x, i) => i === gi
                  ? { ...x, keywords: x.keywords.map((y, j) => j === ki ? { ...y, on } : y) } : x))}>{k.kw}</Tag.CheckableTag>
            )) : <Text type="secondary">加载关键词中…</Text>}
          </Card>
        ))}
        {groups.length > 1 && <Button type="primary" style={{ marginTop: 10 }} onClick={batchAddTopics}>＋ 批量加为主题</Button>}
      </Card>

      {/* Topics */}
      <div>
        <Space style={{ marginBottom: 8 }}>
          <Title level={4} style={{ margin: 0 }}>主题</Title>
          {selTopics.length > 0 && <>
            <Button danger size="small" onClick={batchDeleteTopics}>批量删除</Button>
            <Button type="primary" size="small" onClick={batchDiscoverSelected}>批量发现来源</Button>
          </>}
        </Space>
        <Table rowKey="name" size="small" pagination={false} dataSource={topics}
          rowSelection={{ selectedRowKeys: selTopics, onChange: (k) => setSelTopics(k as string[]) }}
          columns={[
            { title: "名称", dataIndex: "name", render: (v) => <strong>{v}</strong> },
            { title: "关键词", dataIndex: "keywords", render: (v: string[]) => <Text type="secondary">{v.join(", ")}</Text> },
            { title: "操作", width: 220, render: (_, t: Topic) => (
              <Space>
                <Button size="small" onClick={() => setTopicEdit(t)}>编辑</Button>
                <Button size="small" onClick={() => topicDiscover(t.name)}>发现来源</Button>
                <Button size="small" danger onClick={() => deleteTopic(t.name)}>删除</Button>
              </Space>
            ) },
          ]}
          expandable={{
            expandedRowKeys: Object.keys(discover),
            showExpandColumn: false,
            expandedRowRender: (t: Topic) => {
              const st = discover[t.name];
              if (!st) return null;
              return (
                <div>
                  <Text type="secondary">{st.status}</Text>
                  <div style={{ margin: "8px 0" }}>
                    {st.cands.map((c) => (
                      <Tag.CheckableTag key={c.url} checked={st.sel.has(c.url)}
                        onChange={(on) => setDiscover((d) => {
                          const s = new Set(d[t.name].sel); on ? s.add(c.url) : s.delete(c.url);
                          return { ...d, [t.name]: { ...d[t.name], sel: s } };
                        })}><Tooltip title={c.url}>{c.name}</Tooltip></Tag.CheckableTag>
                    ))}
                  </div>
                  {st.cands.length > 0 && <Button type="primary" size="small" onClick={() => topicAddSources(t.name)}>添加选中来源</Button>}
                </div>
              );
            },
          }}
        />
      </div>

      {/* Sources */}
      <div>
        <Space style={{ marginBottom: 8 }}>
          <Title level={4} style={{ margin: 0 }}>来源</Title>
          <Button size="small" disabled={!selSources.length} onClick={() => batchToggleSources(true)}>批量启用</Button>
          <Button size="small" danger disabled={!selSources.length} onClick={() => batchToggleSources(false)}>批量禁用</Button>
          <Button danger size="small" disabled={!selSources.length} onClick={batchDeleteSources}>批量删除</Button>
          <Button size="small" loading={checking} onClick={checkAllReachability}>检测可达性</Button>
          <Button size="small" loading={fixing} onClick={fixDisabledSources}>修复禁用来源</Button>
        </Space>
        <Table rowKey="url" size="small" pagination={false} dataSource={sources}
          rowSelection={{ selectedRowKeys: selSources, onChange: (k) => setSelSources(k as string[]) }}
          rowClassName={(s: Source) => (s.enabled ? "" : "disabled-row")}
          columns={[
            { title: "名称", dataIndex: "name" },
            { title: "类型", dataIndex: "type", width: 70 },
            { title: "URL", dataIndex: "url", ellipsis: true, render: (v) => <a href={v} target="_blank" rel="noopener">{v}</a> },
            { title: "主题", dataIndex: "topics", render: (v: string[]) => v.map((t) => <Tag key={t}>{t}</Tag>) },
            { title: "模式", width: 70, render: (_, s: Source) => (s.type === "scrape" ? s.mode : "-") },
            { title: "状态", width: 100, render: (_, s: Source) => (
              <Space size={2}>
                <Button size="small" type={s.enabled ? "primary" : "default"} disabled={s.enabled} onClick={() => toggleSource(s.url)}>启用</Button>
                <Button size="small" danger={!s.enabled} disabled={!s.enabled} onClick={() => toggleSource(s.url)}>禁用</Button>
              </Space>
            ) },
            { title: "操作", width: 140, render: (_, s: Source) => (
              <Space>
                <Button size="small" onClick={() => setSourceEdit(s)}>编辑</Button>
                <Button size="small" danger onClick={() => deleteSource(s.url, s.name)}>删除</Button>
              </Space>
            ) },
          ]}
        />
      </div>

      {/* Manual discovery */}
      <Card title="手动发现来源">
        <Paragraph type="secondary">给一个网站地址，自动发现它的 RSS 订阅；找不到则加为网页爬取源。</Paragraph>
        <Space wrap style={{ marginBottom: 12 }}>
          <Input placeholder="网站/栏目 URL（如 https://openai.com/blog）" value={discUrl} onChange={(e) => setDiscUrl(e.target.value)} style={{ width: 360 }} />
          <Select placeholder="归类到主题…" allowClear value={discTopic || undefined} onChange={(v) => setDiscTopic(v || "")} options={topicOptions} style={{ width: 160 }} />
          <Button type="primary" onClick={doDiscover}>从网址发现</Button>
        </Space>
        <Paragraph type="secondary">或按关键词联网搜索相关站点并自动添加（DuckDuckGo，无需 API key）。</Paragraph>
        <Space wrap>
          <Input placeholder="关键词（如 physical AI robotics blog）" value={searchKw} onChange={(e) => setSearchKw(e.target.value)} style={{ width: 360 }} />
          <Select placeholder="归类到主题…" allowClear value={searchTopic || undefined} onChange={(v) => setSearchTopic(v || "")} options={topicOptions} style={{ width: 160 }} />
          <Button type="primary" onClick={doSearch}>按关键词发现</Button>
        </Space>
        <Divider />
        <Paragraph type="secondary">批量添加：每行一个，格式 <code>名称 | URL | 类型(rss/scrape) | 主题(逗号分隔) | 模式(single/list)</code></Paragraph>
        <Input.TextArea rows={6} value={batchText} onChange={(e) => setBatchText(e.target.value)}
          placeholder={"OpenAI Blog | https://openai.com/blog | | AI\nGoogle DeepMind | https://deepmind.google/discover/blog/ | scrape | AI | list"} style={{ fontFamily: "monospace" }} />
        <Button type="primary" style={{ marginTop: 8 }} onClick={doBatchAdd}>批量添加</Button>
      </Card>

      {/* Topic edit modal */}
      <TopicEditModal topic={topicEdit} onClose={() => setTopicEdit(null)} onSaved={() => { setTopicEdit(null); refetch(); }} />
      {/* Source edit modal */}
      <SourceEditModal source={sourceEdit} onClose={() => setSourceEdit(null)} onSaved={() => { setSourceEdit(null); refetch(); }} />
    </Space>
  );
}

function TopicEditModal({ topic, onClose, onSaved }: { topic: Topic | null; onClose: () => void; onSaved: () => void; }) {
  const [form] = Form.useForm();
  return (
    <Modal open={!!topic} title={`编辑主题：${topic?.name || ""}`} onCancel={onClose}
      destroyOnClose forceRender
      onOk={async () => {
        const v = await form.validateFields();
        await postForm("/sources/topics/edit", { old_name: topic!.name, name: v.name, keywords: v.keywords || "" });
        onSaved();
      }}>
      {topic && (
        <Form form={form} layout="vertical" initialValues={{ name: topic.name, keywords: topic.keywords.join(", ") }} preserve={false}>
          <Form.Item name="name" label="主题名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="keywords" label="关键词（逗号分隔）"><Input /></Form.Item>
        </Form>
      )}
    </Modal>
  );
}

function SourceEditModal({ source, onClose, onSaved }: { source: Source | null; onClose: () => void; onSaved: () => void; }) {
  const [form] = Form.useForm();
  const type = Form.useWatch("type", form);
  return (
    <Modal open={!!source} title={`编辑来源：${source?.name || ""}`} onCancel={onClose}
      destroyOnClose forceRender width={640}
      onOk={async () => {
        const v = await form.validateFields();
        await postForm("/sources/edit", {
          url: source!.url, name: v.name, type: v.type,
          topics: v.topics || "", mode: v.mode || "single",
          include_pattern: v.include_pattern || "", exclude_pattern: v.exclude_pattern || "",
          max_pages: v.max_pages || 1, render_js: v.render_js ? "1" : "",
        });
        onSaved();
      }}>
      {source && (
        <Form form={form} layout="vertical" preserve={false} initialValues={{
          name: source.name, type: source.type, topics: source.topics.join(", "),
          mode: source.mode, include_pattern: source.include_pattern || "",
          exclude_pattern: source.exclude_pattern || "", max_pages: source.max_pages || 1,
          render_js: source.render_js,
        }}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="type" label="类型"><Select options={[{ value: "rss" }, { value: "scrape" }]} /></Form.Item>
          <Form.Item name="topics" label="主题（逗号分隔）"><Input /></Form.Item>
          {type === "scrape" && <>
            <Form.Item name="mode" label="模式"><Select options={[{ value: "single" }, { value: "list" }]} /></Form.Item>
            <Form.Item name="include_pattern" label="include_pattern"><Input /></Form.Item>
            <Form.Item name="exclude_pattern" label="exclude_pattern"><Input /></Form.Item>
            <Form.Item name="max_pages" label="翻页数(list)"><InputNumber min={1} /></Form.Item>
            <Form.Item name="render_js" label="JS 渲染" valuePropName="checked"><Checkbox /></Form.Item>
          </>}
        </Form>
      )}
    </Modal>
  );
}
