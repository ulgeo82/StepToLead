export type CampaignStats = {
  leads: number; waiting: number; sent: number; contacted: number; replied: number; reply_rate: number;
  interested: number; hot: number; unsubscribed: number; bounced: number; finished: number;
  by_status: Record<string, number>;
};
export type Campaign = {
  id: number; name: string; status: "draft" | "active" | "paused" | "done"; sequence_id: number; steps: number;
  mailbox_ids: number[]; variables: string[]; start_at: string | null; created_at: string | null;
  window: { tz: string; start_hour: number; end_hour: number; weekdays: number[]; gap_minutes: number; daily_new: number };
  stats: CampaignStats | null;
};
export type Column = { index: number; header: string; sample: string[]; filled: number; field: string; var?: string };
export type UploadPreview = { columns: Column[]; rows: number; fields: Record<string, string>; sample: Record<string, string>[] };
export type ImportReport = {
  added: number; skipped: number; reasons: Record<string, number>; warnings: string[];
  skipped_rows: { line?: number; email?: string | null; company?: string; reason_text: string }[];
  campaign?: Campaign;
};
export type Lead = {
  id: number; company_id: number; company: string; domain: string | null; email: string | null; status: string;
  status_name: string; step: number; next_at: string | null; stop_reason: string | null; mailbox: string | null;
  vars: Record<string, string>; crm_deal_id: number | null;
};
export type Check = {
  leads: number; missing: { variable: string; leads: number }[]; problems: string[];
  daily_capacity: number; days_estimate: number | null;
};

export const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
export function scheduleText(w: Campaign["window"], zoneName?: string): string {
  const days = [...w.weekdays].sort().join(",") === "0,1,2,3,4" ? "Пн–Пт" : w.weekdays.length === 7 ? "каждый день"
    : [...w.weekdays].sort().map((d) => WEEKDAYS[d]).join(", ");
  return `${days}, ${w.start_hour}:00–${w.end_hour}:00 · ${zoneName || w.tz}`;
}
export const STATUS: Record<Campaign["status"], { name: string; tone: "fitYes" | "fitMaybe" | "fitNo" | "" }> = {
  draft: { name: "Черновик", tone: "" },
  active: { name: "Идёт", tone: "fitYes" },
  paused: { name: "На паузе", tone: "fitMaybe" },
  done: { name: "Завершена", tone: "fitNo" },
};
export const STOP_REASONS: Record<string, string> = {
  reply: "ответил", company_replied: "ответил коллега из компании", unsubscribed: "отписался", bounce: "адрес не существует",
  recipient_refused: "адрес не существует", manual: "убран вручную", campaign_done: "кампания завершена",
};
