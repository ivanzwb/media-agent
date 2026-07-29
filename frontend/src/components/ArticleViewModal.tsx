import { Alert, Button, Modal, Spin } from "antd";
import { useQuery } from "@tanstack/react-query";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { getJson } from "../api/client";

interface ViewData {
  ok: boolean;
  article: { title: string; url: string; source_name: string; topic: string;
    published_at: string | null; fetched_at: string; };
  content_md: string; images: string[]; videos: string[];
}

function safeMediaUrl(u: string): string {
  const url = String(u || "").trim();
  if (["/media/", "/images/", "/videos/"].some(
    (prefix) => url.startsWith(prefix))) return url;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? url : "";
  } catch {
    return "";
  }
}
function escapeAttr(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function videoHtml(u: string) {
  const url = safeMediaUrl(u);
  if (!url) return "";
  const src = escapeAttr(url);
  const isFile = /^\/media\//.test(url)
    || /\.(mp4|webm|mov|m4v|ogg|ogv|m3u8)(\?|$)/i.test(url);
  if (isFile) return `<video controls preload="metadata" style="width:100%;margin:1em 0" src="${src}"></video>`;
  return `<div style="position:relative;padding-top:56%;margin:1em 0"><iframe src="${src}" style="position:absolute;inset:0;width:100%;height:100%;border:0" allowfullscreen></iframe></div>`;
}
function imgHtml(u: string) {
  const url = safeMediaUrl(u);
  return url ? `<img src="${escapeAttr(url)}" alt="" style="width:100%;max-width:100%;height:auto;display:block;margin:1em 0" />` : "";
}

function stripVideoFallbackLinks(md: string): string {
  const normalizeUrl = (url: string) => url
    .replace(/&amp;/gi, "&").replace(/&#0*38;/gi, "&");
  const embedded = new Set(
    [...md.matchAll(/<(?:iframe|video)\b[^>]*\bsrc=["']([^"']+)["']/gi)]
      .map((match) => normalizeUrl(match[1])),
  );
  return md.replace(
    /^\s*\[▶ 视频链接\]\(([^)\s]+)\)\s*$/gm,
    (line, url) => embedded.has(normalizeUrl(url)) ? "" : line,
  );
}
function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
function sanitizePreviewHtml(raw: string): string {
  const clean = DOMPurify.sanitize(raw, {
    ADD_TAGS: ["iframe", "video", "source"],
    ADD_ATTR: [
      "allowfullscreen", "controls", "preload", "sandbox", "loading",
      "referrerpolicy", "frameborder",
    ],
  });
  const doc = new DOMParser().parseFromString(clean, "text/html");
  doc.querySelectorAll("iframe").forEach((iframe) => {
    if (!safeMediaUrl(iframe.getAttribute("src") || "")) {
      iframe.remove();
      return;
    }
    iframe.setAttribute(
      "sandbox", "allow-scripts allow-same-origin allow-presentation");
    iframe.setAttribute("loading", "lazy");
    iframe.setAttribute("referrerpolicy", "no-referrer");
  });
  return doc.body.innerHTML;
}

/** Port of article_view.html weaving: replace [[IMG:N]]/[[VIDEO:N]] with media
 * and weave in orphaned images in document order. */
function buildHtml(md: string, images: string[], videos: string[]): string {
  let source = stripVideoFallbackLinks(md);
  const placedImg: Record<number, boolean> = {};
  const placedVid: Record<number, boolean> = {};

  // Repair legacy `![](video.mp4)` before Markdown turns it into a broken img.
  videos.forEach((url, index) => {
    const escaped = escapeRegExp(url);
    const imageVideo = new RegExp(
      `!\\[[^\\]]*\\]\\(${escaped}(?:\\s+[^)]*)?\\)`, "g");
    source = source.replace(imageVideo, () => {
      placedVid[index] = true;
      return `\n\n${videoHtml(url)}\n\n`;
    });
    const embeds = [
      ...source.matchAll(/<(?:iframe|video)\b[^>]*\bsrc=["']([^"']+)["']/gi),
    ];
    if (embeds.some((match) => match[1] === url)) placedVid[index] = true;
  });
  images.forEach((url, index) => {
    const escaped = escapeRegExp(url);
    if (new RegExp(
      `!\\[[^\\]]*\\]\\(${escaped}(?:\\s+[^)]*)?\\)`
      + `|<img\\b[^>]*\\bsrc=["']${escaped}["']`, "i").test(source)) {
      placedImg[index] = true;
    }
  });

  // Weave placeholders while still in Markdown, avoiding block media nested
  // inside the <p> elements produced by marked.
  source = source.replace(
    /\[\[(IMG|VID|VIDEO):(\d+)\]\]/gi,
    (_token, kind, rawIndex) => {
      const index = Number(rawIndex);
      if (String(kind).toUpperCase() === "IMG") {
        if (!images[index]) return "";
        placedImg[index] = true;
        return `\n\n${imgHtml(images[index])}\n\n`;
      }
      if (!videos[index]) return "";
      placedVid[index] = true;
      return `\n\n${videoHtml(videos[index])}\n\n`;
    },
  );

  const missingImages = images
    .filter((_url, index) => !placedImg[index]).map(imgHtml).filter(Boolean);
  const missingVideos = videos
    .filter((_url, index) => !placedVid[index]).map(videoHtml).filter(Boolean);
  if (missingImages.length) source += `\n\n${missingImages.join("\n\n")}`;
  if (missingVideos.length) {
    source += `\n\n## 视频\n\n${missingVideos.join("\n\n")}`;
  }
  const raw = marked.parse(source, { async: false }) as string;
  return sanitizePreviewHtml(raw);
}

export default function ArticleViewModal({
  articleId, onClose,
}: { articleId: number | null; onClose: () => void; }) {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["article-view", articleId],
    queryFn: () => getJson<ViewData>(`/api/archive/${articleId}/view`),
    enabled: articleId != null,
  });

  return (
    <Modal open={articleId != null} onCancel={onClose} footer={null} width="80vw"
      styles={{ body: { maxHeight: "80vh", overflowY: "auto" } }}
      title={data?.article?.title || "原文"}>
      {isError ? (
        <Alert type="error" showIcon message="原文加载失败"
          action={<Button size="small" onClick={() => refetch()}>重试</Button>} />
      ) : isLoading || !data ? <Spin /> : (
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
