// Формулы живут только в backend/app/services/business_economics.py.
export type GrowthInputs = {
  average_order_value: number; purchases_per_customer: number; gross_margin: number;
  fixed_costs: number; current_customers: number; target_customers: number; growth_budget: number;
};
export type GrowthResult = { metrics: Record<string, number | null>; status: "NOT_READY" | "LIMITED" | "TEST_READY"; hints: string[]; formula_version: string };
export const defaults: GrowthInputs = { average_order_value: 50000, purchases_per_customer: 1, gross_margin: 40, fixed_costs: 500000, current_customers: 40, target_customers: 60, growth_budget: 90000 };
export const statusCopy = {
  TEST_READY: { title: "Экономика готова к тестированию", text: "Текущая экономика позволяет выделить выбранный бюджет на тестирование маркетинговых гипотез, сохраняя положительную операционную прибыль и запас до точки безубыточности.", cta: "Разобрать маркетинговый план", next: "Следующий шаг — выбрать гипотезу, аудиторию и критерии теста. Результат маркетинга ещё предстоит проверить." },
  LIMITED: { title: "Тестирование с ограничениями", text: "После вложения бюджета бизнес остаётся прибыльным, но запас ограничен: бюджет превышает половину прибыли или оставшаяся операционная маржа ниже 5%.", cta: "Разобрать ограничения экономики", next: "Проверьте размер бюджета и расходы. Начните с ограниченного теста, который не поставит под угрозу текущую работу." },
  NOT_READY: { title: "Сначала укрепите экономику", text: "Текущей прибыли или запаса прочности недостаточно, либо выбранный бюджет полностью расходует прибыль. Сначала стоит разобраться с экономикой бизнеса.", cta: "Найти точки роста экономики", next: "Посмотрите на маржу, повторные покупки и постоянные расходы. Рост выручки сам по себе не гарантирует прибыль." },
};
export function number(value: number | null | undefined, digits = 0) { return value == null || !Number.isFinite(value) ? "Не определено" : new Intl.NumberFormat("ru-RU", { maximumFractionDigits: digits }).format(value); }
export function money(value: number | null | undefined) { return value == null ? "Не определено" : `${number(value)} ₽`; }
export function percent(value: number | null | undefined) { return value == null ? "Не определено" : `${number(value * 100, 1)}%`; }
