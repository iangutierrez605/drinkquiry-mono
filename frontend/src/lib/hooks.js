// Shared React hooks (Handoff #15 §F4).

import { useEffect, useState } from "react";

/**
 * Debounce a value; search inputs wait ~300ms of quiet before firing.
 * Lived inside ModeratePage's Library tab since #9 — hoisted here verbatim
 * (zero behavior change) now that the browse grid, the host create screen
 * and /create all debounce their server-side searches too.
 */
export function useDebounced(value, ms = 300) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return debounced;
}
