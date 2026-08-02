// Renders a ```mermaid fenced block in the draft editor's live preview.
//
// The pipeline now asks the model to insert mermaid diagrams into search /
// series drafts. The preview must show them rendered, not as raw source.
// Mermaid is large and only ever needed on demand, so it is fetched lazily on
// first render (same pattern as KnowledgeMap). When rendering fails the block
// degrades to a plain <pre><code> so the author can still read / fix the
// source. Export to WeChat PNGs is a separate concern (mermaidExport.ts) and
// must not touch this component.
import DOMPurify from "dompurify";
import { useEffect, useRef, useState } from "react";

// One shared lazy promise per app run — mirrors KnowledgeMap's loadMermaid.
// htmlLabels:false keeps labels as SVG <text>, which survives DOMPurify
// sanitising (foreignObject content would be dropped and nodes go blank).
let pending: Promise<typeof import("mermaid").default> | null = null;

function loadMermaid() {
  if (!pending) {
    pending = import("mermaid").then((module) => {
      module.default.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: "neutral",
        flowchart: { htmlLabels: false, curve: "basis", useMaxWidth: true },
        fontFamily: '-apple-system, "PingFang SC", "Microsoft YaHei", sans-serif',
      });
      return module.default;
    });
  }
  return pending;
}

let sequence = 0;

export default function MermaidBlock({ code }: { code: string }) {
  const [svg, setSvg] = useState("");
  const [broken, setBroken] = useState(false);
  const renderId = useRef(`ma-md-${(sequence += 1)}`).current;

  useEffect(() => {
    let dropped = false;
    setSvg("");
    setBroken(false);
    loadMermaid()
      .then((mermaid) => mermaid.render(renderId, code))
      .then((result) => { if (!dropped) setSvg(DOMPurify.sanitize(result.svg)); })
      .catch(() => { if (!dropped) setBroken(true); });
    return () => { dropped = true; };
  }, [code, renderId]);

  if (broken) {
    return <pre className="ma-knowledge-source"><code>{code}</code></pre>;
  }
  if (!svg) return <div className="ma-mermaid">正在绘制流程图…</div>;
  return <div className="ma-mermaid" dangerouslySetInnerHTML={{ __html: svg }} />;
}
