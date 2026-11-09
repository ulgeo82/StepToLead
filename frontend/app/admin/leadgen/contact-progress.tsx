"use client";

import { TaskProgress } from "@/components/task-progress";
import { base } from "./shared";

export type ContactRun = { id: number; since: number; active: boolean };

export function ContactProgress({ run, finished }: { run: ContactRun | null; finished: () => void }) {
  if (!run) return null;
  return <TaskProgress key={run.id} url={`${base}/runs/${run.id}/progress`}
    active={run.active} since={run.since} onFinish={finished}/>;
}
