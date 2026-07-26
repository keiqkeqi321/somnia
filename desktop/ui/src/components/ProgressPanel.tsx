import type { ReactNode } from "react";

export type ProgressPanelProps = {
  children: ReactNode;
  className?: string;
  ariaLabel?: string;
  as?: "aside" | "div";
};

/** Shared runtime progress boundary for tool, task, worker, and context activity. */
export default function ProgressPanel({ children, className, ariaLabel, as = "aside" }: ProgressPanelProps) {
  const Component = as;
  return <Component className={["progress-panel-frame", className].filter(Boolean).join(" ")} aria-label={ariaLabel}>{children}</Component>;
}
