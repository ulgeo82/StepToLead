"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import AddToCampaignDialog from "../../../outreach/campaigns/AddToCampaignDialog";
import { AddToListDialog, CompanyAction, CompanyCard } from "../../company-dialogs";
import ExportDialog from "../../ExportDialog";
import ImportDialog from "../../ImportDialog";
import { base, LeadList, Pipeline, post, signalLabels, stages } from "../../shared";
import { CompanySource, shortError } from "../../sources/source-types";
import styles from "../../leadgen.module.css";
import css from "./workspace.module.css";
import { CompactActions, WorkspaceAction } from "./compact-actions";
import { Popover } from "./popover";
import { FunctionPanel } from "./function-panel";
import { SidePanel } from "./side-panel";
import { WorkspaceTable } from "./workspace-table";
import {
  ColumnPreference, columnPreferences, recentKey, RecentList, recentLists, rememberList,
  safeRead, safeWrite, Summary, TableData, TableRow, tableQuery, tsv, View,
} from "./workspace-utils";

const initialView: View = { q: "", sort: "", dir: "asc", page: 1, filters: {} };

function Workspace({ id }: { id: number }) {
  const [lists, setLists] = useState<LeadList[]>([]);
  const [list, setList] = useState<LeadList | null>(null);
  const [tabs, setTabs] = useState<RecentList[]>([]);
  const [name, setName] = useState<string | null>(null);
  const [metaError, setMetaError] = useState("");
  const [sourceError, setSourceError] = useState("");
  const [tableError, setTableError] = useState("");
  const [sources, setSources] = useState<CompanySource[]>([]);
  const [sourceValues, setSourceValues] = useState<string[]>([]);
  const [data, setData] = useState<TableData | null>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<View>(initialView);
  const [revision, setRevision] = useState(0);
  const [columns, setColumns] = useState<ColumnPreference[]>([]);
  const [wrap, setWrap] = useState(false);
  const [duplicates, setDuplicates] = useState(false);
  const [aside, setAside] = useState(false);
  const workspace = useRef<HTMLDivElement>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [allSelected, setAllSelected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [verified, setVerified] = useState<number | null>(null);
  const [withEmail, setWithEmail] = useState<number | null>(null);
  const [companyId, setCompanyId] = useState<number | null>(null);
  const [campaign, setCampaign] = useState<number[] | null>(null);
  const [toList, setToList] = useState<number[] | null>(null);
  const [importing, setImporting] = useState(false);
  const [exporting, setExporting] = useState<{ format: "xlsx" | "csv"; ids: number[] } | null>(null);
  const [pipeline, setPipeline] = useState<Pipeline | null>(null);
  const operation = useRef<AbortController | null>(null);
  const changed = useCallback(() => setRevision(value => value + 1), []);
  const closeCard = useCallback(() => setCompanyId(null), []);
  const closeImport = useCallback(() => setImporting(false), []);
  const closeCampaign = useCallback(() => setCampaign(null), []);
  const closeList = useCallback(() => setToList(null), []);
  const closeExport = useCallback(() => setExporting(null), []);
  const storageKey = `stl:leadgen:list:${id}:columns`;
  const locked = busy || running;
  const query = tableQuery(view);
  const labels = useMemo(() => ({ ...Object.fromEntries(sources.flatMap(source =>
    source.signals.map(signal => [signal.kind, signal.label]))), ...signalLabels }), [sources]);
  const sourceLabels = Object.fromEntries(sourceValues.map(code => [code,
    sources.find(source => source.code === code)?.title || ({ import: "Импорт", manual: "Вручную",
      site: "Сайт", yandex_direct: "Яндекс Директ", yandex_organic: "Поисковая выдача" } as Record<string, string>)[code] || code,
  ]));

  useEffect(() => () => operation.current?.abort(), []);
  useEffect(() => {
    const node = workspace.current;
    if (!node) return;
    function size() {
      node?.style.setProperty("--workspace-top", `${Math.max(0, node.getBoundingClientRect().top)}px`);
    }
    size();
    const observer = new ResizeObserver(size);
    if (node.parentElement) observer.observe(node.parentElement);
    window.addEventListener("resize", size);
    return () => { observer.disconnect(); window.removeEventListener("resize", size); };
  }, []);
  function togglePanel(value: boolean) {
    setAside(value);
    safeWrite(`stl:leadgen:list:${id}:panel`, value);
  }
  useEffect(() => {
    setTabs(recentLists(safeRead(recentKey)));
    const value = safeRead(`stl:leadgen:list:${id}:wrap`);
    if (typeof value === "boolean") setWrap(value);
    setAside(safeRead(`stl:leadgen:list:${id}:panel`) === true);
  }, [id]);
  useEffect(() => {
    const controller = new AbortController();
    setMetaError("");
    async function load() {
      const active = await api<{ items: LeadList[] }>(`${base}/lists`, { signal: controller.signal });
      let found = active.items.find(item => item.id === id);
      if (!found) {
        const archived = await api<{ items: LeadList[] }>(`${base}/lists?archived=true`, { signal: controller.signal });
        found = archived.items.find(item => item.id === id);
      }
      if (controller.signal.aborted) return;
      setLists(active.items);
      if (!found) throw new Error("База не найдена или недоступна");
      setList(found);
      const next = rememberList(safeRead(recentKey), { id: found.id, name: found.name });
      setTabs(next);
      safeWrite(recentKey, next);
    }
    load().catch(e => !controller.signal.aborted && setMetaError(shortError(e)));
    return () => controller.abort();
  }, [id, revision]);
  useEffect(() => {
    const controller = new AbortController();
    api<{ items: CompanySource[] }>(`${base}/sources`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) { setSources(value.items); setSourceError(""); } })
      .catch(e => !controller.signal.aborted && setSourceError(shortError(e)));
    api<Pipeline>(`${base}/outreach/pipeline`, { signal: controller.signal })
      .then(value => !controller.signal.aborted && setPipeline(value)).catch(() => undefined);
    api<Summary>(`${base}/lists/${id}/summary?column=source`, { signal: controller.signal })
      .then(value => !controller.signal.aborted && setSourceValues(value.items.flatMap(item => item.value || [])))
      .catch(() => undefined);
    return () => controller.abort();
  }, [id, revision]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setTableError("");
    const timer = setTimeout(() => {
      api<TableData>(`${base}/lists/${id}/table?${query}`, { signal: controller.signal })
        .then(value => {
          if (controller.signal.aborted) return;
          setData(value);
          setColumns(columnPreferences(value.columns, safeRead(storageKey)));
          const last = Math.max(1, Math.ceil(value.total / 100));
          if (view.page > last) setView(current => ({ ...current, page: last }));
        }).catch(e => !controller.signal.aborted && setTableError(shortError(e)))
        .finally(() => !controller.signal.aborted && setLoading(false));
    }, 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [id, query, revision, storageKey, view.page]);
  useEffect(() => {
    const controller = new AbortController();
    setVerified(null);
    setWithEmail(null);
    const query = new URLSearchParams({ column: "email_status", "f.stage": `in:${Object.keys(stages).join(",")}` });
    api<Summary>(`${base}/lists/${id}/summary?${query}`, { signal: controller.signal })
      .then(value => {
        if (controller.signal.aborted) return;
        setVerified(value.items.find(item => item.value === "valid")?.count ?? 0);
        setWithEmail(value.items.filter(item => item.value && item.value !== "none")
          .reduce((total, item) => total + item.count, 0));
      }).catch(() => undefined);
    return () => controller.abort();
  }, [id, revision]);
  const visibleColumns = columns.filter(item => item.visible).flatMap(item =>
    data?.columns.find(column => column.key === item.key) || []);
  const selectedCount = allSelected ? list?.total ?? 0 : selected.length;

  function updateView(next: View) {
    if (next.q !== view.q || JSON.stringify(next.filters) !== JSON.stringify(view.filters)) {
      setSelected([]);
      setAllSelected(false);
    }
    setView(next);
  }
  function updateColumns(next: ColumnPreference[]) {
    setColumns(next);
    safeWrite(storageKey, next);
  }
  async function readRows(signal: AbortSignal): Promise<TableRow[]> {
    const rows = new Map<number, TableRow>();
    for (let page = 1, total = 1; (page - 1) * 100 < total; page++) {
      const query = new URLSearchParams({ page: String(page), per_page: "100",
        "f.stage": `in:${Object.keys(stages).join(",")}` });
      const value = await api<TableData>(`${base}/lists/${id}/table?${query}`, { signal });
      total = value.total;
      value.items.forEach(row => rows.set(row.id, row));
      if (!value.items.length && rows.size < total) throw new Error("База изменилась. Обновите страницу и повторите");
      setNotice(`Читаем базу: ${rows.size} из ${total}`);
    }
    if (list && rows.size !== list.total) {
      throw new Error("Не удалось прочитать всю базу. Обновите страницу и повторите действие");
    }
    return [...rows.values()];
  }
  async function selection(signal: AbortSignal) {
    return allSelected ? (await readRows(signal)).map(row => row.id) : [...selected];
  }
  async function execute(action: (signal: AbortSignal) => Promise<void>) {
    if (locked) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy(true);
    setError("");
    setNotice("");
    try { await action(controller.signal); }
    catch (e) { if (!controller.signal.aborted) setError(shortError(e)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  async function bulk(kind: "outreach" | "review" | "hide" | "delete", provided?: number[]) {
    await execute(async signal => {
      const ids = provided || await selection(signal);
      if (!ids.length) return;
      if (kind === "delete" && !window.confirm(
        `Удалить ${ids.length} компаний из общей базы насовсем? Удалятся контакты и история, не только связь со списком.`,
      )) return;
      let completed = 0;
      const details: string[] = [];
      for (let start = 0; start < ids.length; start += 500) {
        const batch = ids.slice(start, start + 500);
        const path = { outreach: "outreach", review: "review", hide: "hide", delete: "delete" }[kind];
        const result = await post<Record<string, unknown>>(`/companies/${path}`, {
          company_ids: batch, ...(kind === "hide" ? { hidden: true } : {}),
          ...(kind === "delete" ? { block_domains: false } : {}),
        }, signal);
        completed += Number(result.updated ?? result.moved ?? result.done ?? result.deleted ?? 0);
        if (Array.isArray(result.skipped) && result.skipped.length) details.push(`Пропущено: ${result.skipped.length}`);
        if (typeof result.skipped === "number" && result.skipped) details.push(`Пропущено: ${result.skipped}`);
        setNotice(`Обработано ${Math.min(start + batch.length, ids.length)} из ${ids.length}. Изменено: ${completed}`);
        changed();
      }
      setNotice(`Изменено: ${completed}${details.length ? ` · ${details.join(" · ")}` : ""}`);
      setSelected([]);
      setAllSelected(false);
    });
  }
  async function cardAction(action: CompanyAction, ids: number[]) {
    if (action === "outreach" || action === "hide") { await bulk(action, ids); return; }
    await execute(async signal => {
      if (action === "dnc") await post("/dnc", { kind: "company", value: String(ids[0]), reason: "refused" }, signal);
      else if (action === "restore") await post("/companies/hide", { company_ids: ids, hidden: false }, signal);
      else await post(`/companies/${action}`, { company_ids: ids, ...(action === "assess" ? { force: true } : {}) }, signal);
      setNotice("Действие выполнено");
      changed();
    });
  }
  async function rename() {
    if (name == null || !name.trim()) return;
    await execute(async signal => {
      await api(`${base}/lists/${id}`, { method: "PATCH", signal,
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim() }) });
      setName(null);
      changed();
    });
  }
  async function dialog(kind: "campaign" | "list" | "xlsx" | "csv") {
    await execute(async signal => {
      const ids = await selection(signal);
      if (kind === "campaign") setCampaign(ids);
      else if (kind === "list") setToList(ids);
      else setExporting({ format: kind, ids: allSelected ? [] : ids });
    });
  }
  function toggle(id: number, checked: boolean) {
    if (allSelected) {
      void execute(async signal => {
        const ids = await selection(signal);
        setAllSelected(false);
        setSelected(checked ? ids : ids.filter(value => value !== id));
      });
    } else setSelected(current => checked ? [...new Set([...current, id])] : current.filter(value => value !== id));
  }
  const feedback = <>{error && <p role="alert" className={styles.error}>{error}</p>}
    {notice && <p role="status" className={styles.notice}>{notice}</p>}</>;


  const actions: WorkspaceAction[] = [
    { label: "В кампанию…", disabled: locked || !selectedCount, click: () => { void dialog("campaign"); } },
    { label: "В CRM", disabled: locked || !selectedCount, click: () => { void bulk("outreach"); } },
    { label: "В другой список…", disabled: locked || !selectedCount, click: () => { void dialog("list"); } },
    { label: "На проверку", disabled: locked || !selectedCount, click: () => { void bulk("review"); } },
    { label: "Скрыть", disabled: locked || !selectedCount, click: () => { void bulk("hide"); } },
    { label: "Удалить", disabled: locked || !selectedCount, danger: true, click: () => { void bulk("delete"); } },
    { label: "Excel", disabled: locked, click: () => { void dialog("xlsx"); } },
    { label: "CSV", disabled: locked, click: () => { void dialog("csv"); } },
    { label: "Копировать", disabled: locked || !selectedCount, click: () => { void execute(async signal => {
      const rows = await readRows(signal);
      await navigator.clipboard.writeText(tsv(allSelected ? rows : rows.filter(row => selected.includes(row.id)),
        visibleColumns));
      setNotice("Строки скопированы в буфер как TSV");
    }); } },
    { label: "Импорт", disabled: locked, click: () => setImporting(true) },
  ];
  return <div ref={workspace} className={`${styles.page} ${css.workspace}`}>
    <header className={css.header} aria-label="База компаний">
      <Link className={css.back} href="/admin/leadgen/lists">← Все списки</Link>
      <nav className={css.tabs} aria-label="Недавно открытые базы">{tabs.map(tab => <span key={tab.id}>
        <Link href={`/admin/leadgen/lists/${tab.id}`} aria-current={tab.id === id ? "page" : undefined}
          title={tab.name}>{tab.name}</Link>
        <button type="button" aria-label={`Закрыть вкладку ${tab.name}`} onClick={() => {
          const next = tabs.filter(item => item.id !== tab.id);
          setTabs(next); safeWrite(recentKey, next);
        }}>×</button>
      </span>)}</nav>
      {list && <>
        {name == null ? <button type="button" className={css.title} disabled={locked}
          title="Переименовать базу" onClick={() => setName(list.name)}>{list.name} ✎</button>
          : <form className={css.rename} onSubmit={e => { e.preventDefault(); void rename(); }}>
            <input autoFocus aria-label="Название базы" maxLength={180} value={name} disabled={locked}
              onChange={e => setName(e.target.value)} onKeyDown={e => e.key === "Escape" && setName(null)}/>
            <button type="submit" className={css.smallButton} disabled={locked || !name.trim()}>Сохранить</button>
            <button type="button" className={css.smallButton} disabled={locked} onClick={() => setName(null)}>Отмена</button>
          </form>}
        <div className={css.counters}>
          <span>Всего: {list.total}</span><span>С почтой: {withEmail ?? "—"}</span>
          <span title="С проверенной почтой">Проверены: {verified ?? "—"}</span>
          <span>Подходят (ИИ): {list.fit}</span><span>Не выгружались: {list.not_exported}</span>
        </div>
      </>}
    </header>
    {metaError && <p role="alert" className={css.cellError}>{metaError}</p>}
    {!list && !metaError && <p role="status">Загружаем базу…</p>}
    {list && <>
      {(error || notice) && <div className={css.feedback}>{feedback}</div>}
      {sourceError && <p role="alert" className={css.cellError}>{sourceError}</p>}
      <FunctionPanel listId={id} sources={sources} ids={selected} total={list.total} allSelected={allSelected}
        locked={busy} changed={changed} running={setRunning} actions={<CompactActions items={actions}/>}/>
      <div className={css.controls}>
        <input className={css.search} aria-label="Поиск по всем колонкам" placeholder="Поиск по всем колонкам"
          value={view.q} disabled={locked} onChange={e => updateView({ ...view, page: 1, q: e.target.value })}/>
        <label><input type="checkbox" checked={duplicates} onChange={e => setDuplicates(e.target.checked)}/>
          Совпадения</label>
        <label><input type="checkbox" checked={wrap} onChange={e => {
          setWrap(e.target.checked); safeWrite(`stl:leadgen:list:${id}:wrap`, e.target.checked);
        }}/> Перенос строк</label>
        <Popover label="Колонки" trigger="Колонки ▾"><div className={css.columns}>
          {columns.map((item, index) => {
            const pinned = ["display_name", "company"].includes(item.key);
            return <div key={item.key} className={css.column}>
              <label><input type="checkbox" checked={item.visible}
                disabled={pinned || item.visible && columns.filter(value => value.visible).length === 1}
                onChange={e => updateColumns(columns.map(value => value.key === item.key
                  ? { ...value, visible: e.target.checked } : value))}/>
                {data?.columns.find(column => column.key === item.key)?.label || item.key}{pinned ? " 🔒" : ""}
              </label>
              {[-1, 1].map(direction => <button type="button" key={direction}
                aria-label={`${direction < 0 ? "Выше" : "Ниже"}: ${item.key}`}
                disabled={pinned || index + direction <= 0 || index + direction >= columns.length}
                onClick={() => {
                  const next = [...columns];
                  [next[index], next[index + direction]] = [next[index + direction], next[index]];
                  updateColumns(next);
                }}>{direction < 0 ? "↑" : "↓"}</button>)}
            </div>;
          })}
        </div></Popover>
        <button type="button" className={css.smallButton} aria-controls="list-side-panel"
          onClick={() => togglePanel(!aside)} aria-expanded={aside}>Панель</button>
        <Popover label="Настройки таблицы" trigger="⋯" align="end">{close => <div className={css.menuItems}>
          <button type="button" disabled={locked} onClick={() => {
            close(); setSelected([]); setAllSelected(false); changed();
          }}>Обновить</button>
          <button type="button" disabled={locked} onClick={() => { close(); updateView(initialView); }}>
            Сбросить фильтры</button>
        </div>}</Popover>
      </div>
      <div className={`${css.layout} ${!aside ? css.closed : ""}`}>
        <section className={css.main} aria-label="Компании базы">
          {loading && <p role="status" className={css.hint}>Загружаем таблицу…</p>}
          {tableError && <p role="alert" className={css.cellError}>{tableError}</p>}
          {data && !tableError && <WorkspaceTable rows={data.items} columns={visibleColumns} view={view}
            setView={updateView} selected={selected} allSelected={allSelected} toggle={toggle}
            selectPage={checked => {
              if (allSelected && !checked) {
                void execute(async signal => {
                  const ids = await selection(signal);
                  const pageIds = new Set(data.items.map(row => row.id));
                  setAllSelected(false); setSelected(ids.filter(value => !pageIds.has(value)));
                });
              } else setSelected(current => checked ? [...new Set([...current, ...data.items.map(row => row.id)])]
                : current.filter(value => !data.items.some(row => row.id === value)));
            }} open={setCompanyId} changed={changed} labels={labels} sourceLabels={sourceLabels}
            wrap={wrap} duplicates={duplicates} locked={locked || loading}/>}
          <footer className={css.pagination}>
            <span>Отмечено: {selectedCount}{allSelected ? " · вся база" : ""}</span>
            <button type="button" className={css.smallButton} disabled={locked || !list.total}
              title="Выбрать всю базу независимо от фильтров"
              onClick={() => { setAllSelected(true); setSelected([]); }}>Вся база ({list.total})</button>
            <button type="button" className={css.smallButton} disabled={locked || !selectedCount}
              onClick={() => { setAllSelected(false); setSelected([]); }}>Снять выбор</button>
            <button type="button" className={css.smallButton} disabled={loading || view.page <= 1}
              onClick={() => setView(current => ({ ...current, page: current.page - 1 }))}>← Назад</button>
            <span>Страница {view.page} из {Math.max(1, Math.ceil((data?.total || 0) / 100))} · {data?.total ?? "—"}</span>
            <button type="button" className={css.smallButton}
              disabled={loading || view.page * 100 >= (data?.total || 0)}
              onClick={() => setView(current => ({ ...current, page: current.page + 1 }))}>Далее →</button>
          </footer>
        </section>
        {aside && <SidePanel id={id} revision={revision} locked={locked} changed={changed} labels={labels}
          close={() => togglePanel(false)}
          filter={(column, value) => updateView({ ...view, page: 1, filters: { ...view.filters, [column]: value } })}/>}
      </div>
    </>}
    {companyId != null && <CompanyCard id={companyId} labels={labels} onClose={closeCard}
      revision={revision} busy={locked} act={cardAction} pipeline={pipeline} feedback={feedback}/>}
    {campaign && <AddToCampaignDialog ids={campaign} close={closeCampaign} done={text => {
      setNotice(text); changed();
    }}/>}
    {toList && <AddToListDialog ids={toList} lists={lists.filter(item => item.id !== id)} close={closeList}
      done={text => { setToList(null); setNotice(text); changed(); }}/>}
    {importing && <ImportDialog listId={id} close={closeImport} completed={changed}/>}
    {exporting && <ExportDialog lists={lists} close={closeExport} done={changed} format={exporting.format}
      selectedIds={exporting.ids} initialLists={exporting.ids.length ? [] : [id]}/>}
  </div>;
}

export default function ListWorkspacePage() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  if (!Number.isSafeInteger(id) || id <= 0) return <p role="alert">База не найдена</p>;
  return <Workspace key={id} id={id}/>;
}
