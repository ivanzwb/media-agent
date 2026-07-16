// Live-preview helpers for the draft editor's Markdown preview.
//
// The app uses a few custom syntaxes on top of Markdown:
//   inline: {color:#hex}text{/color}  and  ==highlight==
//   block:  :::type ... :::  (center / right / tip / info / warning / success /
//           danger / highlight)
// The SOURCE markdown must keep this exact syntax (backend renderers depend on
// it). These helpers only transform the editor's live preview so the author sees
// styled output instead of literal directive text.

// Flatten an mdast node's text content (soft breaks -> \n).
function _nodeText(n: any): string {
  if (!n) return "";
  if (n.type === "text") return n.value || "";
  if (n.type === "break") return "\n";
  if (Array.isArray(n.children)) return n.children.map(_nodeText).join("");
  return "";
}

// Split text into mdast nodes, turning the app's inline directives into real
// hast elements via data.hName/hProperties: {color:HEX}..{/color} -> <span
// style=color:..> and ==text== -> <mark>. Using hName (not raw HTML) means the
// preview renders these as proper elements regardless of raw-HTML settings.
function _inlineNodes(text: string): any[] {
  const RE = /\{color:(#[0-9a-fA-F]{3,8}|[a-zA-Z][\w-]*)\}([\s\S]*?)\{\/color\}|==([^=]+?)==/g;
  const nodes: any[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = RE.exec(text)) !== null) {
    if (m.index > last) nodes.push({ type: "text", value: text.slice(last, m.index) });
    if (m[1] !== undefined) {
      nodes.push({ type: "textDirective",
        data: { hName: "span", hProperties: { style: `color:${m[1]}` } },
        children: [{ type: "text", value: m[2] }] });
    } else {
      nodes.push({ type: "textDirective", data: { hName: "mark" },
        children: [{ type: "text", value: m[3] }] });
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

// Build a block-container mdast node (rendered as a styled <div> via hName).
function _directiveNode(type: string, inner: string): any {
  const lines = inner.split(/\n+/).map((s) => s.trim()).filter(Boolean);
  const kids: any[] = [];
  lines.forEach((line, i) => {
    if (i > 0) kids.push({ type: "break" });
    kids.push(..._inlineNodes(line));
  });
  const t = type.toLowerCase();
  let style: string;
  if (t === "center") style = "text-align:center";
  else if (t === "right") style = "text-align:right";
  else {
    const [bg, border, color] = _DIRECTIVE_PALETTE[t] || ["#f7f7f7", "#d9d9d9", "#333"];
    style = `background:${bg};border-left:4px solid ${border};color:${color};`
      + "padding:10px 14px;border-radius:4px;margin:12px 0;";
  }
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
const _OPEN = /^:::([a-zA-Z]+)[ \t]*$/;
const _CLOSE = /^:::[ \t]*$/;
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
    const om = lines[i].match(_OPEN);
    if (om) {
      let j = i + 1;
      const inner: string[] = [];
      while (j < lines.length && !_CLOSE.test(lines[j])) { inner.push(lines[j]); j++; }
      if (j < lines.length) { // found matching close line
        found = true;
        flushText();
        out.push(_directiveNode(om[1], inner.join("\n")));
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
