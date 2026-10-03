"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { day, request, when } from "./shared";

type Item = { name: string; qty: number; unit: string; price: number; discount: number };
type Buyer = { name: string; company: string; inn: string; phone: string; email: string };
export type CrmDoc = { id: number; deal_id: number; kind: "offer" | "invoice"; kind_name: string; number: number; title: string | null;
  items: Item[]; total: number; note: string | null; valid_until: string | null; buyer: Buyer; status: string; views: number;
  viewed_at: string | null; paid_at: string | null; created_at: string; url: string };
type DocList = { items: CrmDoc[]; catalog: { name: string; unit: string; price: number }[]; buyer: Buyer;
  requisites_ready: boolean; invoice_ready: boolean; can_edit_requisites: boolean };

const statusNames: Record<string, string> = { draft: "черновик", sent: "отправлен", viewed: "клиент открыл", paid: "оплачен", canceled: "отозван" };
const rub = (n: number) => `${n.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽`;
const lineSum = (i: Item) => Math.round(i.price * i.qty * (100 - (i.discount || 0))) / 100;
const blank = (): Item => ({ name: "", qty: 1, unit: "шт.", price: 0, discount: 0 });

/** «КП и счета» block in the deal card: list, quick actions, editor. */
export function DealDocuments({ dealId, projectId, canEdit, canSale, onChanged }: { dealId: number; projectId: number; canEdit: boolean;
  canSale: boolean; onChanged: () => void }) {
  const [data, setData] = useState<DocList | null>(null);
  const [editing, setEditing] = useState<CrmDoc | "offer" | "invoice" | null>(null);
  const [paying, setPaying] = useState<CrmDoc | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const load = useCallback(() => request<DocList>(`/crm/deals/${dealId}/documents`, "GET").then(setData).catch(e => setError((e as Error).message)), [dealId]);
  useEffect(() => { load(); }, [load]);

  async function act(action: () => Promise<unknown>, message?: string) {
    setError(""); setNotice("");
    try { await action(); await load(); onChanged(); if (message) setNotice(message); } catch (e) { setError((e as Error).message); }
  }
  async function copy(doc: CrmDoc) {
    try { await navigator.clipboard.writeText(doc.url); } catch { window.prompt("Ссылка для клиента", doc.url); return; }
    if (doc.status === "draft") await act(() => request(`/crm/documents/${doc.id}`, "PUT", { status: "sent" }), "Ссылка скопирована — отправьте её клиенту");
    else setNotice("Ссылка скопирована");
  }
  if (!data) return error ? <div className="crmBlock"><h3>КП и счета</h3><p className="crmFormError">{error}</p></div> : null;
  return <div className="crmBlock docBlock"><div className="crmBlockHead"><h3>КП и счета</h3>
      {canEdit && <span className="docAdd"><button className="crmLinkButton" onClick={() => setEditing("offer")}>＋ КП</button>
        <button className="crmLinkButton" onClick={() => setEditing("invoice")}>＋ Счёт</button></span>}</div>
    {!data.items.length && <p className="crmMuted">Соберите коммерческое предложение или счёт из позиций — клиент откроет его по ссылке с телефона, а вы увидите, когда он посмотрел.</p>}
    {error && <p className="crmFormError">{error}</p>}{notice && <p className="crmOk">{notice}</p>}
    {data.items.map(doc => <div key={doc.id} className={`docRow ${doc.status}`}>
      <div><b>{doc.kind === "offer" ? "КП" : "Счёт"} № {doc.number}{doc.title ? ` · ${doc.title}` : ""}</b>
        <small>{day(doc.created_at)} · {rub(doc.total)} · <span className={`docStatus ${doc.status}`}>{statusNames[doc.status] || doc.status}</span>
          {doc.viewed_at ? ` · открыт ${when(doc.viewed_at)}${doc.views > 1 ? ` (${doc.views} раз)` : ""}` : ""}</small></div>
      <div className="docActions">
        <a className="crmLinkButton" href={`/api/crm/documents/${doc.id}/view`} target="_blank" rel="noopener noreferrer">Открыть</a>
        {doc.status !== "canceled" && <button className="crmLinkButton" onClick={() => copy(doc)}>Ссылка</button>}
        {canEdit && doc.status !== "paid" && doc.status !== "canceled" && <button className="crmLinkButton" onClick={() => setEditing(doc)}>Изменить</button>}
        {canEdit && doc.kind === "invoice" && !["paid", "canceled"].includes(doc.status) && <button className="crmLinkButton" onClick={() => setPaying(doc)}>Оплачен</button>}
        {canEdit && !["paid", "canceled"].includes(doc.status) && <button className="crmLinkButton muted" title="Ссылка перестанет открываться"
          onClick={() => act(() => request(`/crm/documents/${doc.id}`, "PUT", { status: "canceled" }), "Документ отозван")}>Отозвать</button>}
      </div></div>)}
    {editing && <DocumentEditor dealId={dealId} projectId={projectId} list={data} doc={typeof editing === "string" ? null : editing}
      kind={typeof editing === "string" ? editing : editing.kind} onClose={() => setEditing(null)}
      onSaved={async (doc, created) => { setEditing(null); await load(); onChanged(); setNotice(created ? `${doc.kind_name} № ${doc.number} готов — скопируйте ссылку и отправьте клиенту` : "Сохранено"); }}/>}
    {paying && <PaidModal doc={paying} canSale={canSale} onClose={() => setPaying(null)} onDone={async message => { setPaying(null); await load(); onChanged(); setNotice(message); }}/>}
  </div>;
}

function PaidModal({ doc, canSale, onClose, onDone }: { doc: CrmDoc; canSale: boolean; onClose: () => void; onDone: (message: string) => void }) {
  const [sale, setSale] = useState(canSale);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await request(`/crm/documents/${doc.id}`, "PUT", { status: "paid" });
      if (sale) await request(`/crm/deals/${doc.deal_id}/sales`, "POST", { amount: doc.total, occurred_at: null, comment: `Оплата по счёту № ${doc.number}` });
      onDone(sale ? "Счёт оплачен, продажа подтверждена — она попала в выручку и ROMI" : "Счёт отмечен оплаченным");
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><form className="resultModal crmModal" onSubmit={submit}>
    <header><h2>Счёт № {doc.number} оплачен</h2><button type="button" onClick={onClose}>×</button></header>
    <p className="crmModalLead">Сумма: <b>{rub(doc.total)}</b></p>
    {canSale && <label className="crmCheck"><input type="checkbox" checked={sale} onChange={e => setSale(e.target.checked)}/> Сразу подтвердить продажу на эту сумму <small>— выручка попадёт в аналитику и рекламные отчёты</small></label>}
    {error && <p className="crmFormError">{error}</p>}
    <button className="resultPrimary" disabled={busy}>{busy ? "Сохраняем…" : "Подтвердить оплату"}</button></form></div>;
}

function DocumentEditor({ dealId, projectId, list, doc, kind, onClose, onSaved }: { dealId: number; projectId: number; list: DocList; doc: CrmDoc | null;
  kind: "offer" | "invoice"; onClose: () => void; onSaved: (doc: CrmDoc, created: boolean) => void }) {
  const [items, setItems] = useState<Item[]>(doc?.items.length ? doc.items : [blank()]);
  const [title, setTitle] = useState(doc?.title || "");
  const [note, setNote] = useState(doc?.note || "");
  const [validDays, setValidDays] = useState(14);
  const [buyer, setBuyer] = useState<Buyer>(() => ({ ...{ name: "", company: "", inn: "", phone: "", email: "" } as Buyer, ...(doc?.buyer || list.buyer) }));
  const [showBuyer, setShowBuyer] = useState(kind === "invoice");
  const [updateAmount, setUpdateAmount] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sum = useMemo(() => items.reduce((acc, i) => acc + lineSum(i), 0), [items]);
  const blocked = kind === "invoice" && !list.invoice_ready;
  const setItem = (index: number, patch: Partial<Item>) => setItems(rows => rows.map((row, i) => i === index ? { ...row, ...patch } : row));
  function pickName(index: number, name: string) {
    const known = list.catalog.find(c => c.name.toLowerCase() === name.trim().toLowerCase());
    setItem(index, known && !items[index].price ? { name, unit: known.unit, price: known.price } : { name });
  }
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    const rows = items.filter(i => i.name.trim()).map(i => ({ ...i, name: i.name.trim(), qty: Number(i.qty) || 1, price: Number(i.price) || 0, discount: Number(i.discount) || 0 }));
    const body = { title, items: rows, note, valid_days: kind === "offer" ? validDays : null, buyer, update_amount: updateAmount };
    try {
      const saved = doc ? await request<CrmDoc>(`/crm/documents/${doc.id}`, "PUT", { ...body, valid_days: kind === "offer" ? validDays : undefined })
        : await request<CrmDoc>(`/crm/deals/${dealId}/documents`, "POST", { ...body, kind });
      onSaved(saved, !doc);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  const settingsLink = `/settings?project_id=${projectId}#requisites`;
  return <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><form className="resultModal crmModal docEditor" onSubmit={submit}>
    <header><h2>{doc ? `${kind === "offer" ? "КП" : "Счёт"} № ${doc.number}` : kind === "offer" ? "Коммерческое предложение" : "Счёт на оплату"}</h2><button type="button" onClick={onClose}>×</button></header>
    {blocked ? <p className="crmWarnLine">Для счёта нужны реквизиты: название, ИНН, банк, БИК и расчётный счёт. {list.can_edit_requisites
      ? <a href={settingsLink}>Заполнить реквизиты →</a> : "Попросите руководителя заполнить их в настройках."}</p>
      : !list.requisites_ready && <p className="crmMuted">Реквизиты компании не заполнены — в шапке документа будет «Компания». {list.can_edit_requisites && <a href={settingsLink}>Заполнить →</a>}</p>}
    {kind === "offer" && <label>Заголовок<input value={title} maxLength={220} onChange={e => setTitle(e.target.value)} placeholder="Кухня угловая 3,2 м — МДФ, фурнитура Blum"/></label>}
    <div className="docItems"><div className="docItemsHead"><span>Позиция</span><span>Кол-во</span><span>Ед.</span><span>Цена, ₽</span><span>Скидка, %</span><span>Сумма</span><span/></div>
      {items.map((row, i) => <div key={i} className="docItem">
        <input aria-label="Позиция" list="docCatalog" required={i === 0} value={row.name} maxLength={300} onChange={e => pickName(i, e.target.value)} placeholder="Например, монтаж"/>
        <input aria-label="Количество" type="number" min="0.001" step="any" value={row.qty} onChange={e => setItem(i, { qty: Number(e.target.value) })}/>
        <input aria-label="Единица" value={row.unit} maxLength={16} onChange={e => setItem(i, { unit: e.target.value })}/>
        <input aria-label="Цена" type="number" min="0" step="0.01" value={row.price} onChange={e => setItem(i, { price: Number(e.target.value) })}/>
        <input aria-label="Скидка" type="number" min="0" max="100" step="any" value={row.discount} onChange={e => setItem(i, { discount: Number(e.target.value) })}/>
        <b>{rub(lineSum(row))}</b>
        <button type="button" aria-label="Убрать позицию" disabled={items.length === 1} onClick={() => setItems(rows => rows.filter((_, j) => j !== i))}>×</button></div>)}
      <datalist id="docCatalog">{list.catalog.map(c => <option key={c.name} value={c.name}>{rub(c.price)}</option>)}</datalist>
      <div className="docItemsFoot"><button type="button" className="crmLinkButton" disabled={items.length >= 100} onClick={() => setItems(rows => [...rows, blank()])}>＋ Позиция</button>
        <span>Итого: <b>{rub(sum)}</b></span></div></div>
    <label>{kind === "offer" ? "Комментарий для клиента" : "Назначение / комментарий"}<textarea value={note} maxLength={3000} onChange={e => setNote(e.target.value)}
      placeholder={kind === "offer" ? "Что входит, сроки изготовления, условия рассрочки" : "Предоплата 50% по договору"}/></label>
    {kind === "offer" && <label>Действует, дней<input type="number" min={1} max={365} value={validDays} onChange={e => setValidDays(Number(e.target.value) || 14)}/></label>}
    <button type="button" className="crmLinkButton" onClick={() => setShowBuyer(v => !v)}>{showBuyer ? "Скрыть данные клиента" : "Данные клиента в документе"}</button>
    {showBuyer && <div className="resultModalGrid">
      <label>Имя<input value={buyer.name} maxLength={180} onChange={e => setBuyer({ ...buyer, name: e.target.value })}/></label>
      <label>Компания<input value={buyer.company} maxLength={240} onChange={e => setBuyer({ ...buyer, company: e.target.value })}/></label>
      <label>ИНН<input value={buyer.inn} maxLength={12} inputMode="numeric" pattern="\d{0,12}" onChange={e => setBuyer({ ...buyer, inn: e.target.value.replace(/\D/g, "") })}/></label>
      <label>Телефон<input value={buyer.phone} maxLength={60} onChange={e => setBuyer({ ...buyer, phone: e.target.value })}/></label>
      <label>Email<input value={buyer.email} maxLength={160} onChange={e => setBuyer({ ...buyer, email: e.target.value })}/></label></div>}
    <label className="crmCheck"><input type="checkbox" checked={updateAmount} onChange={e => setUpdateAmount(e.target.checked)}/> Записать сумму документа в бюджет сделки</label>
    {error && <p className="crmFormError">{error}</p>}
    <button className="resultPrimary" disabled={busy || blocked || !items.some(i => i.name.trim())}>{busy ? "Сохраняем…" : doc ? "Сохранить" : "Создать и получить ссылку"}</button>
  </form></div>;
}

type Requisites = Record<string, string>;
const reqFields: [string, string, string?][] = [["company", "Название (ООО «…», ИП …)"], ["inn", "ИНН", "\\d{10}|\\d{12}"], ["kpp", "КПП", "\\d{9}"], ["ogrn", "ОГРН / ОГРНИП", "\\d{13}|\\d{15}"],
  ["address", "Юридический адрес"], ["director", "Руководитель (для подписи в счёте)"], ["bank", "Банк"], ["bik", "БИК", "\\d{9}"],
  ["account", "Расчётный счёт", "\\d{20}"], ["corr_account", "Корр. счёт", "\\d{20}"], ["phone", "Телефон в документах"], ["email", "Email в документах"], ["site", "Сайт"]];

/** Settings → «Компания»: requisites and texts for offers and invoices. */
export function RequisitesCard({ projectId }: { projectId: number }) {
  const [data, setData] = useState<{ requisites: Requisites; vat_modes: Record<string, string>; can_manage: boolean } | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => { request<typeof data>(`/crm/projects/${projectId}/requisites`, "GET").then(setData).catch(e => setError((e as Error).message)); }, [projectId]);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(""); setNotice("");
    const f = new FormData(event.currentTarget);
    const body: Record<string, string> = {};
    for (const key of [...reqFields.map(r => r[0]), "vat", "offer_intro", "offer_terms", "invoice_terms", "accent"]) body[key] = String(f.get(key) ?? "").trim();
    try { setData(await request(`/crm/projects/${projectId}/requisites`, "PUT", body)); setNotice("Реквизиты сохранены"); } catch (e) { setError((e as Error).message); }
  }
  if (!data) return error ? <section className="resultPanel settingsCard"><p className="crmFormError">{error}</p></section> : null;
  const r = data.requisites; const off = !data.can_manage;
  return <section className="resultPanel settingsCard" id="requisites"><h2>Реквизиты для КП и счетов</h2>
    <p>Подставляются в коммерческие предложения и счета, которые менеджеры собирают в карточке сделки. В уже выставленных документах реквизиты не меняются.</p>
    {error && <p className="crmFormError">{error}</p>}{notice && <p className="crmOk">{notice}</p>}
    <form className="reqForm" onSubmit={save}><div className="reqGrid">
      {reqFields.map(([key, label, pattern]) => <label key={key} className={key === "company" || key === "address" ? "wide" : ""}>{label}
        <input name={key} defaultValue={r[key] || ""} disabled={off} pattern={pattern} inputMode={pattern ? "numeric" : undefined}/></label>)}
      <label>НДС<select name="vat" defaultValue={r.vat || "none"} disabled={off}>{Object.entries(data.vat_modes).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
      <label>Фирменный цвет<input name="accent" type="color" defaultValue={r.accent || "#006BFD"} disabled={off}/></label></div>
      <label>Вступление в КП<textarea name="offer_intro" rows={3} maxLength={2000} defaultValue={r.offer_intro} disabled={off} placeholder="Спасибо за интерес к нашим кухням! Подготовили расчёт по вашему проекту."/></label>
      <label>Условия в КП<textarea name="offer_terms" rows={3} maxLength={2000} defaultValue={r.offer_terms} disabled={off} placeholder="Срок изготовления 25–35 рабочих дней. Гарантия 2 года. Рассрочка 0% на 6 месяцев."/></label>
      <label>Условия в счёте<textarea name="invoice_terms" rows={2} maxLength={1000} defaultValue={r.invoice_terms} disabled={off} placeholder="Счёт действителен 5 банковских дней."/></label>
      {data.can_manage && <button className="crmPrimary">Сохранить реквизиты</button>}</form></section>;
}
