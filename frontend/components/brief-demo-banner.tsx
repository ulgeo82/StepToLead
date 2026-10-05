"use client";
import {useEffect,useState} from "react";
import {api} from "@/lib/api";
import "./brief.css";
export default function BriefDemoBanner({projectId}:{projectId:number|null}){const [info,setInfo]=useState<{demo:unknown;booking_url:string}|null>(null);useEffect(()=>{if(projectId)api<{demo:unknown;booking_url:string}>(`/brief/projects/${projectId}/demo-status`).then(setInfo).catch(()=>setInfo(null));},[projectId]);return info?.demo?<div className="briefDemoBanner">Это демо. Хотите так же на ваших данных?<a href={info.booking_url}>Записаться на разбор →</a></div>:null;}
