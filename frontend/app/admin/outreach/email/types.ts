export type Step = {
  channel: string;
  delay_days: number;
  subject: string | null;
  body: string;
  new_thread: boolean;
};
export type Window = { tz: string; start_hour: number; end_hour: number; weekdays: number[]; gap_minutes: number; daily_new: number };
export type Sequence = {
  id: number;
  name: string;
  steps: Step[];
  window: Window;
  is_active: boolean;
  counts: Record<string, number>;
};
export type Mailbox = {
  id: number;
  client?: { project_id: number; name: string; project: string } | null;
  email: string;
  sender_name: string | null;
  daily_limit: number;
  sent_today: number;
  left_today: number;
  warmup_started_on: string | null;
  is_active: boolean;
  last_error: string | null;
  role?: "outreach" | "helper";
  warmup_enabled?: boolean;
  health?: number | null;
  cold_paused?: boolean;
};
export type Enrollment = {
  id: number;
  company_id: number;
  company: string;
  current_step: number;
  steps_total: number;
  next_at: string | null;
  status: string;
  stop_reason: string | null;
};
export const statusNames: Record<string, string> = {
  active: "Идёт",
  paused: "Пауза",
  finished: "Завершена",
  replied: "Ответила",
  bounced: "Почта не существует",
  stopped: "Остановлена",
};
export const channelNames: Record<string, string> = {
  email: "Письмо",
  call: "Позвонить",
  whatsapp: "WhatsApp",
  telegram: "Telegram",
};
export const emptyStep = (): Step => ({
  channel: "email",
  delay_days: 0,
  subject: "",
  body: "",
  new_thread: false,
});
export const emptyWindow = (): Window => ({
  tz: "Europe/Moscow",
  start_hour: 10,
  end_hour: 18,
  weekdays: [0, 1, 2, 3, 4],
  gap_minutes: 10,
  daily_new: 30,
});
export function fieldForError(text: string) {
  const step = text.match(/Шаг (\d+):/i);
  if (step)
    return `steps.${Number(step[1]) - 1}.${/тема|тему/.test(text) ? "subject" : /пауз|дней/.test(text) ? "delay_days" : /канал/.test(text) ? "channel" : "body"}`;
  const field = text.match(
    /(steps|window|name|email|smtp_host|smtp_port|imap_host|imap_port|password|daily_limit|login|sender_name)(?: · (\d+) · (\w+))?:/,
  );
  if (field) return [field[1], field[2], field[3]].filter(Boolean).join(".");
  if (/часовой пояс|окно отправки/i.test(text)) return "window";
  return "form";
}
export const stopReasons: Record<string, string> = {
  manual: "остановили вручную",
  reply: "ответили",
  unsubscribed: "отписались",
  bounce: "письмо вернулось",
  recipient_refused: "адрес не существует",
  no_email: "нет рабочей почты",
  no_deal: "сделка закрыта",
  sequence_inactive: "цепочку выключили",
  recently_contacted: "писали недавно",
  "crm:client": "уже клиент",
  "crm:open_deal": "открыта сделка",
  "sequence:another_active": "идёт другая цепочка",
  "dnc:company": "в стоп-листе",
  "dnc:domain": "домен в стоп-листе",
  "dnc:email": "почта в стоп-листе",
  "stage:rejected": "отказ",
  "stage:dnc": "в стоп-листе",
  "stage:converted": "уже клиент",
};
// Часовой пояс — получателей: письма уходят в их рабочее время. Сгруппировано для выбора.
export const timezoneGroups: [string, [string, string][]][] = [
  ["Россия", [
    ["Europe/Kaliningrad", "Калининград (МСК−1)"], ["Europe/Moscow", "Москва (МСК)"], ["Europe/Samara", "Самара (МСК+1)"],
    ["Asia/Yekaterinburg", "Екатеринбург (МСК+2)"], ["Asia/Omsk", "Омск (МСК+3)"], ["Asia/Novosibirsk", "Новосибирск (МСК+4)"],
    ["Asia/Krasnoyarsk", "Красноярск (МСК+4)"], ["Asia/Irkutsk", "Иркутск (МСК+5)"], ["Asia/Yakutsk", "Якутск (МСК+6)"],
    ["Asia/Vladivostok", "Владивосток (МСК+7)"], ["Asia/Magadan", "Магадан (МСК+8)"], ["Asia/Kamchatka", "Камчатка (МСК+9)"],
  ]],
  ["СНГ и Кавказ", [
    ["Europe/Minsk", "Минск"], ["Europe/Kyiv", "Киев"], ["Europe/Chisinau", "Кишинёв"], ["Asia/Almaty", "Алматы"],
    ["Asia/Tashkent", "Ташкент"], ["Asia/Bishkek", "Бишкек"], ["Asia/Dushanbe", "Душанбе"], ["Asia/Tbilisi", "Тбилиси"],
    ["Asia/Yerevan", "Ереван"], ["Asia/Baku", "Баку"],
  ]],
  ["Европа", [
    ["Europe/London", "Лондон"], ["Europe/Lisbon", "Лиссабон"], ["Europe/Berlin", "Берлин"], ["Europe/Paris", "Париж"],
    ["Europe/Madrid", "Мадрид"], ["Europe/Rome", "Рим"], ["Europe/Warsaw", "Варшава"], ["Europe/Prague", "Прага"],
    ["Europe/Helsinki", "Хельсинки"], ["Europe/Riga", "Рига"], ["Europe/Belgrade", "Белград"], ["Europe/Istanbul", "Стамбул"],
  ]],
  ["Ближний Восток и Азия", [
    ["Asia/Dubai", "Дубай"], ["Asia/Jerusalem", "Иерусалим"], ["Asia/Kolkata", "Индия"], ["Asia/Bangkok", "Бангкок"],
    ["Asia/Shanghai", "Пекин, Шанхай"], ["Asia/Singapore", "Сингапур"], ["Asia/Tokyo", "Токио"],
  ]],
  ["Америка", [
    ["America/New_York", "США — Нью-Йорк, Восток"], ["America/Chicago", "США — Чикаго, Центр"],
    ["America/Denver", "США — Денвер, Горное"], ["America/Phoenix", "США — Финикс (без летнего)"],
    ["America/Los_Angeles", "США — Лос-Анджелес, Запад"], ["America/Anchorage", "США — Аляска"], ["Pacific/Honolulu", "США — Гавайи"],
    ["America/Toronto", "Канада — Торонто"], ["America/Vancouver", "Канада — Ванкувер"], ["America/Mexico_City", "Мехико"],
    ["America/Sao_Paulo", "Сан-Паулу"], ["America/Buenos_Aires", "Буэнос-Айрес"],
  ]],
  ["Океания", [["Australia/Sydney", "Сидней"], ["Pacific/Auckland", "Окленд"]]],
];
export const timezones: [string, string][] = timezoneGroups.flatMap(([, zones]) => zones);
