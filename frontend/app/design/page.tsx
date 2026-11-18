/* Витрина дизайн-системы «Глубина» (docs/DESIGN.md): все примитивы на одной странице — для проверки и
   для тех, кто делает новые экраны. Не индексируется. */
import type { Metadata } from "next";
import Sidebar from "@/components/Sidebar";
import styles from "@/components/Sidebar.module.css";

export const metadata: Metadata = { title: "Дизайн-система — StepToLead", robots: { index: false, follow: false } };

const rows = [
  { name: "Анна · @onespot_anna", stage: "Общение", day: 7, sent: 5, state: "live" },
  { name: "Борис · @onespot_boris", stage: "Каналы и чтение", day: 3, sent: 0, state: "live" },
  { name: "+994 51 786 37 94", stage: "Подготовка", day: 0, sent: 0, state: "caution" },
  { name: "+7 706 745 46 82", stage: "Готов", day: 21, sent: 12, state: "alert" },
];

export default function DesignPage() {
  return <div className={`shell ${styles.shell}`}><Sidebar/><main className="main">
    <div className="page">
      <header className="pageHeader"><div><p className="eyebrow">Telegram-аутрич</p><h1>Прогрев аккаунтов</h1>
        <p className="subtitle">Подготовка, живая активность днём и проверка @SpamBot раз в сутки. Лимит холодных касаний
          задаёте вы — здесь рекомендация по возрасту аккаунта.</p></div>
        <div className="pageHeaderActions"><button className="button secondary">Настройки</button><button className="button primary">Запустить прогрев</button></div>
      </header>
      <div className="notice" role="status">Прогрев запущен для 4 аккаунтов. Первая сессия — в ближайшие 20 минут.<button aria-label="Закрыть">×</button></div>
      <div className="metricsGrid">
        {[["В прогреве", "18"], ["Готовы к рассылке", "6"], ["Сообщений своим сегодня", "142"], ["Спам-блок", "0"]].map(([l, v]) =>
          <article key={l} className="metricCard"><div className="metricTop"><span>{l}</span></div><strong>{v}</strong></article>)}
      </div>
      <section className="tablePanel">
        <div className="tableTitle"><h2>Аккаунты</h2><span>4 из 18</span></div>
        <div className="filters"><input placeholder="Поиск по имени или номеру" style={{ flex: 1 }}/><select><option>Любой этап</option></select></div>
        <div className="tableWrap"><table><thead><tr><th><input type="checkbox" aria-label="Все"/></th><th>Аккаунт</th><th>Этап</th><th>День</th><th>Холодных сегодня</th></tr></thead>
          <tbody>{rows.map((r, i) => <tr key={r.name}><td><input type="checkbox" defaultChecked={i === 0} aria-label="Отметить"/></td>
            <td>{r.name}</td><td><span className={`chip ${r.state}`}>{r.stage}</span></td><td>{r.day}</td><td>{r.sent}</td></tr>)}</tbody></table></div>
      </section>
      <div className="modal" style={{ marginTop: 28 }}>
        <div className="modalHead"><div><p className="eyebrow">Telegram</p><h2>Добавить по номеру</h2></div><button className="close" aria-label="Закрыть">×</button></div>
        <label>Номер телефона<input placeholder="+7 900 000-00-00" style={{ width: "100%", marginTop: 6 }}/></label>
        <label>Прокси<select style={{ width: "100%", marginTop: 6 }}><option>Автоматически — свободный (12)</option></select></label>
        <button className="button primary full">Получить код</button>
      </div>
    </div>
  </main></div>;
}
