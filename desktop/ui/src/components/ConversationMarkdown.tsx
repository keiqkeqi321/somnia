import { Fragment, type CSSProperties, type ReactNode } from "react";

export type ConversationMarkdownProps = {
  text: string;
  className?: string;
  renderMermaid?: (source: string) => ReactNode;
};

/** Shared Markdown renderer for Desktop and Remote conversation messages. */
export default function ConversationMarkdown({ text, className = "markdown-content", renderMermaid }: ConversationMarkdownProps) {
  return <div className={className}>{renderMarkdownBlocks(text, renderMermaid)}</div>;
}

function renderMarkdownBlocks(text: string, renderMermaid?: (source: string) => ReactNode): ReactNode[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let index = 0;
  let key = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fenceMatch = line.match(/^\s*```([^`]*)\s*$/);
    if (fenceMatch) {
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const language = fenceMatch[1].trim();
      const code = codeLines.join("\n");
      if (/^mermaid\b/i.test(language) && renderMermaid) {
        blocks.push(<Fragment key={`block-${key++}`}>{renderMermaid(code)}</Fragment>);
      } else {
        blocks.push(<pre key={`block-${key++}`} className="markdown-code-block">{language ? <span className="markdown-code-language">{language}</span> : null}<code>{code}</code></pre>);
      }
      continue;
    }

    if (/^\s*---+\s*$/.test(line)) {
      blocks.push(<hr key={`block-${key++}`} />);
      index += 1;
      continue;
    }

    const headingMatch = line.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      const Tag = `h${Math.min(headingMatch[1].length, 6)}` as keyof JSX.IntrinsicElements;
      blocks.push(<Tag key={`block-${key++}`}>{renderInlineMarkdown(headingMatch[2])}</Tag>);
      index += 1;
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quoteLines: string[] = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quoteLines.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      blocks.push(<blockquote key={`block-${key++}`}>{renderInlineMarkdown(quoteLines.join("\n"))}</blockquote>);
      continue;
    }

    if (isMarkdownTableHeader(line, lines[index + 1])) {
      const headerCells = splitMarkdownTableRow(line);
      const alignments = parseMarkdownTableAlignments(lines[index + 1]);
      const bodyRows: string[][] = [];
      index += 2;
      while (index < lines.length && isMarkdownTableRow(lines[index])) bodyRows.push(splitMarkdownTableRow(lines[index++]));
      blocks.push(<div key={`block-${key++}`} className="markdown-table-wrap"><table className="markdown-table"><thead><tr>{headerCells.map((cell, cellIndex) => <th key={`head-${cellIndex}`} style={tableCellStyle(alignments[cellIndex])}>{renderInlineMarkdown(cell)}</th>)}</tr></thead><tbody>{bodyRows.map((row, rowIndex) => <tr key={`row-${rowIndex}`}>{headerCells.map((_, cellIndex) => <td key={`cell-${rowIndex}-${cellIndex}`} style={tableCellStyle(alignments[cellIndex])}>{renderInlineMarkdown(row[cellIndex] ?? "")}</td>)}</tr>)}</tbody></table></div>);
      continue;
    }

    if (/^(\s*)[-*+]\s+(.+)$/.test(line)) {
      const items: ReactNode[] = [];
      while (index < lines.length) {
        const itemMatch = lines[index].match(/^\s*[-*+]\s+(.+)$/);
        if (!itemMatch) break;
        items.push(<li key={`item-${items.length}`}>{renderInlineMarkdown(itemMatch[1])}</li>);
        index += 1;
      }
      blocks.push(<ul key={`block-${key++}`}>{items}</ul>);
      continue;
    }

    if (/^(\s*)\d+[.)]\s+(.+)$/.test(line)) {
      const items: ReactNode[] = [];
      while (index < lines.length) {
        const itemMatch = lines[index].match(/^\s*\d+[.)]\s+(.+)$/);
        if (!itemMatch) break;
        items.push(<li key={`item-${items.length}`}>{renderInlineMarkdown(itemMatch[1])}</li>);
        index += 1;
      }
      blocks.push(<ol key={`block-${key++}`}>{items}</ol>);
      continue;
    }

    const paragraphLines = [line.trim()];
    index += 1;
    while (index < lines.length && lines[index].trim() && !/^\s*```/.test(lines[index]) && !/^\s*---+\s*$/.test(lines[index]) && !/^(#{1,6})\s+/.test(lines[index]) && !/^\s*>\s?/.test(lines[index]) && !/^\s*[-*+]\s+/.test(lines[index]) && !/^\s*\d+[.)]\s+/.test(lines[index])) {
      paragraphLines.push(lines[index].trim());
      index += 1;
    }
    blocks.push(<p key={`block-${key++}`}>{renderInlineMarkdown(paragraphLines.join(" "))}</p>);
  }
  return blocks;
}

function renderInlineMarkdown(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|_[^_\s][^_]*_|\[[^\]]+\]\([^)]+\))/g;
  let cursor = 0;
  let key = 0;
  for (const match of text.matchAll(pattern)) {
    const start = match.index ?? 0;
    if (start > cursor) nodes.push(text.slice(cursor, start));
    const token = match[0];
    if (token.startsWith("`")) nodes.push(<code key={`inline-${key++}`}>{token.slice(1, -1)}</code>);
    else if (token.startsWith("**") || token.startsWith("__")) nodes.push(<strong key={`inline-${key++}`}>{renderInlineMarkdown(token.slice(2, -2))}</strong>);
    else if (token.startsWith("*") || token.startsWith("_")) nodes.push(<em key={`inline-${key++}`}>{renderInlineMarkdown(token.slice(1, -1))}</em>);
    else {
      const linkMatch = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      nodes.push(linkMatch ? <a key={`inline-${key++}`} href={linkMatch[2]} target="_blank" rel="noreferrer">{renderInlineMarkdown(linkMatch[1])}</a> : token);
    }
    cursor = start + token.length;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

type MarkdownTableAlignment = "left" | "center" | "right" | null;
function isMarkdownTableHeader(line: string, nextLine: string | undefined): boolean {
  if (!nextLine || !isMarkdownTableRow(line)) return false;
  const headerCells = splitMarkdownTableRow(line);
  const dividerCells = splitMarkdownTableRow(nextLine);
  return headerCells.length >= 2 && headerCells.length === dividerCells.length && dividerCells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
}
function isMarkdownTableRow(line: string): boolean {
  const trimmed = line.trim();
  const cells = splitMarkdownTableRow(trimmed);
  return Boolean(trimmed && trimmed.includes("|") && cells.length >= 2 && cells.some((cell) => cell.trim()));
}
function splitMarkdownTableRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const cells: string[] = [];
  let current = "";
  for (let index = 0; index < trimmed.length; index += 1) {
    const character = trimmed[index];
    if (character === "\\" && index + 1 < trimmed.length && (trimmed[index + 1] === "|" || trimmed[index + 1] === "\\")) {
      current += trimmed[++index];
    } else if (character === "|") {
      cells.push(current.trim()); current = "";
    } else current += character;
  }
  cells.push(current.trim());
  return cells;
}
function parseMarkdownTableAlignments(line: string): MarkdownTableAlignment[] {
  return splitMarkdownTableRow(line).map((cell) => {
    const trimmed = cell.trim();
    if (trimmed.startsWith(":") && trimmed.endsWith(":")) return "center";
    if (trimmed.endsWith(":")) return "right";
    return trimmed.startsWith(":") ? "left" : null;
  });
}
function tableCellStyle(alignment: MarkdownTableAlignment): CSSProperties | undefined {
  return alignment ? { textAlign: alignment } : undefined;
}
