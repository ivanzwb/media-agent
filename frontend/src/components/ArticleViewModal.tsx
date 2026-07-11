import { Modal, Spin } from "antd";
import { useQuery } from "@tanstack/react-query";
import { marked } from "marked";
import { getJson } from "../api/client";

interface ViewData {
  ok: boolean;
  article: { title: string; url: string; source_name: string; topic: string;
    published_at: string | null; fetched_at: string; };
  content_md: string; images: string[]; videos: string[];
}

function videoHtml(u: string) {
  const isFile = /^\/media\//.test(u) || /\.(mp4|webm|mov|m4v|ogg|ogv)(\?|$)/i.test(u);
  if (isFile) return `<video controls preload="metadata" style="width:100%;margin:1em 0" src="${u}"></video>`;
  return `<div style="position:relative;padding-top:56%;margin:1em 0"><iframe src="${u}" style="position:absolute;inset:0;width:100%;height:100%;border:0" allowfullscreen></iframe></div>`;
}
function imgHtml(u: string) {
  return `<img src="${u}" alt="" style="width:100%;max-width:100%;height:auto;display:block;margin:1em 0" />`;
}

/** Port of article_view.html weaving: replace [[IMG:N]]/[[VIDEO:N]] with media
 * and weave in orphaned images in document order. */
function buildHtml(md: string, images: string[], videos: string[]): string {
  const raw = marked.parse(md, { async: false }) as string;
  const placedImg: Record<number, boolean> = {};
  const placedVid: Record<number, boolean> = {};
  let orphanIdx = 0;
  const orphansUpTo = (n: number) => {
    let out = "";
    while (orphanIdx < n) {
      if (!placedImg[orphanIdx] && images[orphanIdx]) { out += imgHtml(images[orphanIdx]); placedImg[orphanIdx] = true; }
      orphanIdx++;
    }
    return out;
  };
  const parts = raw.split(/(\[\[(?:IMG|VIDEO):\d+\]\])/);
  let outHtml = "";
  for (const seg of parts) {
    const im = seg.match(/^\[\[IMG:(\d+)\]\]$/);
    const vm = seg.match(/^\[\[VIDEO:(\d+)\]\]$/);
    if (im) {
      const i = parseInt(im[1], 10);
      outHtml += orphansUpTo(i);
      if (i >= 0 && i < images.length) { outHtml += imgHtml(images[i]); placedImg[i] = true; }
      if (orphanIdx <= i) orphanIdx = i + 1;
    } else if (vm) {
      const i = parseInt(vm[1], 10);
      if (i >= 0 && i < videos.length) { outHtml += videoHtml(videos[i]); placedVid[i] = true; }
    } else outHtml += seg;
  }
  if (!/<img\s/i.test(raw)) outHtml += orphansUpTo(images.length);
  const extraVids = videos.filter((_u, i) => !placedVid[i]);
  if (extraVids.length) outHtml += "<h2>视频</h2>" + extraVids.map(videoHtml).join("");
  return outHtml;
}

export default function ArticleViewModal({
  articleId, onClose,
}: { articleId: number | null; onClose: () => void; }) {
  const { data, isLoading } = useQuery({
    queryKey: ["article-view", articleId],
    queryFn: () => getJson<ViewData>(`/api/archive/${articleId}/view`),
    enabled: articleId != null,
  });

  return (
    <Modal open={articleId != null} onCancel={onClose} footer={null} width="80vw"
      styles={{ body: { maxHeight: "80vh", overflowY: "auto" } }}
      title={data?.article?.title || "原文"}>
      {isLoading || !data ? <Spin /> : (
        <>
          <p style={{ color: "#888" }}>
            来源：{data.article.source_name} · 主题：{data.article.topic} ·
            发布于：{data.article.published_at || "—"} · 抓取于：{data.article.fetched_at}
            {"  "}
            <a href={data.article.url} target="_blank" rel="noopener">原始链接 ↗</a>
          </p>
          <div className="article-body"
            dangerouslySetInnerHTML={{ __html: buildHtml(data.content_md, data.images, data.videos) }} />
        </>
      )}
    </Modal>
  );
}
