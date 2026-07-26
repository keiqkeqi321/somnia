import type { ReactNode } from "react";

export type ConversationWorkspaceProps = {
  children: ReactNode;
  className?: string;
};

/** Shared workspace frame; shells provide session, conversation, progress, and composer regions. */
export default function ConversationWorkspace({ children, className }: ConversationWorkspaceProps) {
  return <section className={["conversation-workspace", className].filter(Boolean).join(" ")}>{children}</section>;
}
