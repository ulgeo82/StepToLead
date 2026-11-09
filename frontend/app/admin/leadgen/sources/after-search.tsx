"use client";

import { CompanySource, shortError } from "./source-types";
import styles from "../leadgen.module.css";

export function defaultChain(sources: CompanySource[]) {
  return sources.filter(item => item.kind === "enricher" && item.chainable && item.chain_default)
    .map(item => item.code);
}

export function AfterSearch({ sources, selected, changed, disabled = false }: {
  sources: CompanySource[];
  selected: string[];
  changed: (codes: string[]) => void;
  disabled?: boolean;
}) {
  return <fieldset className={styles.multiOptions} disabled={disabled}>
    <legend>После поиска</legend>
    {sources.filter(item => item.kind === "enricher" && item.chainable).map(item =>
      <label className={styles.check} key={item.code}>
        <input type="checkbox" checked={selected.includes(item.code)} onChange={event =>
          changed(event.target.checked ? [...selected, item.code] : selected.filter(code => code !== item.code))}/>
        <span>{item.icon} {item.title}{(!item.ready || !item.enabled) && <small className={styles.hint}>
          {shortError(new Error(item.ready_reason || "Шаг будет пропущен: источник выключен"))}
        </small>}</span>
      </label>)}
  </fieldset>;
}
