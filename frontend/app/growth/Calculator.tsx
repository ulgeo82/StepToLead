"use client";
import { FormEvent, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { defaults, GrowthInputs, GrowthResult, money, number, percent, statusCopy } from "@/lib/growth";

type FieldKey = keyof GrowthInputs;
const fields: { key: FieldKey; label: string; unit: string; hint: string; max: number; step: number }[] = [
  { key: "average_order_value", label: "Средний чек", unit: "₽", hint: "Средняя выручка с одной покупки.", max: 1e12, step: 0.01 },
  { key: "purchases_per_customer", label: "Покупок на клиента", unit: "в месяц", hint: "Среднее число покупок за месяц. Можно дробное.", max: 1e6, step: 0.01 },
  { key: "gross_margin", label: "Валовая маржа", unit: "%", hint: "Доля выручки после прямых затрат, до постоянных расходов.", max: 100, step: 0.1 },
  { key: "fixed_costs", label: "Постоянные расходы", unit: "₽ / мес.", hint: "Аренда, фиксированный фонд оплаты труда и другие постоянные затраты.", max: 1e12, step: 0.01 },
  { key: "current_customers", label: "Клиентов сейчас", unit: "в месяц", hint: "Уникальные платящие клиенты, не количество покупок.", max: 1e9, step: 1 },
  { key: "target_customers", label: "Целевое число клиентов", unit: "в месяц", hint: "Сколько клиентов бизнес способен обслуживать. Это сценарий, не прогноз.", max: 1e9, step: 1 },
  { key: "growth_budget", label: "Бюджет на тестирование", unit: "₽", hint: "Вычитается из операционной прибыли выбранного месяца.", max: 1e12, step: 0.01 },
];
const defaultStrings = Object.fromEntries(Object.entries(defaults).map(([key, value]) => [key, String(value)])) as Record<FieldKey, string>;

function Metric({ label, value, note }: { label: string; value: string; note?: string }) {
  return <div className="g-metric"><span>{label}</span><strong>{value}</strong>{note && <small>{note}</small>}</div>;
}
function Row({ label, value, strong = false }: { label: string; value: string; strong?: boolean }) {
  return <div className={`g-row ${strong ? "g-row-strong" : ""}`}><dt>{label}</dt><dd>{value}</dd></div>;
}
function Comparison({ title, current, target }: { title: string; current: number; target: number }) {
  const scale = Math.max(Math.abs(current), Math.abs(target), 1);
  return <div className="g-chart" role="img" aria-label={`${title}: сейчас ${money(current)}, цель ${money(target)}`}><h3>{title}</h3>{[["Сейчас", current], ["При целевой загрузке", target]].map(([label, value], i) => <div className="g-bar-row" key={label}><div><span>{label}</span><strong>{money(Number(value))}</strong></div><div className="g-bar-track"><span className={Number(value) < 0 ? "negative" : i ? "target" : ""} style={{ width: `${Math.abs(Number(value)) / scale * 100}%` }} /></div></div>)}</div>;
}

export default function Calculator() {
  const [values, setValues] = useState(defaultStrings);
  const [result, setResult] = useState<GrowthResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(true);
  const [isExample, setExample] = useState(true);
  const [retry, setRetry] = useState(0);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState("");
  const [saveError, setSaveError] = useState("");
  const confirmedInputs = useRef<GrowthInputs | null>(null);
  const generation = useRef(0);

  useEffect(() => {
    const version = ++generation.current;
    const controller = new AbortController();
    setBusy(true); setError(""); setResult(null); confirmedInputs.current = null;
    const timer = setTimeout(async () => {
      const payload = {} as GrowthInputs;
      for (const field of fields) {
        const value = Number(values[field.key].replace(",", "."));
        if (!values[field.key].trim() || !Number.isFinite(value) || value < 0 || value > field.max || (field.step === 1 && !Number.isInteger(value))) {
          setError(`«${field.label}»: введите ${field.step === 1 ? "целое " : ""}число от 0 до ${number(field.max)}.`); setBusy(false); return;
        }
        payload[field.key] = value;
      }
      if (payload.target_customers < payload.current_customers) { setError("Целевое число клиентов должно быть не меньше текущего."); setBusy(false); return; }
      try {
        const next = await api<GrowthResult>("/public/growth/calculate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal: controller.signal });
        if (version === generation.current && !controller.signal.aborted) { setResult(next); confirmedInputs.current = payload; }
      } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "Расчёт недоступен"); }
      finally { if (!controller.signal.aborted) setBusy(false); }
    }, 300);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [values, retry]);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!confirmedInputs.current || busy || saving) return;
    const form = new FormData(event.currentTarget);
    setSaving(true); setSaveError("");
    try {
      const response = await api<{ message: string }>("/public/growth/leads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ inputs: confirmedInputs.current, name: form.get("name"), phone: form.get("phone"), telegram: form.get("telegram"), email: form.get("email"), consent: form.get("consent") === "on" }) });
      setSaved(response.message);
    } catch (e) { setSaveError(e instanceof Error ? e.message : "Не удалось сохранить заявку"); }
    finally { setSaving(false); }
  }
  const m = result?.metrics;
  const copy = result ? statusCopy[result.status] : null;
  const contactUrl = process.env.NEXT_PUBLIC_GROWTH_CONTACT_URL || "/contact";
  return <div className="growth">
    <header className="g-header"><div className="g-wrap"><Link href="/growth" className="g-brand" aria-label="StepToLead — главная"><span>↗</span>StepToLead</Link><p>Понятная экономика. Уверенные решения.</p><span className="g-header-tag">Бизнес-инструменты</span></div></header>
    <main className="g-wrap g-main">
      <div className="g-topline"><span className="g-kicker">РЕШЕНИЯ НА ОСНОВЕ ЦИФР</span><nav className="g-steps" aria-label="Этапы"><span aria-current="step"><b>01</b> Экономика</span><i>—</i><span><b>02</b> Маркетинг</span></nav></div>
      <section className="g-intro"><div><h1>Калькулятор<br />экономики роста<span>.</span></h1><p>Проверьте, сколько зарабатывает бизнес<br className="g-desktop" /> и какой бюджет он выдержит без потери устойчивости.</p></div><div className="g-intro-note"><span>7 показателей · один месяц</span><p>Не прогноз лидов.<br />Основа для решения о росте.</p></div></section>
      <div className="g-workspace">
        <aside className="g-inputs g-card"><div className="g-section-head"><div><span className="g-kicker">ИСХОДНЫЕ ДАННЫЕ</span><h2>Экономика вашего бизнеса</h2></div></div><p className="g-small">Все показатели — за один и тот же месяц. Обновляем расчёт автоматически.</p><div className="g-example">{isExample ? "Показан пример. Замените цифры своими." : "Ваш сценарий"}<button type="button" onClick={() => { setValues(defaultStrings); setExample(true); setSaved(""); }}>Сбросить</button></div>
          <div className="g-fields">{fields.map((field, i) => <label className="g-field" key={field.key} htmlFor={field.key}><span><i>{String(i + 1).padStart(2, "0")}</i>{field.label}</span><div className="g-input-wrap"><input id={field.key} type="number" inputMode={field.step === 1 ? "numeric" : "decimal"} min={0} max={field.max} step={field.step} value={values[field.key]} aria-describedby={`${field.key}-hint`} onChange={e => { setValues(v => ({ ...v, [field.key]: e.target.value })); setExample(false); setSaved(""); setBusy(true); confirmedInputs.current = null; }} /><em>{field.unit}</em></div><small id={`${field.key}-hint`}>{field.hint}</small></label>)}</div>
        </aside>
        <div className="g-results" aria-busy={busy}>
          <div className="g-result-head"><h2>Картина бизнеса</h2><span className="g-live">{busy ? "Считаем…" : error ? "Проверьте данные" : "Расчёт обновлён"}</span></div>
          {(busy || error) && <div className="g-card g-empty" role={error ? "alert" : "status"}><span className="g-empty-icon">{error ? "!" : "↻"}</span><h2>{error ? "Нужны корректные данные" : "Считаем экономику"}</h2><p>{error || "Проверяем прибыль, точку безубыточности и бюджет."}</p>{error && <button className="g-button g-secondary" onClick={() => setRetry(n => n + 1)}>Повторить расчёт</button>}</div>}
          {result && m && copy && !busy && <>
            <div className="g-kpis"><Metric label="Выручка в месяц" value={money(m.currentRevenue)} note="Объём продаж сейчас" /><Metric label="Операционная прибыль" value={money(m.currentOperatingProfit)} note="До бюджета на тестирование" /><Metric label="Операционная маржа" value={percent(m.operatingMargin)} note="Прибыль / выручка" /><Metric label="Запас прочности" value={percent(m.safetyMargin)} note="Допустимое снижение клиентской базы" /></div>
            <section className={`g-status ${result.status.toLowerCase()}`} aria-live="polite"><span className="g-status-symbol">{result.status === "TEST_READY" ? "✓" : "!"}</span><div><span className="g-kicker">ИТОГОВЫЙ СТАТУС</span><h2>{copy.title}</h2><p>{copy.text}</p><div className="g-status-foot"><span>Прибыль после вложения бюджета</span><strong>{money(m.profitAfterInvestment)}</strong></div></div></section>
            {result.hints.length > 0 && <div className="g-hints">{result.hints.map(h => <p key={h}>{h}</p>)}</div>}
            <section className="g-card"><div className="g-section-head"><div><span className="g-kicker">01 / ОСНОВА</span><h2>Клиент и текущий бизнес</h2></div></div><div className="g-detail-columns"><div><h3>Экономика клиента</h3><dl><Row label="Выручка на клиента" value={money(m.revenuePerCustomer)} /><Row label="Валовая прибыль на клиента" value={money(m.grossProfitPerCustomer)} strong /></dl><p className="g-small">Валовая прибыль — после прямых затрат, но до постоянных расходов.</p></div><div><h3>Текущий месяц</h3><dl><Row label="Валовая прибыль" value={money(m.currentGrossProfit)} /><Row label="Постоянные расходы" value={money(confirmedInputs.current?.fixed_costs)} /><Row label="Операционная прибыль" value={money(m.currentOperatingProfit)} strong /></dl></div></div></section>
            <section className="g-card"><div className="g-section-head"><div><span className="g-kicker">02 / УСТОЙЧИВОСТЬ</span><h2>Где проходит точка безубыточности</h2></div></div><div className="g-detail-columns"><dl><Row label="Клиентов для покрытия расходов" value={number(m.breakEvenCustomersRounded)} strong /><Row label="Выручка для покрытия расходов" value={money(m.breakEvenRevenue)} /></dl><dl><Row label="Запас в клиентах" value={number(m.safetyCustomers, 1)} /><Row label="Запас прочности" value={percent(m.safetyMargin)} strong /></dl></div><p className="g-small">Число клиентов для безубыточности округлено вверх. Выручка и запас считаются по точному, неокруглённому порогу.</p><div className="g-safety"><div className="g-safety-label"><span>Безубыточность · {number(m.breakEvenCustomersRounded)} клиентов</span><strong>Сейчас · {number(confirmedInputs.current?.current_customers)}</strong></div><div className="g-safety-track"><span style={{ width: `${Math.max(0, Math.min(100, (m.safetyMargin ?? 0) * 100))}%` }} /></div><div className="g-legend"><span><i />Покрытие расходов</span><span><i />Запас прочности</span></div></div></section>
            <section className="g-card"><div className="g-section-head"><div><span className="g-kicker">03 / ПОТЕНЦИАЛ</span><h2>Если выйти на целевую загрузку</h2></div><span className="g-chip">Сценарий, не прогноз</span></div><div className="g-charts"><Comparison title="Выручка" current={m.currentRevenue!} target={m.targetRevenue!} /><Comparison title="Операционная прибыль" current={m.currentOperatingProfit!} target={m.potentialOperatingProfit!} /></div><dl><Row label="Валовая прибыль при цели" value={money(m.targetGrossProfit)} /><Row label="Дополнительные клиенты" value={number(m.additionalCustomers)} /><Row label="Прирост выручки" value={money(m.revenueGrowth)} /><Row label="Потенциальный прирост прибыли" value={money(m.potentialProfitGrowth)} strong /></dl><p className="g-small">При том же чеке, частоте покупок, марже и постоянных расходах. Дополнительный персонал, оборудование и площади считаются отдельно.</p></section>
            <section className="g-card"><div className="g-section-head"><div><span className="g-kicker">04 / РЕШЕНИЕ</span><h2>Что останется после тестирования</h2></div></div><dl><Row label="Бюджет на тестирование" value={money(confirmedInputs.current?.growth_budget)} /><Row label="Нагрузка бюджета на прибыль" value={percent(m.budgetLoad)} /><Row label="Прибыль после вложения" value={money(m.profitAfterInvestment)} strong /><Row label="Операционная маржа после вложения" value={percent(m.operatingMarginAfterInvestment)} /><Row label="Доля сохранённой прибыли" value={percent(m.profitReserveAfterInvestment)} /></dl><p className="g-small">Бюджет вычитается из текущей прибыли, а не из потенциальной. Будущие продажи здесь не предполагаются.</p></section>
            <section className="g-next" id="recommendations"><span className="g-kicker">ЭКОНОМИКА → МАРКЕТИНГ</span><h2>Цифры понятны.<br />Обсудим следующий шаг?</h2><p>{copy.next}</p><a href={contactUrl} className="g-button">{copy.cta}<span aria-hidden>↗</span></a></section>
            <section className="g-card g-contact" id="save-calculation"><span className="g-kicker">СОХРАНИТЬ И ОБСУДИТЬ</span><h2>Получить разбор расчёта</h2><p className="g-muted">Оставьте имя и один удобный способ связи. Расчёт уже доступен — контакты не обязательны для его просмотра.</p>{saved ? <div className="g-success" role="status">✓ {saved}</div> : <form onSubmit={save}><div className="g-contact-fields"><label className="g-field">Ваше имя<input name="name" autoComplete="name" placeholder="Как к вам обращаться" maxLength={120} required /></label><label className="g-field">Telegram<input name="telegram" placeholder="@username" maxLength={64} /></label><label className="g-field">Телефон<input name="phone" type="tel" autoComplete="tel" placeholder="+7 …" maxLength={40} /></label><label className="g-field">Email<input name="email" type="email" autoComplete="email" placeholder="name@company.ru" maxLength={254} /></label></div><label className="g-consent"><input name="consent" type="checkbox" required /><span>Разрешаю StepToLead сохранить эти контакты и расчёт, чтобы связаться со мной по этой заявке.</span></label>{saveError && <p className="g-error" role="alert">{saveError}</p>}<button className="g-button" disabled={saving || busy}>{saving ? "Сохраняем…" : "Сохранить расчёт и отправить заявку"}</button><p className="g-small">Заявка попадёт только в закрытый кабинет. Автоматические рассылки не подключаются.</p></form>}</section>
          </>}
        </div>
      </div>
      <footer className="g-footer"><Link className="g-brand" href="/growth"><span>↗</span>StepToLead</Link><div><p>Базовая управленческая модель, а не бухгалтерское, финансовое или инвестиционное заключение.</p><p>Не учитывает налоги, кредиты, сезонность и кассовые разрывы отдельно. Не прогнозирует число лидов, стоимость привлечения или результат рекламы.</p></div><Link href="/login">Для команды ↗</Link></footer>
    </main>
  </div>;
}
