import type { Company } from "../../shared";

export const stepOrder = [
  "find_site", "site_contacts", "email_check", "name_clean", "site_tech",
  "dadata", "fns_revenue", "fit_ai", "personalize",
] as const;
export const stepNames: Record<string, string> = {
  find_site: "Найти сайт и ИНН", site_contacts: "Найти почты", email_check: "Валидация почт",
  name_clean: "Чистка названий", site_tech: "Проверка сайтов", dadata: "DaData",
  fns_revenue: "Доходы ФНС", fit_ai: "Оценка ЦА", personalize: "Персонализация",
};
export type TableRow = Company & {
  best_email: { value: string; verify_status: string } | null;
  emails: number;
  phone: string | null;
  messengers: string[];
  signals: string[];
  revenue_rub: number | null;
  source: string | null;
  added_at: string | null;
  duplicate_group: string | null;
  personal_line?: string | null;
};
export type Column = { key: string; label: string; filter: "text" | "values" | "empty" };
export type TableData = { total: number; items: TableRow[]; columns: Column[] };
export type Summary = { total: number; items: { value: string | null; count: number }[] };
export type View = { q: string; sort: string; dir: "asc" | "desc"; page: number; filters: Record<string, string> };
export type ColumnPreference = { key: string; visible: boolean };
export type RecentList = { id: number; name: string };
export const recentKey = "stl:leadgen:recent-lists:v1";

export function orderedSteps(selected: string[]): string[] {
  return stepOrder.filter(code => selected.includes(code));
}
export function tableQuery(view: View): string {
  const query = new URLSearchParams({ page: String(view.page), per_page: "100", dir: view.dir });
  if (view.sort) query.set("sort", view.sort);
  if (view.q.trim()) query.set("q", view.q.trim());
  for (const [key, value] of Object.entries(view.filters)) if (value) query.set(`f.${key}`, value);
  return query.toString();
}
export function safeRead(key: string): unknown {
  try { return JSON.parse(localStorage.getItem(key) || "null"); } catch { return null; }
}
export function safeWrite(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Private mode / storage quota. */ }
}
export function recentLists(value: unknown): RecentList[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is RecentList => !!item && Number.isSafeInteger(item.id) && item.id > 0 &&
    typeof item.name === "string").slice(0, 8);
}
export function rememberList(value: unknown, item: RecentList): RecentList[] {
  return [item, ...recentLists(value).filter(other => other.id !== item.id)].slice(0, 8);
}
export function columnPreferences(columns: Column[], saved: unknown): ColumnPreference[] {
  const known = new Set(columns.map(column => column.key));
  const output: ColumnPreference[] = [];
  if (Array.isArray(saved)) for (const item of saved) {
    if (item && known.has(item.key) && !output.some(column => column.key === item.key)) {
      output.push({ key: item.key, visible: item.visible !== false });
    }
  }
  for (const column of columns) if (!output.some(item => item.key === column.key)) {
    output.push({ key: column.key, visible: column.key !== "personal_line" });
  }
  if (output.length && !output.some(item => item.visible)) output[0].visible = true;
  return output;
}
export function validInn(value: string): boolean {
  if (!/^\d{10}$|^\d{12}$/.test(value) || /^0+$/.test(value)) return false;
  const digits = [...value].map(Number);
  const check = (weights: number[]) => weights.reduce((sum, weight, i) => sum + weight * digits[i], 0) % 11 % 10;
  return value.length === 10
    ? check([2, 4, 10, 3, 5, 9, 4, 6, 8]) === digits[9]
    : check([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) === digits[10] &&
      check([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) === digits[11];
}
export function valueOf(row: TableRow, key: string): string {
  switch (key) {
    case "company": case "display_name": return row.display_name || row.domain_display || row.domain || "";
    case "email": case "best_email": return row.best_email?.value || "";
    case "email_status": return row.best_email?.verify_status || "unknown";
    case "signal": case "signals": return row.signals.join(", ");
    case "fit": return row.fit_label || "";
    default: {
      const value = row[key as keyof TableRow];
      return value == null ? "" : Array.isArray(value) ? value.join(", ") : String(value);
    }
  }
}
export function tsv(rows: TableRow[], columns: Column[]): string {
  // Prevent spreadsheet formulas when users paste untrusted company names into Excel.
  const cell = (value: string) => {
    const text = value.replace(/[\t\r\n]+/g, " ");
    return /^[\s]*[=+@-]/.test(text) ? `'${text}` : text;
  };
  return [columns.map(column => cell(column.label)).join("\t"),
    ...rows.map(row => columns.map(column => cell(valueOf(row, column.key))).join("\t"))].join("\n");
}
