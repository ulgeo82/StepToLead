import type { SourceEstimate } from "./source-types";

export function SourceCost({ cost }: { cost: SourceEstimate["cost"] }) {
  if (!cost) return null;
  return <small>Запросов к поиску: {cost.requests.toLocaleString("ru-RU")}
    {cost.rub == null ? "" : ` (≈ ${cost.rub.toLocaleString("ru-RU", { maximumFractionDigits: 4 })} ₽)`}</small>;
}
