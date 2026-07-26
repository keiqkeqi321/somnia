import { useEffect, useId, useState } from "react";

import type { ConversationImageReferenceBlock, ConversationRow, ConversationRowPart } from "./types";
import ConversationMessageRow from "./components/ConversationMessageRow";
import ConversationMarkdown from "./components/ConversationMarkdown";

type ImageResolver = (path: string) => Promise<string>;

export function RemoteConversationRow({ row, resolveImage }: { row: ConversationRow; resolveImage?: ImageResolver }) {
  const parts = row.parts ?? (row.text ? [{ id: `${row.id}-text`, type: "text" as const, text: row.text }] : []);
  return (
    <ConversationMessageRow row={row} showRole className={`remote-message remote-message-${row.role}${row.isStreaming ? " remote-message-streaming" : ""}`}>
      {parts.map((part) => <RemotePart key={part.id} part={part} resolveImage={resolveImage} />)}
      {row.images?.map((image, index) => <RemoteImage key={`${row.id}-image-${index}`} image={image} resolveImage={resolveImage} />)}
    </ConversationMessageRow>
  );
}

function RemotePart({ part, resolveImage }: { part: ConversationRowPart; resolveImage?: ImageResolver }) {
  if (part.type === "thinking_log") {
    return <details className="remote-detail-card"><summary>Thinking · {part.thinkingLog.status ?? "finished"}</summary><pre>{part.thinkingLog.text || part.thinkingLog.path || "Thinking details available"}</pre></details>;
  }
  if (part.type === "tool_call") {
    return <details className="remote-detail-card"><summary>{part.toolCall.name} · {part.toolCall.status ?? "finished"}</summary><strong>Input</strong><pre>{part.toolCall.input}</pre><strong>Output</strong><pre>{part.toolCall.output}</pre>{part.toolCall.contentBlocks?.filter((block) => block.type === "image_reference").map((image, index) => <RemoteImage key={`${part.id}-${index}`} image={image as ConversationImageReferenceBlock} resolveImage={resolveImage} />)}</details>;
  }
  return <RemoteMarkdown text={part.text} />;
}

export function RemoteMarkdown({ text }: { text: string }) {
  return <ConversationMarkdown className="markdown-content remote-markdown" text={text} renderMermaid={(source) => <RemoteMermaid source={source} />} />;
}

function RemoteImage({ image, resolveImage }: { image: ConversationImageReferenceBlock; resolveImage?: ImageResolver }) {
  const path = image.path ?? image.absolute_path;
  const [resolved, setResolved] = useState(image.image_url ?? "");
  useEffect(() => {
    let active = true;
    if (!resolved && path && resolveImage) void resolveImage(path).then((url) => { if (active) setResolved(url); }).catch(() => undefined);
    return () => { active = false; };
  }, [path, resolveImage, resolved]);
  return resolved && safeImageUrl(resolved) ? <img className="remote-content-image" src={resolved} alt={path ?? "Conversation image"} loading="lazy" /> : <span className="remote-image-reference">Image: {path ?? "unavailable"}</span>;
}

function RemoteMermaid({ source }: { source: string }) {
  const id = `remote-mermaid-${useId().replace(/:/g, "")}`;
  const [svg, setSvg] = useState("");
  useEffect(() => { let active = true; void import("mermaid").then(async ({ default: mermaid }) => { mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" }); const rendered = await mermaid.render(id, source); if (active) setSvg(rendered.svg); }).catch(() => { if (active) setSvg(""); }); return () => { active = false; }; }, [id, source]);
  return <figure className="remote-mermaid"><figcaption>Mermaid diagram</figcaption>{svg ? <div dangerouslySetInnerHTML={{ __html: svg }} /> : <pre className="remote-code"><code>{source}</code></pre>}</figure>;
}

function safeImageUrl(url: string): boolean { return /^(https?:|data:image\/)/i.test(url); }
