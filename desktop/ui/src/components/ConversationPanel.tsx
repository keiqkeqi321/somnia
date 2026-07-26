import type { ReactNode } from "react";

export type ConversationPanelProps = {
  children: ReactNode;
  className?: string;
  ariaLabel?: string;
};

/** Shared conversation panel boundary for Desktop and Remote shells. */
export default function ConversationPanel({ children, className, ariaLabel }: ConversationPanelProps) {
  return <section className={["conversation-panel-frame", className].filter(Boolean).join(" ")} aria-label={ariaLabel}>{children}</section>;
}
