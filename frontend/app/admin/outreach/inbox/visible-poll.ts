"use client";

import { useEffect } from "react";

/** One request at a time; abort old results when filters/scope change or the tab becomes hidden. */
export function useVisiblePoll(load: (signal: AbortSignal) => Promise<void>, interval: number, delay = 0) {
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController | undefined;
    let disposed = false;
    const stop = () => { clearTimeout(timer); controller?.abort(); };
    const run = async () => {
      if (disposed || document.hidden) return;
      const request = new AbortController();
      controller = request;
      try { await load(request.signal); }
      finally {
        if (!disposed && !request.signal.aborted && !document.hidden) timer = setTimeout(run, interval);
      }
    };
    const visibility = () => { stop(); if (!document.hidden) void run(); };
    if (!document.hidden) timer = setTimeout(run, delay);
    document.addEventListener("visibilitychange", visibility);
    return () => { disposed = true; stop(); document.removeEventListener("visibilitychange", visibility); };
  }, [load, interval, delay]);
}
