import type { ReactNode } from "react";

export type SessionSidebarProps = {
  children: ReactNode;
  className?: string;
  ariaLabel?: string;
  as?: "aside" | "div";
};

/** Shared session navigation boundary for Desktop and Remote shells. */
export default function SessionSidebar({ children, className, ariaLabel, as = "aside" }: SessionSidebarProps) {
  const Component = as;
  return <Component className={["session-sidebar-frame", className].filter(Boolean).join(" ")} aria-label={ariaLabel}>{children}</Component>;
}
