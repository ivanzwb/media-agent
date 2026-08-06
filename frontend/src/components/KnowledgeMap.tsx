import DOMPurify from "dompurify";
import { marked } from "marked";
import { useEffect, useMemo, useState } from "react";

// The rendered SVG is injected without a second sanitising pass. Mermaid's own
// securityLevel:"strict" scrubs label markup on the way out, and running the
// result through DOMPurify again would blank the nodes: mermaid v11 always
// renders node labels as <foreignObject> regardless of htmlLabels:false, and
// DOMPurify removes foreignObject contents by default.
//
// Mermaid is large and only ever needed on the two series pages, so it is
// fetched on first render rather than bundled into the initial load.
let pending: Promise<typeof import("mermaid").default> | null = null;

function loadMermaid() {
  if (!pending) {
    pending = import("mermaid").then((module) => {
      module.default.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: "neutral",
        // Labels as SVG text, not foreignObject: sanitising the result drops
        // foreign content, which would leave every node blank.
        flowchart: { htmlLabels: false, curve: "basis", useMaxWidth: true },
        fontFamily: '-apple-system, "PingFang SC", "Microsoft YaHei", sans-serif',
      });
      return module.default;
    });
  }
  return pending;
}

export function isDiagram(source?: string | null) {
  return /^\s*(?:graph|flowchart)\b/i.test(source || "");
}

let sequence = 0;

/** The field's shape, as a diagram when it is one and as text when it is not.
 *
 * Series planned before the map became a diagram hold a nested Markdown list,
 * and a diagram the renderer chokes on still says something as source, so
 * neither case is allowed to leave the panel empty.
 */
export default function KnowledgeMap({ source }: { source?: string | null }) {
  const text = (source || "").trim();
  const diagram = isDiagram(text);
  const [svg, setSvg] = useState("");
  const [broken, setBroken] = useState(false);

  useEffect(() => {
    if (!diagram) return;
    let dropped = false;
    setSvg("");
    setBroken(false);
    loadMermaid()
      .then((mermaid) => mermaid.render(`ma-map-${sequence += 1}`, text))
      .then((result) => { if (!dropped) setSvg(result.svg); })
      .catch(() => { if (!dropped) setBroken(true); });
    return () => { dropped = true; };
  }, [text, diagram]);

  const markdown = useMemo(() => (
    diagram || !text
      ? ""
      : DOMPurify.sanitize(marked.parse(text, { async: false }) as string)
  ), [text, diagram]);

  if (!text) return null;
  if (!diagram) {
    return <div className="ma-knowledge-map"
      dangerouslySetInnerHTML={{ __html: markdown }} />;
  }
  if (broken) return <pre className="ma-knowledge-source">{text}</pre>;
  if (!svg) return <div className="ma-knowledge-map">正在绘制…</div>;
  return <div className="ma-mermaid" dangerouslySetInnerHTML={{ __html: svg }} />;
}
