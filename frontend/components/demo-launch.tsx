"use client";
import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";

type Q = { k: string; t: string; type: "one" | "text" | "area"; required: boolean; o?: string[]; hint?: string; example?: string };
type Form = { questions: Q[]; prefill: Record<string, string>; sent_at: string | null; booking_url: string };

/** «Хочу такой портал»: 5–7 вопросов из демо; ответы экспресс-оценки уже подставлены. */
export function DemoLaunch({ projectId, onClose }: { projectId: number; onClose: () => void }) {
  const [form, setForm] = useState<Form | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState<string | null>(null);
  useEffect(() => { api<Form>(`/brief/demo/launch?project_id=${projectId}`).then(f => { setForm(f); setAnswers(f.prefill); }).catch(e => setError(e.message)); }, [projectId]);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try { const r = await api<{ booking_url: string }>(`/brief/demo/launch?project_id=${projectId}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ answers }) }); setDone(r.booking_url || ""); }
    catch (e) { setError(e instanceof Error ? e.message : "Не удалось отправить"); } finally { setBusy(false); }
  }
  return <div className="demoLaunchBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
    <section className="demoLaunch" role="dialog" aria-label="Хочу свой портал"><button type="button" className="demoLaunchClose" onClick={onClose} aria-label="Закрыть">×</button>
      {done !== null ? <div className="demoLaunchDone"><h2>Заявка у нас 🔥</h2><p>Менеджер StepToLead свяжется с вами в выбранное время. На созвоне покажем портал на ваших цифрах и посчитаем, сколько стоит ваша заявка сейчас.</p>
        {done && <a className="demoLaunchPrimary" href={done} target="_blank" rel="noopener noreferrer">Выбрать время созвона самому →</a>}<button type="button" onClick={onClose}>Вернуться в демо</button></div>
      : <form onSubmit={submit}><h2>Такой портал — на ваших данных</h2><p>7 коротких вопросов, часть мы уже заполнили по вашей оценке. Запуск — после 20-минутного созвона.</p>
        {!form && !error && <p>Загружаем…</p>}
        {form?.questions.map(q => <label key={q.k} className="demoLaunchField"><span>{q.t}{q.required && " *"}</span>{q.hint && <small>{q.hint}</small>}
          {q.type === "one" ? <div className="demoLaunchOptions">{q.o!.map(o => <button type="button" key={o} aria-pressed={answers[q.k] === o} onClick={() => setAnswers(a => ({ ...a, [q.k]: o }))}>{o}</button>)}</div>
            : q.type === "area" ? <textarea rows={3} value={answers[q.k] || ""} placeholder={q.example} onChange={e => setAnswers(a => ({ ...a, [q.k]: e.target.value }))}/>
            : <input value={answers[q.k] || ""} onChange={e => setAnswers(a => ({ ...a, [q.k]: e.target.value }))}/>}</label>)}
        {error && <p className="demoLaunchError" role="alert">{error}</p>}
        {form && <button className="demoLaunchPrimary" disabled={busy}>{busy ? "Отправляем…" : form.sent_at ? "Обновить заявку" : "Хочу свой портал"}</button>}</form>}
    </section></div>;
}
