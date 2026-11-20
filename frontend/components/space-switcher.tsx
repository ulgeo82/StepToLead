"use client";

/* Переключатель клиента в разделах «Лидогенерация» и «Аутрич»: база, списки, ящики, кампании и входящие
   показываются в пространстве выбранного клиента. Пока клиент один (только агентство) — переключателя нет.
   Смена клиента перезагружает экран с его корня: открытый список или кампания другого клиента недоступны. */

import { usePathname, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { chooseSpace, companyFromPath, currentSpace, isSpacePage, sectionRoot } from "@/lib/space";
import css from "./space-switcher.module.css";

type Space = { id: number; name: string; is_home: boolean; companies: number; mailboxes: number; active_campaigns: number };

export default function SpaceSwitcher() {
  const pathname = usePathname() || "";
  const search = useSearchParams();
  const shown = isSpacePage(pathname);
  const [items, setItems] = useState<Space[] | null>(null);
  const [space, setSpace] = useState<number | null>(null);
  const [error, setError] = useState("");

  useEffect(() => { if (shown) setSpace(currentSpace()); }, [shown, pathname, search]);
  useEffect(() => {
    if (!shown || items) return;
    api<{ items: Space[] }>("/admin/leadgen/spaces", { noSpace: true })
      .then(result => {
        setItems(result.items);
        const chosen = currentSpace();
        if (chosen && !result.items.some(item => item.id === chosen)) {   // клиента отключили — возвращаемся к агентству
          chooseSpace(null);
          window.location.assign(sectionRoot(window.location.pathname));
        }
      })
      .catch(reason => setError(reason instanceof Error ? reason.message : "Не удалось загрузить клиентов"));
  }, [shown, items]);

  if (!shown || companyFromPath(pathname)) return null;   // внутри компании клиент уже выбран адресом
  if (error) return <p className={`${css.bar} ${css.error}`} role="alert">Клиенты не загрузились: {error}</p>;
  if (!items) return null;
  if (items.length < 2) return <p className={css.bar}>
    Все данные сейчас — агентства. Чтобы у компании были свои аккаунты, прокси, кампании и входящие, заведите её
    в разделе «Клиенты» — здесь появится переключатель.</p>;
  const home = items.find(item => item.is_home);
  const value = space && items.some(item => item.id === space) ? space : home?.id ?? items[0].id;
  const active = items.find(item => item.id === value);

  function change(id: number) {
    chooseSpace(id === home?.id ? null : id);
    window.location.assign(sectionRoot(window.location.pathname));
  }

  return <div className={css.bar} data-client={active && !active.is_home ? "true" : "false"}>
    <label>Клиент
      <select value={value} onChange={event => change(Number(event.target.value))} aria-label="Клиент, в пространстве которого идёт работа">
        {items.map(item => <option key={item.id} value={item.id}>{item.name}{item.is_home ? " — агентство" : ""}</option>)}
      </select>
    </label>
    {active && <small>компаний в базе: {active.companies} · ящиков: {active.mailboxes} · кампаний идёт: {active.active_campaigns}</small>}
  </div>;
}
