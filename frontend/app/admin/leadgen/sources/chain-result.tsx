import styles from "../leadgen.module.css";
import { ChainSummary, shortError } from "./source-types";

const counters = {
  valid: "✓ Корректных почт", invalid: "✕ Некорректных почт", risky: "⚠ Почт с риском",
  cleaned: "Названий очищено", site_found: "Сайтов найдено", inn_found: "ИНН найдено",
};
export function CheckCounters({ result }: { result: Partial<ChainSummary> }) {
  return <ul>{Object.entries(counters).map(([key, label]) => {
    const count = result[key as keyof typeof counters];
    return count == null ? null : <li key={key}>{label}: {count}</li>;
  })}</ul>;
}
export function ChainResult({ chain, names, title = "После поиска" }: {
  chain: Record<string, ChainSummary>; names: Record<string, string>; title?: string;
}) {
  return <section aria-label="Итог цепочки"><h4>{title}</h4>
    {Object.entries(chain).map(([code, step]) => <article key={code} className={styles.chainStep}>
      <b>{names[code] || code}</b>
      <p>Сделано: {step.done} · пропущено: {step.skipped} · ошибок: {step.failed}</p>
      <CheckCounters result={step}/>
      {Object.entries(step.reasons || {}).map(([reason, count]) => <p key={reason}>
        {shortError(new Error(reason))}: {count}</p>)}
      {Object.entries(step.errors || {}).map(([key, reason]) => <p className={styles.errorText} key={key}>
        {shortError(new Error(reason))}</p>)}
    </article>)}
  </section>;
}
