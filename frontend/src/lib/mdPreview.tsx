// Live-preview helpers for the draft editor's Markdown preview.
//
// The app uses a few custom syntaxes on top of Markdown:
//   inline: {color:#hex}text{/color}  and  ==highlight==
//   block:  :::type ... :::  (center / right / tip / info / warning / success /
//           danger / highlight)
//   layout: ::::columns ... :::col ... ::: ... :::: (秀米-style flex columns)
// The SOURCE markdown must keep this exact syntax (backend renderers depend on
// it). These helpers only transform the editor's live preview so the author sees
// styled output instead of literal directive text.

// Flatten an mdast node's text content (soft breaks -> \n).
function _nodeText(n: any): string {
  if (!n) return "";
  if (n.type === "text") return n.value || "";
  if (n.type === "break") return "\n";
  // Re-serialize inline images to `![alt](url)` so the URL survives flattening
  // when an image lives INSIDE a directive body (e.g. :::imgcard / :::col).
  if (n.type === "image") return `![${n.alt || ""}](${n.url || ""})`;
  if (Array.isArray(n.children)) return n.children.map(_nodeText).join("");
  return "";
}

// Split text into mdast nodes, turning the app's inline directives into real
// hast elements via data.hName/hProperties: {color:HEX}..{/color} -> <span
// style=color:..> and ==text== -> <mark>. Using hName (not raw HTML) means the
// preview renders these as proper elements regardless of raw-HTML settings.
// Default image style — byte-identical with formatter.py's st["img"] so an
// image inside a directive body renders the same in preview and WeChat.
const _INLINE_IMG_STYLE = "max-width:100%;display:block;margin:14px auto;border-radius:6px;";

function _inlineNodes(text: string): any[] {
  // Order matters: image (![..](..)) before link ([..](..)); bold (**) before italic (*).
  const RE = /\{color:(#[0-9a-fA-F]{3,8}|[a-zA-Z][\w-]*)\}([\s\S]*?)\{\/color\}|==([^=]+?)==|!\[([^\]]*)\]\(([^)\s]+)\)|\*\*([^*]+?)\*\*|\*([^*]+?)\*|\[([^\]]+?)\]\(([^)\s]+)\)/g;
  const nodes: any[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = RE.exec(text)) !== null) {
    if (m.index > last) nodes.push({ type: "text", value: text.slice(last, m.index) });
    if (m[1] !== undefined) {
      nodes.push({ type: "textDirective",
        data: { hName: "span", hProperties: { style: `color:${m[1]}` } },
        children: [{ type: "text", value: m[2] }] });
    } else if (m[3] !== undefined) {
      nodes.push({ type: "textDirective", data: { hName: "mark" },
        children: [{ type: "text", value: m[3] }] });
    } else if (m[4] !== undefined || m[5] !== undefined) {
      nodes.push({ type: "textDirective",
        data: { hName: "img",
          hProperties: { src: m[5], alt: m[4] || "", style: _INLINE_IMG_STYLE } },
        children: [] });
    } else if (m[6] !== undefined) {
      nodes.push({ type: "textDirective", data: { hName: "strong" },
        children: [{ type: "text", value: m[6] }] });
    } else if (m[7] !== undefined) {
      nodes.push({ type: "textDirective", data: { hName: "em" },
        children: [{ type: "text", value: m[7] }] });
    } else {
      nodes.push({ type: "textDirective",
        data: { hName: "a", hProperties: { href: m[9] } },
        children: [{ type: "text", value: m[8] }] });
    }
    last = RE.lastIndex;
  }
  if (last < text.length) nodes.push({ type: "text", value: text.slice(last) });
  return nodes;
}

const _DIRECTIVE_PALETTE: Record<string, [string, string, string]> = {
  tip: ["#f0f9eb", "#67c23a", "#3c6e2a"],
  success: ["#f0f9eb", "#67c23a", "#3c6e2a"],
  info: ["#eef4fd", "#409eff", "#1d4e89"],
  warning: ["#fdf6ec", "#e6a23c", "#8a6d1f"],
  danger: ["#fef0f0", "#f56c6c", "#a13333"],
  highlight: ["#fffbe6", "#faad14", "#874d00"],
};

// Decorative-component styles — kept BYTE-IDENTICAL with the matching
// constants in app/wechat/formatter.py so these render the same in the live
// preview and the published WeChat HTML (strict WYSIWYG).
const _DIVIDER_DASHED_STYLE = "border:none;border-top:1px dashed #c8c8c8;height:0;margin:22px 0;";
const _DIVIDER_GRADIENT_STYLE = "border:none;height:3px;margin:22px 0;border-radius:2px;background:linear-gradient(to right,rgba(64,158,255,0),#409eff,rgba(64,158,255,0));";
const _BLOCKTITLE_STYLE = "background:#2f6fb3;color:#ffffff;padding:8px 16px;border-radius:6px;text-align:center;font-weight:bold;font-size:17px;margin:16px 0;";
const _DUALLINE_STYLE = "border-top:2px solid #333333;border-bottom:2px solid #333333;padding:8px 0;text-align:center;font-weight:bold;font-size:18px;color:#222222;margin:18px 0;";
const _TITLENUM_WRAP_STYLE = "margin:18px 0 10px;";
const _TITLENUM_BADGE_STYLE = "display:inline-block;min-width:26px;height:26px;line-height:26px;text-align:center;background:#2f6fb3;color:#ffffff;border-radius:13px;font-weight:bold;font-size:15px;margin-right:10px;padding:0 6px;";
const _TITLENUM_TEXT_STYLE = "font-size:18px;font-weight:bold;color:#222222;vertical-align:middle;";
const _IMGCARD_FRAME_STYLE = "border:1px solid #e6e8eb;border-radius:10px;overflow:hidden;margin:16px 0;box-shadow:0 2px 12px rgba(0,0,0,0.08);background:#ffffff;";
const _IMGCARD_IMG_STYLE = "display:block;width:100%;margin:0;border-radius:0;";
const _IMGCARD_CAP_STYLE = "margin:0;padding:8px 12px;font-size:13px;color:#888888;text-align:center;";
// Matches formatter.py :::card box style exactly.
const _CARD_STYLE = "background:#ffffff;border:1px solid #e6e8eb;color:#333;padding:14px 16px;margin:16px 0;border-radius:8px;";
// wrapper blocks + numbered steps — byte-identical with formatter.py.
const _BOX_BLOCK_STYLE = "background:#f5f7fa;padding:14px 16px;margin:16px 0;border-radius:8px;color:#333333;";
const _BORDER_BLOCK_STYLE = "border:1px solid #d9d9d9;padding:14px 16px;margin:16px 0;border-radius:8px;color:#333333;";
const _STEPS_WRAP_STYLE = "margin:16px 0;";
const _STEPS_ITEM_STYLE = "display:flex;align-items:flex-start;margin:12px 0;";
const _STEPS_BADGE_STYLE = "flex:0 0 auto;width:26px;height:26px;line-height:26px;text-align:center;background:#2f6fb3;color:#ffffff;border-radius:13px;font-weight:bold;font-size:14px;margin-right:12px;";
const _STEPS_BODY_STYLE = "flex:1;min-width:0;color:#333333;font-size:15px;line-height:1.7;";
const _IMG_LINE_RE = /!\[([^\]]*)\]\(([^)\s]+)\)/;

// Framed image card: image (if any) + optional caption, matching formatter's
// _render_imgcard. Parses the image line itself so it survives regardless of
// the inline image transform.
function _imgcardNode(rawLines: string[]): any {
  const children: any[] = [];
  const captionParts: string[] = [];
  let hasImg = false;
  for (const ln of rawLines) {
    const m = ln.match(_IMG_LINE_RE);
    if (m && !hasImg) {
      hasImg = true;
      children.push({ type: "textDirective",
        data: { hName: "img", hProperties: { src: m[2], alt: m[1] || "", style: _IMGCARD_IMG_STYLE } },
        children: [] });
    } else {
      captionParts.push(ln);
    }
  }
  if (captionParts.length) {
    children.push({ type: "containerDirective",
      data: { hName: "p", hProperties: { style: _IMGCARD_CAP_STYLE } },
      children: _inlineNodes(captionParts.join(" ")) });
  }
  return { type: "containerDirective",
    data: { hName: "section", hProperties: { style: _IMGCARD_FRAME_STYLE } },
    children };
}

// Parse a directive open-line arg (`color=#e74c3c align=center`) into params.
function _parseParams(arg: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const tok of (arg || "").trim().split(/\s+/)) {
    const eq = tok.indexOf("=");
    if (eq > 0) out[tok.slice(0, eq).toLowerCase()] = tok.slice(eq + 1);
  }
  return out;
}

// Build an inline-style override suffix (last declaration wins) — mirrors
// formatter.py _param_overrides so preview == WeChat.
function _paramOverrides(dp: Record<string, string>, kind: string): string {
  let css = "";
  const c = dp.color;
  if (c) {
    if (kind === "bg") css += `background:${c};`;
    else if (kind === "border") css += `border-color:${c};`;
    else if (kind === "leftbar") css += `border-left-color:${c};`;
  }
  if (dp.align === "left" || dp.align === "center" || dp.align === "right")
    css += `text-align:${dp.align};`;
  return css;
}

// Build a block-container mdast node (rendered as a styled element via hName).
function _directiveNode(type: string, inner: string, arg: string = ""): any {
  const t = type.toLowerCase();
  const dp = _parseParams(arg);
  const rawLines = inner.split(/\n+/).map((s) => s.trim()).filter(Boolean);

  // ── decorative dividers (no body) ──
  if (t === "dashed")
    return { type: "containerDirective",
      data: { hName: "div", hProperties: { style: _DIVIDER_DASHED_STYLE } }, children: [] };
  if (t === "gradient")
    return { type: "containerDirective",
      data: { hName: "div", hProperties: { style: _DIVIDER_GRADIENT_STYLE } }, children: [] };

  // ── decorative section titles ──
  if (t === "blocktitle" || t === "dualline")
    return { type: "containerDirective",
      data: { hName: "section", hProperties: { style: t === "blocktitle" ? _BLOCKTITLE_STYLE + _paramOverrides(dp, "bg") : _DUALLINE_STYLE } },
      children: _inlineNodes(rawLines.join(" ")) };
  if (t === "titlenum") {
    const raw = rawLines[0] || "";
    const bar = raw.indexOf("|");
    const num = (bar >= 0 ? raw.slice(0, bar) : "1").trim() || "1";
    const title = (bar >= 0 ? raw.slice(bar + 1) : raw).trim();
    return { type: "containerDirective",
      data: { hName: "section", hProperties: { style: _TITLENUM_WRAP_STYLE } },
      children: [
        { type: "textDirective", data: { hName: "span", hProperties: { style: _TITLENUM_BADGE_STYLE } },
          children: [{ type: "text", value: num }] },
        { type: "textDirective", data: { hName: "span", hProperties: { style: _TITLENUM_TEXT_STYLE } },
          children: _inlineNodes(title) },
      ] };
  }
  if (t === "imgcard") return _imgcardNode(rawLines);

  // ── numbered steps / list (auto-numbered badge + body per line) ──
  if (t === "steps" || t === "numlist") {
    return { type: "containerDirective",
      data: { hName: "section", hProperties: { style: _STEPS_WRAP_STYLE } },
      children: rawLines.map((item, idx) => ({
        type: "containerDirective",
        data: { hName: "section", hProperties: { style: _STEPS_ITEM_STYLE } },
        children: [
          { type: "textDirective", data: { hName: "span", hProperties: { style: _STEPS_BADGE_STYLE } },
            children: [{ type: "text", value: String(idx + 1) }] },
          { type: "containerDirective", data: { hName: "section", hProperties: { style: _STEPS_BODY_STYLE } },
            children: _inlineNodes(item) },
        ],
      })) };
  }

  // ── generic containers with inline (soft-break) content ──
  const kids: any[] = [];
  rawLines.forEach((line, i) => {
    if (i > 0) kids.push({ type: "break" });
    kids.push(..._inlineNodes(line));
  });
  if (t === "box" || t === "border")
    return { type: "containerDirective",
      data: { hName: "section", hProperties: { style: (t === "box" ? _BOX_BLOCK_STYLE : _BORDER_BLOCK_STYLE) + _paramOverrides(dp, t === "box" ? "bg" : "border") } }, children: kids };
  if (t === "center")
    return { type: "containerDirective",
      data: { hName: "div", hProperties: { style: "text-align:center" } }, children: kids };
  if (t === "right")
    return { type: "containerDirective",
      data: { hName: "div", hProperties: { style: "text-align:right" } }, children: kids };
  if (t === "card")
    return { type: "containerDirective",
      data: { hName: "section", hProperties: { style: _CARD_STYLE + _paramOverrides(dp, "border") } }, children: kids };
  const [bg, border, color] = _DIRECTIVE_PALETTE[t] || ["#f7f7f7", "#d9d9d9", "#333"];
  const style = `background:${bg};border-left:4px solid ${border};color:${color};`
    + "padding:10px 14px;border-radius:4px;margin:12px 0;" + _paramOverrides(dp, "leftbar");
  return { type: "containerDirective",
    data: { hName: "div", hProperties: { style } }, children: kids };
}

// Build a plain paragraph mdast node from soft-break-separated lines, applying
// the inline transforms ({color}/==mark==) to each line.
function _textParagraph(lines: string[]): any {
  const kids: any[] = [];
  lines.forEach((line, i) => {
    if (i > 0) kids.push({ type: "break" });
    kids.push(..._inlineNodes(line));
  });
  return { type: "paragraph", children: kids };
}

// Scan a single (flattened) paragraph's text for `:::type ... :::` blocks that
// may appear ANYWHERE — not only when the whole paragraph is a directive.
// Because remark merges consecutive non-blank lines into one paragraph, a
// directive written in the middle of an article shares a paragraph with its
// surrounding lines (joined by soft breaks). We split that text into an ordered
// list of nodes: plain paragraphs for the non-directive lines and styled block
// nodes for each directive. Returns null when the text has no directive block
// (so the original paragraph — and its inline nodes — is left untouched).
const _OPEN = /^:::([a-zA-Z]+)[ \t]*(.*)$/;
const _CLOSE = /^:::[ \t]*$/;

// ── multi-column layout (::::columns / :::col) ───────────────────────────
// 秀米-style side-by-side columns. FOUR colons open the outer container so
// ordinary 3-colon directives (:::tip …) nest cleanly inside a column. Kept in
// lock-step with the backend (app/wechat/formatter.py) so the live preview and
// the published WeChat HTML share the exact same flex structure + inline styles.
//   ::::columns          equal-width columns (count = number of :::col blocks)
//   ::::columns 1:2      custom flex ratios per column (left:right → flex:1/2)
//   ::::columns cols=3   explicit equal-width count
const _COL_CONTAINER_OPEN = /^::::columns\b[ \t]*(.*)$/;
const _COL_CONTAINER_CLOSE = /^::::[ \t]*$/;
const _COL_ITEM_OPEN = /^:::col\b[ \t]*(.*)$/;
// Byte-identical with formatter.py's _COLUMNS_ROW_STYLE (WYSIWYG parity).
const _COLUMNS_ROW_STYLE = "display:flex;gap:12px;margin:16px 0;align-items:flex-start;";

function _parseColRatios(param: string): number[] {
  const p = (param || "").trim();
  if (!p) return [];
  const cm = p.match(/^cols\s*=\s*(\d+)$/);
  if (cm) return Array(Math.max(1, parseInt(cm[1], 10))).fill(1);
  if (/^\d+(\s*:\s*\d+)*$/.test(p))
    return p.split(":").map((x) => Math.max(1, parseInt(x.trim(), 10)));
  return [];
}

// Split a columns container's inner lines into per-column line lists by the
// `:::col … :::` markers (3-colon depth-tracked so nested :::tip closes right).
function _splitColBlocks(lines: string[]): string[][] {
  const cols: string[][] = [];
  let i = 0;
  while (i < lines.length) {
    if (!_COL_ITEM_OPEN.test(lines[i].trim())) { i++; continue; }
    i++;
    const body: string[] = [];
    let depth = 1;
    while (i < lines.length) {
      const s = lines[i].trim();
      if (_CLOSE.test(s)) { depth--; if (depth === 0) { i++; break; } body.push(lines[i]); i++; continue; }
      if (_OPEN.test(s) || _COL_ITEM_OPEN.test(s)) depth++;
      body.push(lines[i]); i++;
    }
    cols.push(body);
  }
  return cols;
}

// Render a column body: reuse the directive/inline splitter so nested
// `:::tip`/`{color}`/`==mark==` render; fall back to a plain paragraph.
function _colInnerNodes(lines: string[]): any[] {
  const split = _splitParagraphText(lines.join("\n"));
  if (split && split.length) return split;
  const clean = [...lines];
  while (clean.length && clean[0].trim() === "") clean.shift();
  while (clean.length && clean[clean.length - 1].trim() === "") clean.pop();
  return clean.length ? [_textParagraph(clean)] : [];
}

// Build a columns container mdast node -> flex <section> row of column
// <section>s, matching the backend HTML exactly.
function _columnsNode(param: string, innerLines: string[]): any {
  const blocks = _splitColBlocks(innerLines);
  const ratios = _parseColRatios(param);
  const cells = blocks.map((body, idx) => ({
    type: "containerDirective",
    data: { hName: "section",
      hProperties: { style: `flex:${idx < ratios.length ? ratios[idx] : 1};min-width:0;` } },
    children: _colInnerNodes(body),
  }));
  return { type: "containerDirective",
    data: { hName: "section", hProperties: { style: _COLUMNS_ROW_STYLE } },
    children: cells };
}

function _splitParagraphText(text: string): any[] | null {
  const lines = text.split("\n");
  const out: any[] = [];
  let buf: string[] = [];
  let found = false;
  const flushText = () => {
    while (buf.length && buf[0].trim() === "") buf.shift();
    while (buf.length && buf[buf.length - 1].trim() === "") buf.pop();
    if (buf.length) out.push(_textParagraph(buf));
    buf = [];
  };
  for (let i = 0; i < lines.length; i++) {
    // ── multi-column container (::::columns … ::::) ──
    const colm = lines[i].match(_COL_CONTAINER_OPEN);
    if (colm) {
      let j = i + 1;
      let depth = 1;
      const inner: string[] = [];
      while (j < lines.length) {
        if (_COL_CONTAINER_CLOSE.test(lines[j])) { depth--; if (depth === 0) break; inner.push(lines[j]); j++; continue; }
        if (_COL_CONTAINER_OPEN.test(lines[j])) depth++;
        inner.push(lines[j]); j++;
      }
      if (j < lines.length) { // found matching :::: close
        found = true;
        flushText();
        out.push(_columnsNode(colm[1], inner));
        i = j;
        continue;
      }
      // no closing :::: — fall through and treat the open line as plain text
    }
    const om = lines[i].match(_OPEN);
    if (om) {
      let j = i + 1;
      const inner: string[] = [];
      while (j < lines.length && !_CLOSE.test(lines[j])) { inner.push(lines[j]); j++; }
      if (j < lines.length) { // found matching close line
        found = true;
        flushText();
        // Open-line arg is style params only when it contains '='; otherwise
        // treat it as the first body line (one-liner like ":::tip 文字").
        const openArg = (om[2] || "").trim();
        const isParams = openArg.includes("=");
        const body = isParams ? inner : (openArg ? [openArg, ...inner] : inner);
        out.push(_directiveNode(om[1], body.join("\n"), isParams ? openArg : ""));
        i = j;
        continue;
      }
      // no closing ::: — fall through and treat the open line as plain text
    }
    buf.push(lines[i]);
  }
  flushText();
  return found ? out : null;
}

// Preview-only remark plugin: render the app's custom directives so the live
// preview matches published output — inline `{color:..}{/color}` and `==mark==`,
// and block `:::type ... :::` containers (center/right/tip/info/...). Produces
// real hast elements via data.hName/hProperties (no raw HTML). The source
// markdown keeps the original syntax (backend renderers depend on it); this
// only transforms the editor live preview.
export function remarkAppDirectives() {
  const inlineVisit = (node: any) => {
    if (!node || !Array.isArray(node.children)) return;
    const next: any[] = [];
    for (const child of node.children) {
      if (child.type === "text" && typeof child.value === "string"
          && (child.value.includes("{color:") || child.value.includes("=="))) {
        next.push(..._inlineNodes(child.value));
      } else {
        inlineVisit(child);
        next.push(child);
      }
    }
    node.children = next;
  };
  return (tree: any) => {
    if (Array.isArray(tree.children)) {
      const next: any[] = [];
      for (const child of tree.children) {
        // Only paragraphs can contain the directive text. Scan each paragraph's
        // flattened text; if it holds a `:::type ... :::` block (mid-text or as
        // its own paragraph), replace it with the split node list.
        if (child.type === "paragraph") {
          const replaced = _splitParagraphText(_nodeText(child));
          if (replaced) { next.push(...replaced); continue; }
        }
        next.push(child);
      }
      tree.children = next;
    }
    inlineVisit(tree);
  };
}

// Extra react-markdown component overrides used by the live preview.
export const previewComponents = {
  // The rewriter emits each video as an <iframe> plus a redundant
  // `[▶ 视频链接](url)` fallback link. The iframe already renders the video,
  // so hide the trailing link.
  a: ({ children, ...props }: any) => {
    const text = String(Array.isArray(children) ? children.join("") : children ?? "");
    if (text.trim().startsWith("▶")) return null;
    return <a {...props}>{children}</a>;
  },
};
