import type { ReactNode } from "react";

export type SessionSidebarProps = {
  children: ReactNode;
  className?: string;
  ariaLabel?: string;
};

/** Shared session navigation boundary for Desktop and Remote shells. */
export default function SessionSidebar({ children, className, ariaLabel }: SessionSidebarProps) {
  return <aside className={["session-sidebar-frame", className].filter(Boolean).join(" ")} aria-label={ariaLabel}>{children}</aside>;
}
