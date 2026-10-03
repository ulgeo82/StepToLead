import { api } from "@/lib/api";

export type Project = { id: number; name: string; organization_name: string };
export type Contact = { id: number; name: string; phones: string[]; emails: string[]; telegram: string | null;
  company: string | null; position?: string | null; notes?: string | null; tags?: string[] };
export type Task = { id: number; deal_id: number | null; contact_id?: number | null; type_code: string; title: string;
  description?: string | null; due_at: string; status: string; result: string | null; priority: string;
  responsible_user_id?: number | null; deal_name?: string | null; contact_name?: string | null; responsible_name?: string | null };
export type Activity = { id: string | number; event_type: string; actor_name: string | null; payload: Record<string, unknown>; created_at: string };
export type Deal = { id: number; name: string; amount: number | null; contact: Contact; lead_id: number | null;
  stage_id: number; stage_name: string; pipeline_id: number; analytics_type: string; source_id: number | null;
  responsible_user_id: number | null; origin: string; source_name?: string | null; responsible_name?: string | null;
  custom_fields: Record<string, unknown>; attribution_snapshot: Record<string, unknown>; task_state: string;
  next_task: Task | null; created_at: string; archived_at?: string | null; tags: string[];
  days_in_stage: number | null; idle_days: number | null; first_response_at?: string | null;
  lost_reason_id?: number | null; lost_comment?: string | null; closed_at?: string | null;
  form_data?: Record<string, unknown> | null; tasks?: Task[]; activities?: Activity[];
  quality?: "target" | "non_target" | null; quality_reason?: string | null;
  sales?: { id: number; amount: number | null; occurred_at: string }[] };
export type Stage = { id: number; name: string; analytics_type: string; color: string; position?: number; required_fields: string[] };
export type Pipeline = { id: number; name: string; is_default?: boolean; stages: Stage[] };
export type Column = { stage: Stage; total: number; amount: number; deals: Deal[]; has_more: boolean };
export type Board = { pipeline: { id: number; name: string }; columns: Column[]; control: Record<string, number>;
  inbound_count: number; page: number };
export type Team = { id: number; display_name: string };
export type Source = { id: number; name: string };
export type CustomField = { id: number; key: string; name: string; field_type: string; options: string[] | null };
export type Inbound = { id: number; name: string | null; phone: string | null; email: string | null; status: string;
  received_at: string; raw_payload: Record<string, unknown>; attribution: Record<string, unknown>;
  potential_duplicates: { id: number; name: string }[] };

export const stateLabels: Record<string, string> = { OVERDUE: "Просрочено", NO_TASK: "Без задачи", TODAY: "На сегодня", PLANNED: "Запланировано" };
export const typeLabels: Record<string, string> = { CALL: "Позвонить", MEETING: "Встреча", MESSAGE: "Написать",
  SEND: "Отправить КП / документы", FOLLOW_UP: "Связаться повторно", OTHER: "Другое" };
export const priorityLabels: Record<string, string> = { HIGH: "Высокий", NORMAL: "Обычный", LOW: "Низкий" };
export const qualityReasons = ["Спам или ошибка", "Не та услуга", "Не наш регион", "Нет бюджета", "Дубль", "Не выходит на связь", "Другое"];
export const rejectReasons: Record<string, string> = { SPAM: "Спам", DUPLICATE: "Дубль", TEST: "Тестовая заявка",
  INVALID: "Некорректные данные", NOT_TARGET: "Нецелевой клиент", OTHER: "Другое" };

export const when = (value?: string | null) => value ? new Date(value).toLocaleString("ru-RU",
  { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
export const day = (value?: string | null) => value ? new Date(value).toLocaleDateString("ru-RU",
  { day: "numeric", month: "long" }) : "—";
export const plural = (n: number, one: string, few: string, many: string) => {
  const a = Math.abs(n) % 100, b = a % 10;
  return a > 10 && a < 20 ? many : b === 1 ? one : b >= 2 && b <= 4 ? few : many;
};
export const days = (n: number | null | undefined) => n == null ? "—" : n === 0 ? "сегодня" : `${n} ${plural(n, "день", "дня", "дней")}`;
export const request = <T,>(path: string, method: string, body?: unknown) => api<T>(path, { method,
  headers: body === undefined ? undefined : { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body) });

export const localInput = (date: Date) => {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
};
/** Quick due-date presets used in task forms, like amoCRM: in 15 min, in an hour, tomorrow 10:00, in 3 days. */
export function duePresets(): { label: string; value: string }[] {
  const now = new Date();
  const at = (d: Date) => localInput(d);
  const tomorrow = new Date(now); tomorrow.setDate(now.getDate() + 1); tomorrow.setHours(10, 0, 0, 0);
  const inThree = new Date(now); inThree.setDate(now.getDate() + 3); inThree.setHours(10, 0, 0, 0);
  const nextWeek = new Date(now); nextWeek.setDate(now.getDate() + 7); nextWeek.setHours(10, 0, 0, 0);
  return [{ label: "Через 15 мин", value: at(new Date(now.getTime() + 15 * 60000)) },
    { label: "Через час", value: at(new Date(now.getTime() + 3600000)) },
    { label: "Завтра 10:00", value: at(tomorrow) }, { label: "Через 3 дня", value: at(inThree) },
    { label: "Через неделю", value: at(nextWeek) }];
}

const fieldNames: Record<string, string> = { amount: "Сумма", name: "Название", responsible_user_id: "Ответственный",
  source_id: "Источник", custom_fields: "Доп. поля", tags: "Теги" };

/** Human-readable timeline line for a CRM / lead event. */
export function describeActivity(a: Activity): { icon: string; text: string; tone?: string } {
  const p = a.payload || {};
  const s = (k: string) => (p[k] == null ? "" : String(p[k]));
  switch (a.event_type) {
    case "DEAL_CREATED": return { icon: "＋", text: "Сделка создана" };
    case "INBOUND_CREATED": return { icon: "↘", text: `Заявка получена${s("source") ? ` · ${s("source")}` : ""}` };
    case "INBOUND_ACCEPTED": return { icon: "✓", text: "Заявка принята в работу" };
    case "STAGE_CHANGED": return { icon: "→", text: `Этап: ${s("from")} → ${s("to")}`, tone: "stage" };
    case "OWNER_CHANGED": return { icon: "👤", text: "Сменён ответственный" };
    case "ATTRIBUTION_CHANGED": return { icon: "⌖", text: "Скорректирован источник" };
    case "TAGS_CHANGED": return { icon: "#", text: `Теги: ${Array.isArray(p.new) ? (p.new as string[]).join(", ") || "—" : "изменены"}` };
    case "FIELD_CHANGED": return { icon: "✎", text: `Изменено: ${fieldNames[s("field")] || s("field")}${p.new != null && typeof p.new !== "object" ? ` → ${s("new")}` : ""}` };
    case "TASK_CREATED": return { icon: "◷", text: `Поставлена задача «${s("title")}»` };
    case "TASK_COMPLETED": return { icon: "☑", text: `Выполнена задача${s("title") ? ` «${s("title")}»` : ""}: ${s("result")}`, tone: "task" };
    case "TASK_UPDATED": return { icon: "◷", text: "Задача изменена" };
    case "QUALITY_CHANGED": return { icon: p.quality === "non_target" ? "⊘" : "◎", text: s("text"), tone: p.quality === "non_target" ? "lost" : "stage" };
    case "INBOUND_REPEAT": return { icon: "↻", text: `Повторное обращение${s("source") ? ` · ${s("source")}` : ""}`, tone: "touch" };
    case "COMMENT_ADDED": return { icon: "💬", text: s("text"), tone: "comment" };
    case "CALL_LOGGED": return { icon: "📞", text: `Звонок${s("text") ? `: ${s("text")}` : ""}`, tone: "touch" };
    case "CALL_ANALYZED": return { icon: "✨", text: `Разбор звонка${p.score != null ? ` · ${s("score")}/100` : ""}: ${s("summary")}${s("next_step") ? `\nСледующий шаг: ${s("next_step")}` : ""}`, tone: "comment" };
    case "DOCUMENT_CREATED": return { icon: "📄", text: s("text") || "Создан документ", tone: "touch" };
    case "DOCUMENT_VIEWED": return { icon: "👁", text: s("text") || "Клиент открыл документ", tone: "stage" };
    case "DOCUMENT_PAID": return { icon: "₽", text: s("text") || "Счёт оплачен", tone: "won" };
    case "MESSAGE_SENT": return { icon: "✉", text: `Сообщение клиенту${s("text") ? `: ${s("text")}` : ""}`, tone: "touch" };
    case "MEETING_HELD": return { icon: "🤝", text: `Встреча${s("text") ? `: ${s("text")}` : ""}`, tone: "touch" };
    case "AUTOMATION": return { icon: "⚡", text: `${s("text")} (правило «${s("rule")}»)`, tone: "auto" };
    case "SALE_CREATED": return { icon: "₽", text: `Подтверждена продажа${p.amount != null ? ` на ${Number(p.amount).toLocaleString("ru-RU")} ₽` : ""}`, tone: "won" };
    case "DEAL_WON": return { icon: "🏆", text: "Сделка выиграна", tone: "won" };
    case "DEAL_LOST": case "LEAD_LOST": return { icon: "✕", text: "Сделка проиграна", tone: "lost" };
    case "DEAL_ARCHIVED": return { icon: "▤", text: "Перенесена в архив" };
    case "CONTACT_UPDATED": return { icon: "✎", text: "Обновлены данные контакта" };
    case "CONTACT_MERGED": return { icon: "⇄", text: `Контакт «${s("from")}» объединён с «${s("to")}»` };
    case "LEAD_CREATED": return { icon: "＋", text: s("text") || "Лид создан" };
    case "LEAD_QUALIFIED": return { icon: "★", text: s("text") || "Лид квалифицирован" };
    default: return { icon: "•", text: s("text") || s("result") || s("title") || a.event_type };
  }
}

export function contactLinks(contact: Contact) {
  const phone = contact.phones?.[0];
  const digits = phone ? phone.replace(/\D/g, "").replace(/^8(\d{10})$/, "7$1") : "";
  const tg = contact.telegram?.trim();
  return {
    call: phone ? `tel:+${digits}` : null,
    whatsapp: digits ? `https://wa.me/${digits}` : null,
    telegram: tg ? (tg.startsWith("http") ? tg : `https://t.me/${tg.replace(/^@/, "")}`) : null,
    email: contact.emails?.[0] ? `mailto:${contact.emails[0]}` : null,
  };
}
