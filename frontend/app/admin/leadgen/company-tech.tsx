import { date, TechProfile } from "./shared";
import styles from "./leadgen.module.css";

const labels: Record<string, string> = {
  tilda: "Тильда", wordpress: "WordPress", bitrix: "1С-Битрикс", other: "Другая",
  yandex_metrika: "Яндекс Метрика", google_analytics: "Google Analytics", calltouch: "Calltouch",
  comagic: "CoMagic", roistat: "Roistat", mango: "Mango", callibri: "Callibri", jivo: "Jivo",
  bitrix24: "Битрикс24", "talk-me": "Talk-Me", amocrm: "amoCRM",
};
const names = (values: string[], hidden = false) => values.map(code => labels[code] || code).join(", ")
  || (hidden ? "Не видно (возможно, через Tag Manager)" : "Не обнаружено");

export function CompanyTech({ tech }: { tech?: TechProfile | null }) {
  return <section className={styles.companyTech} aria-labelledby="company-tech-title">
    <h3 id="company-tech-title">Тех-профиль сайта</h3>
    {!tech ? <p className={styles.muted}>Сайт ещё не проверяли.</p> : <>
      <p className={styles.muted}>Проверен: {date(tech.checked_at)}</p>
      <dl className={styles.facts}>{[
        ["Платформа", tech.platform ? labels[tech.platform] || tech.platform : "Не определена"],
        ["Яндекс Метрика", tech.analytics.includes("yandex_metrika") ? "Обнаружена"
          : tech.tag_manager ? "Не видно (возможно, через Tag Manager)" : "Не обнаружена"],
        ["Аналитика", names(tech.analytics, tech.tag_manager)],
        ["Коллтрекинг", names(tech.calltracking, tech.tag_manager)],
        ["Google Tag Manager", tech.tag_manager ? "Есть — часть счётчиков может быть скрыта" : "Нет"],
        ["Онлайн-чат", names(tech.chat)], ["CRM-формы", names(tech.crm_forms)],
        ["Квиз", tech.quiz ? "Есть" : "Не обнаружен"],
        ["Политика персональных данных", tech.privacy_policy ? "Есть" : "Не обнаружена"],
        ["Год в подвале", tech.footer_year ?? "Не найден"],
        ["Скорость на телефоне (PageSpeed)", tech.pagespeed_mobile == null ? "Не проверяли"
          : `${tech.pagespeed_mobile}/100`],
      ].map(([label, value]) => <div key={label} className={styles.wide}>
        <dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    </>}
  </section>;
}
