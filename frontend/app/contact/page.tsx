import Link from "next/link";
import "../growth/growth.css";
export default function Contact() {
  return <main className="growth g-login"><section className="g-card"><Link className="g-brand" href="/growth"><span>↗</span>StepToLead</Link><h1>Разобрать экономику роста</h1><p className="g-muted">Прямой канал связи StepToLead пока настраивается. Вы уже можете оставить контакты вместе с расчётом — заявка сохранится в закрытом кабинете команды.</p><Link className="g-button" href="/growth#save-calculation">К расчёту и форме заявки</Link></section></main>;
}
