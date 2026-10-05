"use client";
import Link from "next/link";
import {useEffect,useState} from "react";
import {api} from "@/lib/api";
import {BaselineCard} from "./brief-baseline";
export function ClientBriefEntry({workspaceId}:{workspaceId:number}){
 const [projects,setProjects]=useState<{id:number;name:string;workspace_id:number}[]>([]),[id,setId]=useState(0),[error,setError]=useState("");
 useEffect(()=>{api<typeof projects>("/admin/briefs/targets").then(rows=>{const ps=rows.filter(p=>p.workspace_id===workspaceId);setProjects(ps);setId(ps[0]?.id||0);}).catch(e=>setError(e.message));},[workspaceId]);
 return <section className="briefShell"><h2>Бриф клиента</h2>{error&&<p role="alert">{error}</p>}<select aria-label="Проект брифа" value={id} onChange={e=>setId(Number(e.target.value))}>{projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select>{id>0&&<><Link href={`/brief?project_id=${id}`}>Открыть полный бриф</Link><BaselineCard key={id} projectId={id}/></>}</section>;
}
