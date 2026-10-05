"use client";
import {useEffect,useState} from "react";
import BriefForm from "@/components/brief-form";
import ExpressQuiz from "@/components/express-quiz";
export default function Page(){const [project,setProject]=useState<number|undefined>(),[ready,setReady]=useState(false);useEffect(()=>{const id=Number(new URLSearchParams(location.search).get("project_id"));setProject(id>0?id:undefined);setReady(true);},[]);return ready?(project?<BriefForm key={project} projectId={project}/>:<ExpressQuiz/>):<p>Загрузка…</p>;}
