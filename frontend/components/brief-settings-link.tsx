import Link from "next/link";
export default function BriefSettingsLink({projectId}:{projectId:number}){return <section className="resultPanel settingsCard"><h2>Бриф</h2><p>Заполните вручную или загрузите запись встречи. Проверьте ответы и подтвердите бриф перед применением знаний.</p><Link className="settingsOutline" href={`/brief?project_id=${projectId}`}>Открыть бриф</Link></section>;}
