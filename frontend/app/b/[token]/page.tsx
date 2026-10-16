"use client";
import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { BrandMark } from "@/components/brand-mark";
import { FunnelsEditor } from "@/components/brief-funnels";
import { briefError, contactError, inputModeFor } from "@/components/brief-validate";
import "@/components/brief.css";
import "./public-brief.css";

type Question = { key: string; label: string; hint: string; type: string; required: boolean; options: string[]; unit: string; example: string; large: boolean;
  format?: string; min?: number; max?: number };
type Section = { key: string; title: string; description: string; questions: Question[] };
type Data = { sections: Section[]; answers: Record<string, string>; contacts: Record<string, string>;
  contact_fields: { k: string; t: string; required: boolean }[]; submitted_at: string | null; privacy_url: string };

const call = async <T,>(path: string, method = "GET", body?: unknown): Promise<T> => {
  const r = await fetch(`/api/public/brief/${path}`, { method, headers: body ? { "Content-Type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined });
  const json = await r.json().catch(() => null);
  if (!r.ok) throw new Error(typeof json?.detail === "string" ? json.detail : "Не удалось сохранить");
  return json as T;
};

// Бриф по ссылке после созвона: без логина, черновик сохраняется сам, после отправки — менеджеру и в сделку.
export default function PublicBrief() {
  const { token } = useParams<{ token: string }>();
  const [data, setData] = useState<Data | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [contacts, setContacts] = useState<Record<string, string>>({});
  const [step, setStep] = useState(-1);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [done, setDone] = useState(false);
  const ready = useRef(false);
  useEffect(() => { call<Data>(encodeURIComponent(token)).then(d => { setData(d); setAnswers(d.answers); setContacts(d.contacts); setDone(Boolean(d.submitted_at)); ready.current = true; })
    .catch(e => setError(e.message)); }, [token]);
  useEffect(() => { if (!ready.current || done) return; setSaved("Сохраняем…");
    const t = setTimeout(() => call(encodeURIComponent(token), "PUT", { answers, contacts }).then(() => setSaved("Черновик сохранён — можно продолжить позже по этой же ссылке")).catch(e => setSaved(e.message)), 800);
    return () => clearTimeout(t); }, [answers, contacts]);
  if (error && !data) return <main className="pbShell"><div className="pbCard"><BrandMark/><h1>Бриф недоступен</h1><p>{error}</p></div></main>;
  if (!data) return <main className="pbShell"><div className="pbCard"><p>Загружаем бриф…</p></div></main>;
  if (done) return <main className="pbShell"><div className="pbCard"><BrandMark/><h1>Спасибо! Бриф у нас ✅</h1><p>Менеджер StepToLead изучит ответы и вернётся с планом запуска. Если что-то вспомните — напишите менеджеру.</p></div></main>;
  const required = data.sections.flatMap(s => s.questions).filter(q => q.required);
  const filled = required.filter(q => answers[q.key]?.trim()).length;
  const set = (k: string, v: string) => { setAnswers(a => ({ ...a, [k]: v })); setErrors(e => { const { [k]: _, ...rest } = e; return rest; }); };
  // Проверка шага: не пускаем дальше, пока на шаге есть пустые обязательные или некорректные ответы.
  const stepErrors = (i: number) => {
    const found: Record<string, string> = {};
    if (i === -1) data.contact_fields.forEach(f => { const e = contactError(f.k, contacts[f.k], f.required); if (e) found["contact:" + f.k] = e; });
    else data.sections[i].questions.forEach(q => { const e = briefError(q, answers[q.key]); if (e) found[q.key] = e; });
    return found;
  };
  const goto = (i: number) => { setStep(i); window.scrollTo(0, 0); };
  const next = () => {
    const found = stepErrors(step);
    setErrors(found);
    if (Object.keys(found).length) { setError(`Проверьте поля на этом шаге: ${Object.keys(found).length}`); window.scrollTo(0, 0); return; }
    setError(""); goto(step + 1);
  };
  const sections = data.sections;
  const current = step >= 0 ? sections[step] : null;
  async function submit() {
    for (let i = -1; i < sections.length; i++) {
      const found = stepErrors(i);
      if (Object.keys(found).length) { setErrors(found); setError(`Не всё заполнено: шаг «${i === -1 ? "Контакты" : sections[i].title}»`); goto(i); return; }
    }
    setBusy(true); setError("");
    try { await call(`${encodeURIComponent(token)}/submit`, "POST", { answers, contacts }); setDone(true); window.scrollTo(0, 0); }
    catch (e) { setError(e instanceof Error ? e.message : "Не удалось отправить"); } finally { setBusy(false); }
  }
  return <main className="pbShell"><div className="pbWrap">
    <header className="pbHeader"><BrandMark/><div><h1>Бриф на запуск</h1><p>Около 15 минут. Ответы помогут запустить рекламу и настроить портал под ваш бизнес. Обязательные вопросы отмечены; остальное можно пропустить — обсудим на созвоне.</p></div></header>
    <div className="pbProgress"><strong>Обязательные: {filled} из {required.length}</strong><progress value={filled} max={required.length || 1}/><span>{saved}</span></div>
    <div className="pbLayout"><nav className="briefRail"><button aria-current={step === -1 ? "step" : undefined} onClick={() => setStep(-1)}>0. Контакты</button>
      {sections.map((s, i) => <button key={s.key} aria-current={i === step ? "step" : undefined} onClick={() => setStep(i)}>{i + 1}. {s.title} <small>{s.questions.filter(q => answers[q.key]?.trim()).length}/{s.questions.length}</small></button>)}</nav>
      <section className="pbSection">{step === -1 ? <><h2>Контакты</h2><p>Кто будет на связи по запуску.</p>
        {data.contact_fields.map(f => <label key={f.k} className={`pbField${errors["contact:" + f.k] ? " invalid" : ""}`}><span>{f.t}{f.required && " *"}</span>
          <input value={contacts[f.k] || ""} aria-invalid={Boolean(errors["contact:" + f.k])} onChange={e => { setContacts(c => ({ ...c, [f.k]: e.target.value })); setErrors(x => { const { ["contact:" + f.k]: _, ...rest } = x; return rest; }); }}
            type={f.k === "email" ? "email" : f.k === "phone" ? "tel" : "text"} inputMode={f.k === "phone" ? "tel" : f.k === "email" ? "email" : undefined}/>
          {errors["contact:" + f.k] && <em className="pbError">{errors["contact:" + f.k]}</em>}</label>)}</>
        : current && <><h2>{current.title}</h2><p>{current.description}</p>
          {current.questions.map(q => <div className={`briefQuestion${q.type === "funnels" ? " wide" : ""}${errors[q.key] ? " invalid" : ""}`} key={q.key}><div><label htmlFor={q.key}>{q.label} {q.required && <small className="briefBadge">обязательно</small>}</label><p>{q.hint}</p></div>
            {q.type === "funnels" ? <FunnelsEditor value={answers[q.key] || ""} onChange={v => set(q.key, v)} disabled={busy} hintText={answers["sales.path"]} briefId={null} beforeApply={async () => undefined}/>
            : <div>{q.type === "one" || q.type === "many" ? <div className="briefOptions">{q.options.map(v => { const values = (answers[q.key] || "").split(", "); const selected = q.type === "many" ? values.includes(v) : answers[q.key] === v;
                return <button type="button" key={v} aria-pressed={selected} onClick={() => set(q.key, q.type === "many" ? (selected ? values.filter(x => x !== v) : [...values.filter(Boolean), v]).join(", ") : selected ? "" : v)}>{v}</button>; })}</div>
              : q.type === "area" ? <textarea id={q.key} rows={q.large ? 6 : 3} maxLength={10000} placeholder={q.example ? "Например: " + q.example : undefined} value={answers[q.key] || ""} onChange={e => set(q.key, e.target.value)}/>
              : <input id={q.key} maxLength={10000} type={q.format === "url" ? "url" : "text"} inputMode={inputModeFor(q.format, q.type)} placeholder={q.example ? "Например: " + q.example : undefined}
                  aria-invalid={Boolean(errors[q.key])} value={answers[q.key] || ""} onChange={e => set(q.key, e.target.value)}/>}<small>{q.unit}</small>
              {errors[q.key] && <em className="pbError">{errors[q.key]}</em>}</div>}
            {q.type === "funnels" && errors[q.key] && <em className="pbError">{errors[q.key]}</em>}</div>)}</>}
        <div className="briefActions"><button disabled={step === -1} onClick={() => { setErrors({}); setError(""); goto(step - 1); }}>Назад</button>
          {step < sections.length - 1 ? <button onClick={next}>Дальше</button>
            : <button className="pbSubmit" disabled={busy} onClick={() => void submit()}>{busy ? "Отправляем…" : "Отправить бриф"}</button>}</div>
        {error && <p className="briefError" role="alert">{error}</p>}
        {step === sections.length - 1 && data.privacy_url && <small className="pbPrivacy">Отправляя бриф, вы соглашаетесь с <a href={data.privacy_url} target="_blank" rel="noopener noreferrer">политикой обработки персональных данных</a>.</small>}
      </section></div>
  </div></main>;
}
