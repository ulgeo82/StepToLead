export type NavigationItem = { kind: "item"; id: string; label: string } & (
  { href: string; disabled?: never } | { disabled: true; href?: never }
);
export type NavigationBranch = {
  kind: "group" | "folder";
  id: string;
  label: string;
  badge?: "NEW";
  children: NavigationNode[];
};
export type NavigationNode = NavigationItem | NavigationBranch;
export const adminNavigation: NavigationNode[] = [
  { kind: "item", id: "overview", label: "Обзор", href: "/admin" },
  {
    kind: "group",
    id: "clients",
    label: "Клиенты",
    children: [
      { kind: "item", id: "results", label: "Результаты клиентов", href: "/result" },
      { kind: "item", id: "access", label: "Клиенты и доступы", href: "/admin/clients" },
      { kind: "item", id: "advertising", label: "Реклама", href: "/admin/advertising" },
      { kind: "item", id: "briefs", label: "Брифы", href: "/admin/briefs" },
    ],
  },
  {
    kind: "group",
    id: "growth",
    label: "Рост агентства",
    children: [
      { kind: "item", id: "growth-leads", label: "Заявки роста", href: "/admin/growth-leads" },
      {
        kind: "folder",
        id: "leadgen",
        label: "Лидогенерация",
        badge: "NEW",
        children: [
          { kind: "item", id: "company-search", label: "Поиск компаний", href: "/admin/leadgen/search" },
          { kind: "item", id: "companies", label: "База компаний", href: "/admin/leadgen/companies" },
          { kind: "item", id: "segments", label: "Сегменты", disabled: true },
          { kind: "item", id: "enrichment", label: "Обогащение", disabled: true },
          { kind: "item", id: "leadgen-analytics", label: "Аналитика", href: "/admin/leadgen/analytics" },
          { kind: "item", id: "leadgen-dnc", label: "Стоп-лист", href: "/admin/leadgen/dnc" },
          { kind: "item", id: "leadgen-settings", label: "Настройки", href: "/admin/leadgen/settings" },
        ],
      },
      {
        kind: "folder",
        id: "outreach",
        label: "Аутрич",
        children: [
          { kind: "item", id: "inbound", label: "Входящие", disabled: true },
          { kind: "item", id: "email", label: "Email-кампании", href: "/admin/outreach/email" },
          {
            kind: "folder",
            id: "telegram",
            label: "Telegram",
            children: [
              { kind: "item", id: "campaigns", label: "Кампании", href: "/admin/campaigns" },
              { kind: "item", id: "queue", label: "Очередь", href: "/admin/leads" },
              { kind: "item", id: "accounts", label: "Аккаунты", href: "/admin/accounts" },
              { kind: "item", id: "parser", label: "Сбор контактов", href: "/admin/parser" },
              { kind: "item", id: "proxies", label: "Прокси", href: "/admin/proxies" },
            ],
          },
        ],
      },
    ],
  },
  {
    kind: "group",
    id: "system",
    label: "Система",
    children: [
      { kind: "item", id: "monitor", label: "Мониторинг", href: "/admin/monitor" },
      { kind: "item", id: "ai", label: "ИИ: качество и расход", href: "/admin/ai" },
      { kind: "item", id: "ai-settings", label: "AI-настройки", disabled: true },
    ],
  },
];
export function isActiveRoute(path: string, href: string) {
  return path === href || (href !== "/admin" && path.startsWith(`${href}/`));
}
export function activeParents(nodes: NavigationNode[], path: string): string[] {
  return nodes.flatMap((node) => {
    if (node.kind === "item") return [];
    const children = activeParents(node.children, path);
    const active = node.children.some(
      (child) => child.kind === "item" && child.href && isActiveRoute(path, child.href),
    );
    return active || children.length ? [node.id, ...children] : [];
  });
}
export function filterNavigation(nodes: NavigationNode[], query: string): NavigationNode[] {
  const term = query.trim().toLocaleLowerCase("ru");
  if (!term) return nodes;
  return nodes.flatMap((node): NavigationNode[] => {
    if (node.label.toLocaleLowerCase("ru").includes(term)) return [node];
    if (node.kind === "item") return [];
    const children = filterNavigation(node.children, term);
    return children.length ? [{ ...node, children }] : [];
  });
}
export function navigationNumbers(nodes: NavigationNode[]) {
  const numbers: Record<string, string> = {};
  for (const node of nodes) {
    let index = 0;
    const visit = (item: NavigationNode) => {
      if (item.kind === "item") numbers[item.id] = String(++index).padStart(2, "0");
      else item.children.forEach(visit);
    };
    visit(node);
  }
  return numbers;
}
