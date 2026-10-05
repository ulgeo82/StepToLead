"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type FunctionState = {feature: string; provider: string; model: string; key_set: boolean; configured: boolean};
type Overview = {functions: FunctionState[]; stt: FunctionState; limits: {global: number; workspace: number}; workspaces: {id: number; name: string}[]};
type Usage = {feature: string; model: string; workspace_id: number | null; requests: number; errors: number; tokens_in: number; tokens_out: number; audio_seconds: number; cost_rub: number; price_known: boolean};
const names: Record<string, string> = {chat: "Подсказка ответа", summary: "Резюме переписки", calls: "Анализ звонка", campaigns: "Кампании", stt: "Распознавание речи"};

export default function AIPage() {
  const [data, setData] = useState<Overview | null>(null);
  const [rows, setRows] = useState<Usage[]>([]);
  const [days, setDays] = useState(1);
  const [workspace, setWorkspace] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [answers, setAnswers] = useState<Record<string, unknown>>({});
  const [file, setFile] = useState<File | null>(null);
  const refresh = async () => setRows((await api<{groups: Usage[]}>(`/admin/ai/usage?days=${days}`)).groups);
  useEffect(() => { api<Overview>("/admin/ai").then(setData).catch(e => setError(e.message)); }, []);
  useEffect(() => { refresh().catch(e => setError(e.message)); }, [days]);
  const test = async (feature: string) => {
    setBusy(feature); setError("");
    try {
      let answer;
      if (feature === "stt") {
        if (!file) throw new Error("Выберите WAV с русской речью длительностью до 60 секунд");
        const body = new FormData(); body.append("file", file);
        const response = await fetch("/api/admin/ai/stt", {method: "POST", body, credentials: "same-origin"});
        const result = await response.json();
        if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Не удалось проверить распознавание");
        answer = result;
      } else {
        answer = await api(`/admin/ai/test/${feature}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({workspace_id: workspace ? Number(workspace) : null}), timeoutMs: 300000});
      }
      setAnswers(prev => ({...prev, [feature]: answer}));
    } catch (e) { setError(e instanceof Error ? e.message : "Ошибка проверки ИИ"); }
    finally { setBusy(""); await refresh().catch(() => {}); }
  };
  return <>
    <section className="pageHeader"><div><h1>ИИ: проверка и расход</h1><p>Проверки платные. Используйте вымышленные данные. Периоды расхода — по UTC.</p></div></section>
    {error && <p role="alert" className="crmFormError">{error}</p>}
    <section className="panel"><h2>Подключение</h2>
      <label>Компания для текстовых проверок <select value={workspace} onChange={e => setWorkspace(e.target.value)}><option value="">Без компании</option>{data?.workspaces.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}</select></label>
      <p>Дневной лимит: общий {data?.limits.global || "без ограничения"}; на компанию {data?.limits.workspace || "без ограничения"} токенов.</p>
      {[...(data?.functions || []), ...(data ? [data.stt] : [])].map(f => <div key={f.feature} className="panel">
        <h3>{names[f.feature]}</h3><p>Провайдер: {f.provider || "не задан"} · Модель: {f.model || "не задана"} · Ключ: {f.key_set ? "задан" : "не задан"} · {f.configured ? "Настроено" : "Не настроено"}</p>
        {f.feature === "stt" && <label>WAV с русской фразой, до 60 секунд <input type="file" accept=".wav,audio/wav" onChange={e => setFile(e.target.files?.[0] || null)} /></label>}
        <button className="button primary" disabled={!!busy || !f.configured} onClick={() => test(f.feature)}>{busy === f.feature ? "Проверяем…" : "Проверить"}</button>
        {answers[f.feature] !== undefined && <pre style={{whiteSpace: "pre-wrap", overflowWrap: "anywhere"}}>{JSON.stringify(answers[f.feature], null, 2)}</pre>}
      </div>)}
      <p>Проверка кампаний использует серверную конфигурацию. Собственный ключ кампании проверяйте в её настройках. Тарифные права клиентов проверяются отдельно.</p>
    </section>
    <section className="panel"><h2>Расход</h2><select value={days} onChange={e => setDays(Number(e.target.value))}><option value={1}>Сегодня</option><option value={7}>7 дней</option><option value={30}>30 дней</option></select>
      <div style={{overflowX: "auto"}}><table><thead><tr>{["Функция", "Модель", "Компания", "Запросы", "Ошибки", "Токены вход / выход", "Аудио, сек.", "Оценка, ₽"].map(x => <th key={x}>{x}</th>)}</tr></thead><tbody>{rows.map((r, i) => <tr key={i}><td>{names[r.feature]}</td><td>{r.model}</td><td>{data?.workspaces.find(w => w.id === r.workspace_id)?.name || "Без компании"}</td><td>{r.requests}</td><td>{r.errors}</td><td>{r.tokens_in} / {r.tokens_out}</td><td>{r.audio_seconds.toFixed(1)}</td><td>{r.price_known ? r.cost_rub.toFixed(4) : "Цена не задана"}</td></tr>)}</tbody></table></div>
      {!rows.length && <p>Вызовов за выбранный период нет.</p>}<p>Стоимость приблизительная, по AI_PRICES. Стоимость секунд STT не включена.</p>
    </section>
  </>;
}
