import { useEffect, useState } from "react";

/**
 * Subscribe to a CSS media query from JS.
 *
 * Used by the mobile drawer layout to toggle drawer classes and auto-close
 * drawers on selection. Server-safe: returns false when window is undefined.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(query).matches
      : false,
  );

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mql = window.matchMedia(query);
    const handler = (event: MediaQueryListEvent) => setMatches(event.matches);
    mql.addEventListener("change", handler);
    setMatches(mql.matches);
    return () => mql.removeEventListener("change", handler);
  }, [query]);

  return matches;
}

/** True when the viewport is at or below the mobile breakpoint (900px). */
export function useIsMobile(): boolean {
  return useMediaQuery("(max-width: 900px)");
}
