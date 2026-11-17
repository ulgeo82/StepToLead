"use client";

import { useCallback, useEffect, useState } from "react";
import { inboxRequest } from "@/app/admin/outreach/inbox/inbox-api";
import type { InboxCounts } from "@/app/admin/outreach/inbox/inbox-types";
import { countChangedEvent, inboxRoot } from "@/app/admin/outreach/inbox/inbox-utils";
import { useVisiblePoll } from "@/app/admin/outreach/inbox/visible-poll";
import css from "@/app/admin/outreach/inbox/inbox.module.css";

export default function InboxCounter() {
  const [counts, setCounts] = useState<InboxCounts | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const refresh = () => setRevision(value => value + 1);
    window.addEventListener(countChangedEvent, refresh);
    return () => window.removeEventListener(countChangedEvent, refresh);
  }, []);
  const load = useCallback(async (signal: AbortSignal) => {
    try {
      const value = await inboxRequest<InboxCounts>(`${inboxRoot}/inbox-counter`, null, { signal });
      if (!signal.aborted) setCounts(value);
    } catch { /* The navigation remains available when the count endpoint is unavailable. */ }
  // A local mutation refreshes the count immediately rather than waiting for the next interval.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revision]);
  useVisiblePoll(load, 30_000);
  return counts != null && counts.unhandled > 0 ? <span className={css.menuCount}
    aria-label={`Не разобрано: ${counts.unhandled}; горячих: ${counts.hot}`}>
    {counts.unhandled > 99 ? "99+" : counts.unhandled}
  </span> : null;
}
