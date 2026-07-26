import { forwardRef, type CSSProperties, type ReactNode } from "react";

export type ConversationWorkspaceProps = {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
  as?: "section" | "div" | "main";
};

/** Shared workspace frame; shells provide session, conversation, progress, and composer regions. */
const ConversationWorkspace = forwardRef<HTMLElement, ConversationWorkspaceProps>(function ConversationWorkspace({ children, className, style, as = "section" }, ref) {
  const Component = as === "main" ? "main" : as === "div" ? "div" : "section";
  return <Component ref={ref as never} style={style} className={["conversation-workspace-frame", className].filter(Boolean).join(" ")}>{children}</Component>;
});

export default ConversationWorkspace;
