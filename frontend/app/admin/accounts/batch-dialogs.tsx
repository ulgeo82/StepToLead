"use client";

import { FormEvent, useState } from "react";
import { api } from "@/lib/api";
import { InfraProgress, safeDiagnostic, toggleId, useInfraTask } from "../telegram-infra";
import type {
  AvatarPreview, AvatarStrength, Batch, ImportBatch, ImportMode, ImportResult, InfraAccount, InfraProxy,
} from "../telegram-types";
import css from "../telegram-infra.module.css";

const base = "/telegram-accounts";
const mb = 1024 * 1024;
const importLabels = { imported: "Добавлен", waiting_proxy: "Ждёт прокси", skipped: "Пропущен", error: "Ошибка" };
const photoTypes = ["image/jpeg", "image/png", "image/webp"];

export function accountName(account: InfraAccount) {
  return [account.first_name, account.last_name].filter(Boolean).join(" ") || account.phone;
}

export function ImportDialog({ proxies, free, onClose, onChanged }: {
  proxies: InfraProxy[];
  free?: number;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [mode, setMode] = useState<ImportMode>("auto");
  const [order, setOrder] = useState<Record<number, string>>({});
  const [label, setLabel] = useState("");
  const [batch, setBatch] = useState<ImportBatch | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const job = useInfraTask();
  const locked = busy || !!job.task?.active;
  const orderedIds = files.map((_, i) => order[i]).filter(Boolean);
  const available = proxies.filter(proxy =>
    proxy.pool === "agency" && proxy.status !== "error" && proxy.health?.level !== "dead");
  const selectionValid = orderedIds.length === files.length &&
    new Set(orderedIds).size === files.length && orderedIds.every(id => available.some(p => String(p.id) === id));

  function selectFiles(next: File[]) {
    setOrder({});
    if (next.length > 200 || next.some(file => !/\.zip$/i.test(file.name) || file.size > 100 * mb)) {
      setFiles([]);
      setError("Выберите до 200 ZIP-архивов, каждый не больше 100 МБ.");
      return;
    }
    setError("");
    setFiles(next);
  }

  async function getResult(id: number) {
    setBusy(true);
    setError("");
    try { setResult(await api<ImportResult>(`${base}/imports/${id}`)); }
    catch { setError("Не удалось загрузить итоги импорта."); }
    finally { setBusy(false); }
  }

  async function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (locked || batch || !files.length || (mode === "order" && !selectionValid)) return;
    const data = new FormData();
    files.forEach(file => data.append("files", file));
    data.set("mode", mode);
    if (mode === "order") data.set("proxy_ids", orderedIds.join(","));
    if (label.trim()) data.set("label", label.trim());
    setBusy(true); setError("");
    try {
      const started = await api<ImportBatch>(`${base}/import-batch`, {
        method: "POST", body: data, timeoutMs: 600_000,
      });
      setBatch(started);
      setFiles([]);
      setOrder({});
      job.start(`${base}/imports/${started.batch_id}/progress`);
    } catch { setError("Не удалось отправить архивы. Проверьте размер файлов и параметры импорта."); }
    finally { setBusy(false); }
  }

  return <div className="modalBackdrop"><form role="dialog" aria-label="Импорт tdata"
    className={`modal tdataModal ${css.modal}`} onSubmit={start}>
    <div className="modalHead"><h2>Импорт tdata</h2>
      <button type="button" disabled={locked} aria-label="Закрыть" onClick={onClose}>×</button>
    </div>
    {!batch && <>
      <p>До 200 ZIP-архивов по 100 МБ. В каждом — папка tdata одного аккаунта.</p>
      <label>Архивы<input type="file" accept=".zip,application/zip" multiple disabled={locked}
        onChange={e => selectFiles(Array.from(e.target.files ?? []))}/></label>
      <label>Режим<select value={mode} disabled={locked} onChange={e => setMode(e.target.value as ImportMode)}>
        <option value="auto">Автоматически раздать прокси</option>
        <option value="order">По порядку</option>
      </select></label>
      <label>Метка (необязательно)<input value={label} disabled={locked} onChange={e => setLabel(e.target.value)}/></label>
      <p>Файлов {files.length} — {mode === "order" ? `прокси ${orderedIds.length}` : `свободных прокси ${free ?? "—"}`}.</p>
      {free != null && files.length > free &&
        <p role="status">{files.length - free} файлов не загрузятся — не хватит прокси.</p>}
      {!!files.length && <div className={css.choices}>
        {files.map((file, index) => <div key={index}>
          {mode === "order" ? <label>{index + 1}. {file.name}
            <select aria-label={`Прокси для файла ${index + 1}`} value={order[index] ?? ""} disabled={locked}
              onChange={e => setOrder(prev => ({ ...prev, [index]: e.target.value }))}>
              <option value="">Выберите прокси</option>
              {available.map(p => <option key={p.id} value={p.id}
                disabled={order[index] !== String(p.id) && orderedIds.includes(String(p.id))}>
                {p.host}:{p.port} · {p.scheme.toUpperCase()}
              </option>)}
            </select>
          </label> : <p>{index + 1}. {file.name}</p>}
        </div>)}
      </div>}
      <button className="button primary" disabled={locked || !files.length || (mode === "order" && !selectionValid)}>
        {busy ? "Загружаем…" : "Импортировать"}
      </button>
    </>}
    {error && <p role="alert">{error}</p>}
    {batch && <div role="status">
      <p>Поставлено в очередь: {batch.queued} · Не хватило прокси: {batch.without_proxy ?? 0}
        {" · "}Отклонено: {batch.rejected?.length ?? 0}</p>
      {!!batch.rejected?.length && <ul>{batch.rejected.map((item, index) =>
        <li key={index}>{item.filename}: {safeDiagnostic(item.error)}</li>)}</ul>}
    </div>}
    <InfraProgress job={job} onFinish={() => {
      if (batch) void getResult(batch.batch_id);
      onChanged();
    }}/>
    {batch && !locked && !result && <button type="button" onClick={() => void getResult(batch.batch_id)}>
      Загрузить итоги
    </button>}
    {result && <div className={css.scroll} tabIndex={0} aria-label="Итоги импорта">
      <table><thead><tr><th>Файл</th><th>Результат</th><th>Телефон</th><th>Прокси</th></tr></thead>
        <tbody>{result.items.map((item, i) => <tr key={i}>
          <td>{item.filename}</td><td>{importLabels[item.status]}
            {item.status === "waiting_proxy" &&
              <small>Добавьте свободный прокси и импортируйте этот файл снова.</small>}
            {item.error && item.status === "error" && <small>Не удалось обработать архив.</small>}</td>
          <td>{item.phone ?? "—"}</td><td>{item.proxy ?? "—"}</td>
        </tr>)}</tbody>
      </table>
    </div>}
  </form></div>;
}

export function AvatarDialog({ accounts, initialIds, onClose, onChanged }: {
  accounts: InfraAccount[];
  initialIds: number[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const [ids, setIds] = useState(() => initialIds.filter(id => accounts.some(a => a.id === id && a.proxy_id)));
  const [files, setFiles] = useState<File[]>([]);
  const [strength, setStrength] = useState<AvatarStrength>("low");
  const [pause, setPause] = useState("40");
  const [preview, setPreview] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const job = useInfraTask();
  const locked = busy || !!job.task?.active;

  async function generate() {
    if (locked || !files.length) return;
    setError(""); setBusy(true); setPreview([]);
    const data = new FormData();
    data.set("file", files[0]);
    data.set("strength", strength);
    try {
      const { variants } = await api<AvatarPreview>(`${base}/avatars/preview`, { method: "POST", body: data });
      const images = variants.filter(url => /^data:image\/(?:jpeg|png|webp);base64,[A-Za-z0-9+/=]+$/i.test(url));
      if (images.length !== 4) throw new Error("Неверное превью");
      setPreview(images);
    } catch { setError("Не удалось получить четыре варианта превью."); }
    finally { setBusy(false); }
  }

  async function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (locked || !ids.length || !files.length || !Number.isSafeInteger(Number(pause))
        || Number(pause) < 10 || Number(pause) > 600) return;
    const data = new FormData();
    files.forEach(file => data.append("files", file));
    data.set("account_ids", ids.join(","));
    data.set("strength", strength);
    data.set("pause_seconds", pause);
    setBusy(true); setError("");
    try {
      const batch = await api<Batch>(`${base}/avatars`, { method: "POST", body: data });
      job.start(`${base}/avatars/${batch.batch_id}/progress`);
    } catch { setError("Не удалось запустить установку аватарок."); }
    finally { setBusy(false); }
  }

  return <div className="modalBackdrop"><form className={`modal ${css.modal}`} role="dialog"
    aria-label="Аватарки" onSubmit={start}>
    <div className="modalHead"><h2>Аватарки</h2>
      <button type="button" disabled={locked} aria-label="Закрыть" onClick={onClose}>×</button>
    </div>
    <p>Уникальная аватарка не защищает от бана сама — главное лимиты и тексты.</p>
    <label>1–20 фото JPG, PNG или WEBP, до 8 МБ каждое
      <input type="file" multiple accept={photoTypes.join(",")} disabled={locked} onChange={e => {
        const next = Array.from(e.target.files ?? []);
        const invalid = next.length > 20 || next.some(file => !photoTypes.includes(file.type) || file.size > 8 * mb);
        setFiles(invalid ? [] : next);
        setError(invalid ? "Выберите до 20 фото JPG, PNG или WEBP по 8 МБ." : "");
        setPreview([]);
      }}/>
    </label>
    <label>Сила<select value={strength} disabled={locked} onChange={e => {
      setStrength(e.target.value as AvatarStrength); setPreview([]);
    }}><option value="low">Слабо</option><option value="medium">Средне</option></select></label>
    <button type="button" disabled={locked || !files.length} onClick={() => void generate()}>
      Превью первого фото
    </button>
    {!!preview.length && <div className={css.preview}>{preview.map((src, index) =>
      // eslint-disable-next-line @next/next/no-img-element
      <img src={src} alt={`Вариант ${index + 1}`} key={index}/>)}</div>}
    <fieldset className={css.choices} disabled={locked}><legend>Аккаунты ({ids.length})</legend>
      {accounts.map(account => <label key={account.id}>
        <input type="checkbox" checked={ids.includes(account.id)} disabled={!account.proxy_id}
          onChange={() => setIds(prev => toggleId(prev, account.id))}/>
        {accountName(account)}{account.phone !== accountName(account) ? ` · ${account.phone}` : ""}
        {!account.proxy_id && " · без прокси"}
      </label>)}
    </fieldset>
    <label>Пауза между аккаунтами, с (10–600)<input type="number" min={10} max={600} step={1} required
      value={pause} disabled={locked} onChange={e => setPause(e.target.value)}/></label>
    {error && <p role="alert">{error}</p>}
    <InfraProgress job={job} onFinish={onChanged}/>
    <button className="button primary" disabled={locked || !files.length || !ids.length || !pause}>
      Поставить аватарки
    </button>
  </form></div>;
}
