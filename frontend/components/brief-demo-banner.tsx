"use client";
import {useEffect,useState} from "react";
import {api} from "@/lib/api";
import {DemoLaunch} from "./demo-launch";
import "./brief.css";
export default function BriefDemoBanner({projectId}:{projectId:number|null}){
  const [info,setInfo]=useState<{demo:unknown;booking_url:string}|null>(null),[open,setOpen]=useState(false);
  useEffect(()=>{if(projectId)api<{demo:unknown;booking_url:string}>(`/brief/projects/${projectId}/demo-status`).then(setInfo).catch(()=>setInfo(null));},[projectId]);
  if(!info?.demo||!projectId)return null;
  return <><div className="briefDemoBanner">Это демо. Хотите так же на ваших данных?<button type="button" className="briefDemoCta" onClick={()=>setOpen(true)}>Хочу такой портал →</button></div>
    {open&&<DemoLaunch projectId={projectId} onClose={()=>setOpen(false)}/>}</>;
}
