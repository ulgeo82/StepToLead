/* Пространство клиента в разделах «Лидогенерация» и «Аутрич» (docs/PLAN.md, этап 1).
   Раздел работает «внутри клиента»: выбранный клиент уходит на сервер заголовком X-Workspace-Id (запросы)
   или параметром ?ws= (ссылки и скачивание файлов). Не выбран — пространство агентства, как раньше.
   Выбор хранится на вкладку (sessionStorage): две вкладки с разными клиентами не путают друг друга.
   localStorage — только подсказка для новой вкладки. */

export const SPACE_HEADER = "X-Workspace-Id";
export const SPACE_PARAM = "ws";
const KEY = "stl:space";

/** Страницы Telegram-аутрича (этап B): тоже работают внутри клиента. */
const TELEGRAM_PAGES = /^\/admin\/(campaigns|leads|accounts|parser|proxies)(\/|$)/;

/** Страницы, где работа идёт внутри клиента. */
export const isSpacePage = (pathname: string) =>
  /^\/admin\/(leadgen|outreach)(\/|$)/.test(pathname) || TELEGRAM_PAGES.test(pathname);

/** Запросы API, которые сервер понимает «в пространстве клиента». */
export const isSpaceApi = (path: string) =>
  /^\/admin\/(leadgen|outreach)(\/|\?|$)/.test(path)
  || /^\/(campaigns|leads|telegram-accounts|telegram-parser|proxies)(\/|\?|$)/.test(path);

const parse = (value: string | null | undefined): number | null => {
  const id = Number(value);
  return value && Number.isInteger(id) && id > 0 ? id : null;
};

/** Какой клиент выбран: ?ws= из ссылки главнее, затем выбор вкладки, затем последний выбор в браузере. */
export function pickSpace(search: string, session: string | null, local: string | null): number | null {
  return parse(new URLSearchParams(search).get(SPACE_PARAM)) ?? parse(session) ?? parse(local);
}

/** Добавить ?ws= к адресу (скачивание файла, ссылка) — заголовок туда не передать. */
export function spaceUrl(url: string, space: number | null): string {
  if (!space) return url;
  const [head, hash = ""] = url.split("#");
  return `${head}${head.includes("?") ? "&" : "?"}${SPACE_PARAM}=${space}${hash ? `#${hash}` : ""}`;
}

/** Куда вернуться при смене клиента: корень экрана без id и параметров — чужой список у другого клиента не открыть. */
export function sectionRoot(pathname: string): string {
  const parts = pathname.split("/").filter(Boolean);
  if (TELEGRAM_PAGES.test(pathname)) return `/${parts.slice(0, 2).join("/")}`;   // /admin/campaigns/12 → /admin/campaigns
  return isSpacePage(pathname) && parts.length >= 3 ? `/${parts.slice(0, 3).join("/")}` : pathname;
}

function storage(kind: "sessionStorage" | "localStorage"): Storage | null {
  try { return typeof window === "undefined" ? null : window[kind]; } catch { return null; }
}

/** Выбранный клиент этой вкладки (null — агентство). Клиента из ссылки запоминает. */
export function currentSpace(): number | null {
  if (typeof window === "undefined") return null;
  const session = storage("sessionStorage"), local = storage("localStorage");
  let fromSession: string | null = null, fromLocal: string | null = null;
  try { fromSession = session?.getItem(KEY) ?? null; fromLocal = local?.getItem(KEY) ?? null; } catch { /* приватный режим */ }
  // «0» во вкладке — агентство выбрано явно: подсказку из другой вкладки не берём.
  const space = pickSpace(window.location.search, fromSession, fromSession === null ? fromLocal : null);
  if (String(space ?? 0) !== fromSession) { try { session?.setItem(KEY, String(space ?? 0)); } catch { /* приватный режим */ } }
  return space;
}

export function chooseSpace(space: number | null): void {
  for (const kind of ["sessionStorage", "localStorage"] as const) {
    try { storage(kind)?.setItem(KEY, String(space ?? 0)); } catch { /* приватный режим */ }
  }
}

/** Клиент для запроса: только на страницах раздела и только для его API (радар вакансий остаётся у агентства). */
export function spaceForRequest(path: string): number | null {
  if (typeof window === "undefined" || !isSpaceApi(path) || !isSpacePage(window.location.pathname)) return null;
  return currentSpace();
}

/** Адрес для скачивания файла в пространстве выбранного клиента. */
export const withSpace = (url: string) => spaceUrl(url, typeof window === "undefined" ? null : currentSpace());
