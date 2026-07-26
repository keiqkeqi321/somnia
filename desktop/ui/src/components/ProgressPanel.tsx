import type { ReactNode } from "react";

export type ProgressPanelProps = {
  children: ReactNode;
  className?: string;
  ariaLabel?: string;
};

/** Shared runtime progress boundary for tool, task, worker, and context activity. */
export default function ProgressPanel({ children, className, ariaLabel }: ProgressPanelProps) {
  return <aside className={["progress-panel-frame", className].filter(Boolean).join(" ")} aria-label={ariaLabel}>{children}</aside>;
}
