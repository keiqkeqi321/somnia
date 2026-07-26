import { forwardRef, type ReactNode } from "react";

export type ConversationMessageListProps = {
  children: ReactNode;
  className?: string;
};

/** Shared message stream boundary; shells own scroll anchoring refs and row rendering. */
const ConversationMessageList = forwardRef<HTMLDivElement, ConversationMessageListProps>(function ConversationMessageList({ children, className }, ref) {
  return <div ref={ref} className={["conversation-message-list", className].filter(Boolean).join(" ")}>{children}</div>;
});

export default ConversationMessageList;
