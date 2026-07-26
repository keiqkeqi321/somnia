import type { ReactNode } from "react";

export type ContextPanelProps = {
  children: ReactNode;
  className?: string;
  ariaLabel?: string;
};

/** Shared session/context inspector boundary for Desktop and Remote shells. */
export default function ContextPanel({ children, className, ariaLabel }: ContextPanelProps) {
  return <aside className={["context-panel-frame", className].filter(Boolean).join(" ")} aria-label={ariaLabel}>{children}</aside>;
}
