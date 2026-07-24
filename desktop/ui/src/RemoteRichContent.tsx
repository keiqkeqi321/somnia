import { useEffect, useId, useState } from "react";

import type { ConversationImageReferenceBlock, ConversationRow, ConversationRowPart } from "./types";

type ImageResolver = (path: string) => Promise<string>;

export function RemoteConversationRow({ row, resolveImage }: { row: ConversationRow; resolveImage?: ImageResolver }) {
  const parts = row.parts ?? (row.text ? [{ id: `${row.id}-text`, type: "text" as const, text: row.text }] : []);
  return (
    <article className={`remote-message remote-message-${row.role}${row.isStreaming ? " remote-message-streaming" : ""}`}>
      <span>{row.role}</span>
      {parts.map((part) => <RemotePart key={part.id} part={part} resolveImage={resolveImage} />)}
      {row.images?.map((image, index) => <RemoteImage key={`${row.id}-image-${index}`} image={image} resolveImage={resolveImage} />)}
    </article>
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
  const blocks = splitFences(text);
  return <div className="remote-markdown">{blocks.map((block, index) => block.kind === "code" ? (block.language === "mermaid" ? <RemoteMermaid key={index} source={block.value} /> : <pre className="remote-code" key={index}><span>{block.language}</span><code>{block.value}</code></pre>) : <MarkdownText key={index} text={block.value} />)}</div>;
}

function MarkdownText({ text }: { text: string }) {
  return <>{text.split(/\n{2,}/).filter(Boolean).map((paragraph, index) => {
    const heading = /^(#{1,6})\s+(.+)$/.exec(paragraph);
    if (heading) return <h3 key={index}>{heading[2]}</h3>;
    const lines = paragraph.split("\n");
    if (lines.every((line) => /^[-*]\s+/.test(line))) return <ul key={index}>{lines.map((line) => <li key={line}>{inlineMarkdown(line.replace(/^[-*]\s+/, ""))}</li>)}</ul>;
    return <p key={index}>{lines.map((line, lineIndex) => <span key={lineIndex}>{inlineMarkdown(line)}{lineIndex < lines.length - 1 ? <br /> : null}</span>)}</p>;
  })}</>;
}

function inlineMarkdown(text: string) {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`|!\[[^\]]*\]\([^\s)]+\)|\[[^\]]+\]\([^\s)]+\))/g).filter(Boolean).map((token, index) => {
    let match = /^\*\*(.+)\*\*$/.exec(token); if (match) return <strong key={index}>{match[1]}</strong>;
    match = /^`(.+)`$/.exec(token); if (match) return <code key={index}>{match[1]}</code>;
    match = /^!\[([^\]]*)\]\(([^)]+)\)$/.exec(token); if (match && safeImageUrl(match[2])) return <img key={index} src={match[2]} alt={match[1]} loading="lazy" />;
    match = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token); if (match && /^https?:\/\//.test(match[2])) return <a key={index} href={match[2]} target="_blank" rel="noreferrer">{match[1]}</a>;
    return token;
  });
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

function splitFences(text: string): Array<{ kind: "text" | "code"; language: string; value: string }> {
  const blocks: Array<{ kind: "text" | "code"; language: string; value: string }> = [];
  const pattern = /```([^\n`]*)\n([\s\S]*?)```/g;
  let offset = 0; let match: RegExpExecArray | null;
  while ((match = pattern.exec(text))) { if (match.index > offset) blocks.push({ kind: "text", language: "", value: text.slice(offset, match.index) }); blocks.push({ kind: "code", language: match[1].trim().toLowerCase(), value: match[2].trimEnd() }); offset = pattern.lastIndex; }
  if (offset < text.length) blocks.push({ kind: "text", language: "", value: text.slice(offset) });
  return blocks;
}

function safeImageUrl(url: string): boolean { return /^(https?:|data:image\/)/i.test(url); }
