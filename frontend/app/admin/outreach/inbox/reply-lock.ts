"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { inboxMutation } from "./inbox-api";
import type { ReplyLock } from "./inbox-types";

// A late release from a previous editor must not delete a newly acquired lock on the same conversation.
const queues = new Map<string, Promise<unknown>>();
function serial<T>(key: string, action: () => Promise<T>): Promise<T> {
  const next = (queues.get(key) ?? Promise.resolve()).catch(() => undefined).then(action);
  queues.set(key, next);
  const cleanup = () => { if (queues.get(key) === next) queues.delete(key); };
  void next.then(cleanup, cleanup);
  return next;
}

export function useReplyLock(path: string, workspace: number | null, writing: boolean) {
  const [until, setUntil] = useState(0);
  const [error, setError] = useState("");
  const owned = useRef(false);
  const mounted = useRef(false);
  const url = `${path}/lock`;
  const key = `${workspace}:${url}`;

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const acquire = useCallback(async () => {
    try {
      const lease = await inboxMutation<ReplyLock>(url, workspace);
      owned.current = true;
      if (mounted.current) { setUntil(Date.parse(lease.until)); setError(""); }
    } catch (cause) {
      if (mounted.current) {
        setUntil(0);
        setError(cause instanceof Error ? cause.message : "Не удалось заблокировать редактор.");
      }
      throw cause;
    }
  }, [url, workspace]);

  useEffect(() => {
    if (!writing) return;
    let stopped = false;
    let pending = false;
    const renew = async () => {
      if (pending) return;
      pending = true;
      try { await serial(key, async () => { if (!stopped) await acquire(); }); }
      catch { /* The error is shown in the editor; the next renewal can recover after expiry. */ }
      finally { pending = false; }
    };
    void renew();
    const timer = setInterval(renew, 20_000);
    return () => {
      stopped = true;
      clearInterval(timer);
      setUntil(0);
      setError("");
      void serial(key, async () => {
        if (!owned.current) return;
        owned.current = false;
        await inboxMutation(url, workspace, "DELETE");
      }).catch(() => { /* A failed release expires on the server after 60 seconds. */ });
    };
  }, [acquire, key, url, workspace, writing]);

  const withLock = useCallback(<T,>(action: () => Promise<T>) => serial(key, async () => {
    await acquire();
    return action();
  }), [key, acquire]);
  return { until, error, withLock };
}
