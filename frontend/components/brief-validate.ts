// Проверка ответов брифа — те же правила, что на сервере (services/brief.py: format_error).
export type BriefQuestionRule = { key: string; label: string; type: string; required: boolean; format?: string; min?: number; max?: number };

const number = (value: string) => {
  const text = value.toLowerCase().replace(/[\s ₽%]|руб\.?/g, "").replace(",", ".");
  return text && !Number.isNaN(Number(text)) ? Number(text) : null;
};

export function innValid(value: string) {
  if (!/^\d{10}$|^\d{12}$/.test(value)) return false;
  const d = value.split("").map(Number);
  const check = (k: number[]) => k.reduce((sum, c, i) => sum + c * d[i], 0) % 11 % 10;
  if (d.length === 10) return check([2, 4, 10, 3, 5, 9, 4, 6, 8]) === d[9];
  return check([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) === d[10] && check([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) === d[11];
}

/** Текст ошибки для поля или null. Пустое необязательное поле — не ошибка. */
export function briefError(q: BriefQuestionRule, raw: string | undefined): string | null {
  const value = (raw || "").trim();
  if (!value) {
    if (!q.required) return null;
    if (q.type === "one" || q.type === "many") return "выберите вариант";
    if (q.type === "funnels") return "добавьте хотя бы одно направление";
    return "заполните поле";
  }
  if (q.type === "funnels") {
    try { const parsed = JSON.parse(value); if (!parsed?.directions?.length) return "добавьте хотя бы одно направление"; } catch { /* текстовый формат допустим */ }
    return null;
  }
  const { format, min, max } = q;
  if (format === "url") return /^(https?:\/\/)?([\w-]+\.)+[a-zа-яё]{2,}(:\d+)?(\/\S*)?$/i.test(value) ? null : "укажите адрес сайта, например example.ru";
  if (format === "inn") return innValid(value.replace(/\s/g, "")) ? null : "ИНН — 10 или 12 цифр, проверьте номер";
  if (format === "money" || format === "int" || format === "percent") {
    const n = number(value);
    if (n === null) return "укажите число";
    if (format === "int" && !Number.isInteger(n)) return "укажите целое число";
    if (min != null && n < min) return `не меньше ${min}`;
    if (max != null && n > max) return `не больше ${max}`;
    return null;
  }
  if (format === "text" && min && value.length < min) return `слишком коротко — хотя бы ${min} символов`;
  return null;
}

export function contactError(key: string, raw: string | undefined, required: boolean): string | null {
  const value = (raw || "").trim();
  if (!value) return required ? "заполните поле" : null;
  if (key === "phone" && value.replace(/\D/g, "").length < 10) return "укажите телефон полностью, например +7 900 000-00-00";
  if (key === "email" && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) return "проверьте email";
  if (key === "full_name" && value.length < 2) return "укажите имя";
  return null;
}

export const inputModeFor = (format?: string, type?: string) =>
  format === "money" || format === "percent" ? "decimal" : format === "int" || format === "inn" ? "numeric" : type === "num" ? "decimal" : undefined;
