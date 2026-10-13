"use client";
import {useEffect,useState} from "react";
import {api} from "@/lib/api";
import {DemoLaunch} from "./demo-launch";
import "./brief.css";
type Info={demo:unknown;live:{expires_at:string}|null;booking_url:string};
export default function BriefDemoBanner({projectId}:{projectId:number|null}){
  const [info,setInfo]=useState<Info|null>(null),[open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(""),[qr,setQr]=useState<{url:string;svg:string}|null>(null);
  const showQr=async()=>{setError("");try{setQr(await api<{url:string;svg:string}>(`/brief/demo/test-lead?project_id=${projectId}`));}catch(e){setError(e instanceof Error?e.message:"Не получилось");}};
  useEffect(()=>{if(projectId)api<Info>(`/brief/projects/${projectId}/demo-status`).then(setInfo).catch(()=>setInfo(null));},[projectId]);
  if(!info?.demo||!projectId)return null;
  const goLive=async()=>{if(!window.confirm("Удалим вымышленные данные демо и откроем пустой кабинет на 7 дней. Подключите туда свои рекламные кабинеты — только на чтение, без изменений в рекламе. Продолжить?"))return;
    setBusy(true);setError("");try{const r=await api<{project_id:number}>(`/brief/demo/go-live?project_id=${projectId}`,{method:"POST"});location.assign(`/ads?project_id=${r.project_id}`);}
    catch(e){setError(e instanceof Error?e.message:"Не получилось");setBusy(false);}};
  const days=info.live?Math.max(0,Math.ceil((new Date(info.live.expires_at).getTime()-Date.now())/86400000)):null;
  return <><div className="briefDemoBanner">{info.live?<>Живое демо · осталось {days} дн. Подключите свои рекламные кабинеты — увидим, что реально работает.<a href={`/ads?project_id=${projectId}`}>Подключить кабинет →</a></>
      :<>Это демо на примере. Хотите увидеть свои цифры?<button type="button" className="briefDemoGhost" disabled={busy} onClick={goLive}>{busy?"Готовим…":"Очистить демо и подключить свои кабинеты"}</button></>}
    <button type="button" className="briefDemoGhost" onClick={()=>void showQr()}>📱 Тестовая заявка с телефона</button>
    <button type="button" className="briefDemoCta" onClick={()=>setOpen(true)}>Хочу такой портал →</button>{error&&<small className="briefDemoError">{error}</small>}</div>
    {open&&<DemoLaunch projectId={projectId} onClose={()=>setOpen(false)}/>}
    {qr&&<div className="demoLaunchBackdrop" onMouseDown={e=>{if(e.target===e.currentTarget)setQr(null);}}><section className="demoLaunch demoQr" role="dialog" aria-label="Тестовая заявка">
      <button type="button" className="demoLaunchClose" onClick={()=>setQr(null)} aria-label="Закрыть">×</button>
      <h2>Заявка за секунду</h2><p>Наведите камеру телефона на код и оставьте заявку как ваш клиент. Она сразу появится в колонке «Неразобранное» в CRM, а если вы подключили Telegram в «Мои уведомления» — придёт и туда.</p>
      <div className="demoQrCode" dangerouslySetInnerHTML={{__html:qr.svg}}/><a href={qr.url} target="_blank" rel="noopener noreferrer">{qr.url}</a></section></div>}</>;
}
