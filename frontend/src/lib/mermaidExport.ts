// Browser-side Mermaid → PNG for 微信 export.
//
// Ported from spider-media's MermaidConverter.ts — the reference pattern for
// getting diagrams into 微信 (the editor strips inline <svg>). Each ```mermaid
// fence in the draft body is rendered with htmlLabels:true (the real browser
// measures CJK node text), its <foreignObject> labels are converted to
// absolutely-positioned SVG <text> (canvas drawing with foreignObject both
// mis-renders and pollutes the canvas → SecurityError on toDataURL),
// rasterized to a white-background PNG data URL, uploaded via the caller's
// `upload` callback (POST /api/draft/{id}/mermaid-image), and the fence is
// replaced with ![](/media/...) so the backend's normal image +
// media-localization path carries it to 微信 unchanged.
//
// Render/upload failure THROWS a descriptive error: 公众号 markdown_to_html
// has no code-fence handling, so leaking a raw fence would publish garbage
// paragraphs. The editor preview already shows broken diagrams (MermaidBlock
// falls back to <pre>), so the author can fix and retry.

type MermaidModule = typeof import("mermaid").default;
type MermaidConfig = Parameters<MermaidModule["initialize"]>[0];

let pending: Promise<MermaidModule> | null = null;

function loadMermaid(): Promise<MermaidModule> {
  if (!pending) {
    pending = import("mermaid").then((module) => module.default);
  }
  return pending;
}

// Export layout: browser-measured labels (htmlLabels) so CJK text fits and
// centers inside node boxes; useMaxWidth off so diagrams rasterize at their
// natural size.
const EXPORT_CONFIG: MermaidConfig = {
  startOnLoad: false,
  flowchart: { htmlLabels: true, useMaxWidth: false },
  sequence: { useMaxWidth: false },
  gantt: { useMaxWidth: false },
  class: { useMaxWidth: false },
};

// Editor-preview config — restored after export so on-page mermaid previews
// (KnowledgeMap / MermaidBlock) keep rendering as SVG text.
const PREVIEW_CONFIG: MermaidConfig = {
  startOnLoad: false,
  securityLevel: "strict",
  theme: "neutral",
  flowchart: { htmlLabels: false, curve: "basis", useMaxWidth: true },
  fontFamily: '-apple-system, "PingFang SC", "Microsoft YaHei", sans-serif',
};

const _MERMAID_FENCE_RE = /```mermaid[ \t]*\r?\n([\s\S]*?)```/g;

/** Replace every ```mermaid fence in `markdown` with ![](<uploaded PNG URL>).
 *
 * `upload` receives the PNG data URL and must return the public URL (the
 * caller posts it to the mermaid-image endpoint). Returns the converted
 * markdown and the number of diagrams rendered. Throws on the first render /
 * upload failure.
 */
export async function convertMermaidInMarkdown(
  markdown: string,
  upload: (pngDataUrl: string) => Promise<string>,
): Promise<{ markdown: string; count: number }> {
  const fences = Array.from(markdown.matchAll(_MERMAID_FENCE_RE));
  if (fences.length === 0) return { markdown, count: 0 };

  const mermaid = await loadMermaid();
  mermaid.initialize(EXPORT_CONFIG);
  try {
    const out: string[] = [];
    let last = 0;
    let count = 0;
    for (let i = 0; i < fences.length; i++) {
      const fence = fences[i];
      out.push(markdown.slice(last, fence.index));
      const code = fence[1] ?? "";
      const png = await renderMermaidToPng(mermaid, code, i);
      if (!png) {
        throw new Error(`第 ${i + 1} 个 Mermaid 流程图渲染失败，请修正图中语法后重试`);
      }
      const url = await upload(png.dataUrl);
      out.push(`![mermaid 流程图](${url})`);
      count += 1;
      last = fence.index + fence[0].length;
    }
    out.push(markdown.slice(last));
    return { markdown: out.join(""), count };
  } finally {
    mermaid.initialize(PREVIEW_CONFIG);
  }
}

async function renderMermaidToPng(
  mermaid: MermaidModule,
  code: string,
  index: number,
): Promise<{ dataUrl: string; width: number } | null> {
  try {
    const { svg } = await mermaid.render(`ma-export-${Date.now()}-${index}`, code);
    const sanitized = foreignObjectToText(svg);
    return await svgToPng(sanitized);
  } catch (err) {
    console.warn("[media-agent] mermaid 渲染失败", err);
    return null;
  }
}

/**
 * Replace mermaid's <foreignObject> label blocks with absolutely positioned
 * SVG <text> (port of spider-media MermaidConverter.foreignObjectToText):
 *   1. canvas drawing an SVG containing foreignObject pollutes the canvas,
 *      making toDataURL throw SecurityError
 *   2. just dropping foreignObject leaves text floating at (0,0) — instead
 *      use its x/y/width/height to compute centered coordinates, splitting
 *      lines into <tspan dy> rows
 */
function foreignObjectToText(svg: string): string {
  return svg.replace(
    /<foreignObject\s+([^>]*?)>([\s\S]*?)<\/foreignObject>/g,
    (_match, attrs: string, inner: string) => {
      const x = Number(/(?:^|\s)x="([\d.-]+)"/.exec(attrs)?.[1] ?? 0);
      const y = Number(/(?:^|\s)y="([\d.-]+)"/.exec(attrs)?.[1] ?? 0);
      const width = Number(/(?:^|\s)width="([\d.-]+)"/.exec(attrs)?.[1] ?? 0);
      const height = Number(/(?:^|\s)height="([\d.-]+)"/.exec(attrs)?.[1] ?? 0);

      // Multi-line: mermaid expresses lines as <br> or multiple <span> in a <div>.
      const text = String(inner)
        .replace(/<br\s*\/?>(\s*)/gi, "\n")
        .replace(/<\/p>\s*<p[^>]*>/gi, "\n")
        .replace(/<[^>]+>/g, "")
        .replace(/&nbsp;/g, " ")
        .replace(/&amp;/g, "&")
        .replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">")
        .trim();
      if (!text) return "";

      const lines = text.split(/\n+/).map((l) => l.trim()).filter(Boolean);
      const cx = x + width / 2;
      const cy = y + height / 2;
      const fontSize = 14;
      const lineHeight = fontSize * 1.25;
      const totalH = (lines.length - 1) * lineHeight;
      const firstY = cy - totalH / 2;

      const tspans = lines
        .map((line, i) => {
          const safe = line
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
          return `<tspan x="${cx}" y="${firstY + i * lineHeight}">${safe}</tspan>`;
        })
        .join("");
      return `<text text-anchor="middle" dominant-baseline="middle" font-size="${fontSize}" font-family="-apple-system,BlinkMacSystemFont,'PingFang SC','Helvetica Neue',Arial,sans-serif" fill="#333">${tspans}</text>`;
    },
  );
}

/** SVG string → PNG data URL via canvas rasterization (port of
 * MermaidConverter.svgToPng). White background so 微信's dark-mode readers
 * still see a clean diagram. */
async function svgToPng(svg: string): Promise<{ dataUrl: string; width: number } | null> {
  try {
    // Fallback xmlns, otherwise the <img> load fails.
    let normalized = svg.trim();
    if (!/xmlns=/.test(normalized)) {
      normalized = normalized.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"');
    }
    const { width, height } = parseSvgSize(normalized);
    const scale = window.devicePixelRatio > 1 ? 2 : 1.5; // crisper export
    // data URL (not Blob URL): Blob URLs are treated as cross-origin in
    // Electron/Chromium (no CORS headers) and pollute the canvas. data URLs
    // are same-origin, so NO crossOrigin attribute on the <img>.
    const utf8 = new TextEncoder().encode(normalized);
    let bin = "";
    for (let i = 0; i < utf8.length; i++) bin += String.fromCharCode(utf8[i]);
    const dataUrl = `data:image/svg+xml;base64,${btoa(bin)}`;
    const img = await loadImage(dataUrl);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    return { dataUrl: canvas.toDataURL("image/png"), width: Math.round(width) };
  } catch (err) {
    console.warn("[media-agent] SVG → PNG 失败", err);
    return null;
  }
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`加载图片失败: ${src.slice(0, 64)}…`));
    img.src = src;
  });
}

function parseSvgSize(svg: string): { width: number; height: number } {
  const widthAttr = svg.match(/<svg[^>]*\swidth="([\d.]+)(?:px)?"/);
  const heightAttr = svg.match(/<svg[^>]*\sheight="([\d.]+)(?:px)?"/);
  if (widthAttr && heightAttr) {
    return { width: Number(widthAttr[1]), height: Number(heightAttr[1]) };
  }
  const viewBox = svg.match(/<svg[^>]*\sviewBox="[\d.\s-]*?\s([\d.]+)\s([\d.]+)"/);
  if (viewBox) {
    return { width: Number(viewBox[1]), height: Number(viewBox[2]) };
  }
  return { width: 800, height: 600 };
}

/** data:image/...;base64 → File for multipart upload (server infers the
 * extension from the filename). */
export function dataUrlToFile(dataUrl: string, filename = "mermaid.png"): File {
  const comma = dataUrl.indexOf(",");
  const mime = /data:([^;,]+)/.exec(dataUrl.slice(0, comma))?.[1] || "image/png";
  const bin = atob(dataUrl.slice(comma + 1));
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new File([bytes], filename, { type: mime });
}
