import type { ReactNode } from "react";
import type { ConversationRow } from "../types";

export type ConversationMessageRowProps = {
  row: ConversationRow;
  children: ReactNode;
  className?: string;
  showRole?: boolean;
};

/** Shared message row container; clients provide rich part rendering as children. */
export default function ConversationMessageRow({ row, children, className, showRole = false }: ConversationMessageRowProps) {
  return <article className={["conversation-message-row", `conversation-message-row-${row.role}`, row.isStreaming ? "conversation-message-row-streaming" : "", className].filter(Boolean).join(" ")}>{showRole ? <span>{row.role}</span> : null}{children}</article>;
}
