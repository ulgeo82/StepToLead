import hashlib
import logging
import re
import secrets
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.routes.access import client_address, rate_limit
from app.api.routes.crm import project_for
from app.api.routes.messaging import can_manage_channels
from app.api.routes.portal import InboundLead
from app.core.access import check_origin, require_admin, require_portal_user
from app.core.config import settings
from app.db import get_db, SessionLocal
from app.models.brief import ClientBrief, ExpressAssessment
from app.models.marketing import ClientWorkspace, LeadInboundSource, PortalUser, Project
from app.services import brief as service, demo, ai
from app.services.inbound_lead import create_inbound
from app.services.project_scope import default_project

router = APIRouter(tags=["brief"])
admin = APIRouter(prefix="/admin/briefs", dependencies=[Depends(require_admin)])


def output(row):
    return {**{k: getattr(row, k) for k in ("id", "workspace_id", "project_id", "lead_id", "demo_workspace_id", "answers", "preview", "source", "status", "created_at", "updated_at", "expires_at", "transcript", "ai_fields")},
            "labels": {k:service.QUESTIONS[k]["label"] for k in row.answers if k in service.QUESTIONS}}


async def saved_output(db, row):
    await db.commit()
    await db.refresh(row)  # SQL-computed updated_at must not lazy-load in async serialization.
    return output(row)


@router.get("/brief/questions")
async def questions():
    return {"sections": service.BRIEF, "privacy_url": settings.brief_privacy_url, "booking_url": settings.brief_booking_url,
            "accepting": bool(settings.agency_workspace_id and settings.brief_privacy_url)}


class AnswersIn(BaseModel):
    answers: dict[str, str | list[str]]


class PublicIn(BaseModel):
    answers: dict
    consent: bool
    website: str = Field(default="", max_length=500)
    utm: dict[str, str] = Field(default_factory=dict)
    referrer: str = Field(default="", max_length=2000)


@router.get("/brief/express")
async def express_schema():
    return {"questions":service.EXPRESS, "privacy_url":settings.brief_privacy_url,
            "accepting":bool(settings.agency_workspace_id and settings.brief_privacy_url)}


async def admin_alert(assessment_id):
    from html import escape
    from app.services import monitor
    from app.models.crm import CrmInbound
    try:
        async with SessionLocal() as db:
            row=await db.get(ExpressAssessment,assessment_id)
            lead=await db.get(CrmInbound,row.lead_id)
            a=row.answers
            price=lambda v: f"{v:,.0f} ₽".replace(","," ") if v is not None else "не указана"
            url=settings.frontend_origin.rstrip("/")+f"/crm?project_id={lead.project_id}&tab=inbound&inbound_id={lead.id}"
            message=(f"📝 Экспресс-оценка #{row.id}\nИмя: {escape(a.get('name',''))}\n"
                     f"Ниша: {escape(a.get('what',''))}\nБюджет: {escape(a.get('budget',''))}\n"
                     f"Предел цены заявки: {price(row.limit_cpl)}\nСейчас: {price(row.current_cpl)}\n"
                     f'<a href="{escape(url,quote=True)}">Открыть заявку #{lead.id}</a>')
            await monitor.send(db,message)
    except Exception:
        logging.getLogger("uvicorn.error.brief").exception("Не удалось отправить уведомление об экспресс-оценке #%s",assessment_id)


@router.post("/brief/submit", status_code=201)
async def public_submit(payload: PublicIn, request: Request, tasks: BackgroundTasks, response: Response, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    response.headers["Cache-Control"]="no-store"
    if payload.website:
        raise HTTPException(422,"Не удалось принять форму")
    if not payload.consent:
        raise HTTPException(422,"Нужно согласие на обработку персональных данных")
    # Per-IP limit applies always, even when the global demo cap is exhausted and no demo is created.
    await rate_limit(request,"brief_submit",5,3600)
    answers=service.express_answers(payload.answers)
    contact=answers["contact"].strip()
    is_telegram=bool(re.fullmatch(r"@[a-zA-Z0-9_]{5,32}",contact))
    if not is_telegram and not (10<=len(re.sub(r"\D","",contact))<=15 and re.fullmatch(r"[+\d ()-]+",contact)):
        raise HTTPException(422,"Укажите телефон или Telegram в формате @username")
    agency=await db.get(ClientWorkspace,settings.agency_workspace_id)
    if not agency or agency.status!="active" or not settings.brief_privacy_url:
        raise HTTPException(503,"Приём экспресс-оценок пока не настроен")
    now=datetime.now(timezone.utc)
    ip_hash=hashlib.sha256((settings.encryption_secret+":brief:"+client_address(request)).encode()).hexdigest()
    if db.bind.dialect.name=="postgresql":
        # One shared transaction lock prevents different IPs racing past the cap.
        await db.execute(text("SELECT pg_advisory_xact_lock(:k)"),{"k":731904260053})
        await db.execute(text("SELECT pg_advisory_xact_lock(:k)"),{"k":int(ip_hash[:15],16)})
    today=now.replace(hour=0,minute=0,second=0,microsecond=0)
    total=await db.scalar(select(func.count(ExpressAssessment.id)).where(ExpressAssessment.demo_workspace_id.is_not(None),ExpressAssessment.created_at>=today))
    total+=await db.scalar(select(func.count(ClientBrief.id)).where(ClientBrief.ip_hash.is_not(None),ClientBrief.demo_workspace_id.is_not(None),ClientBrief.created_at>=today))
    create_demo=total<max(0,settings.brief_demo_daily_cap)
    count=await db.scalar(select(func.count(ExpressAssessment.id)).where(ExpressAssessment.ip_hash==ip_hash,ExpressAssessment.demo_workspace_id.is_not(None),ExpressAssessment.created_at>=today))
    legacy=await db.scalar(select(func.count(ClientBrief.id)).where(ClientBrief.ip_hash==ip_hash,ClientBrief.created_at>=today))
    if create_demo and count+legacy>=3: raise HTTPException(429,"Можно создать не больше трёх демо в сутки")
    project=await default_project(db,agency.id)
    source=await db.scalar(select(LeadInboundSource).where(LeadInboundSource.project_id==project.id,LeadInboundSource.name=="Экспресс-оценка"))
    if not source:
        source=LeadInboundSource(workspace_id=agency.id,project_id=project.id,name="Экспресс-оценка",token_hash=secrets.token_hex(32),
            token_prefix="express",active=False,auto_assign=False,auto_accept=False)
        db.add(source);await db.flush()
    calculation=service.express_calculation(answers)
    utm={k:v[:255] for k,v in payload.utm.items() if k in {"utm_source","utm_medium","utm_campaign","utm_content","utm_term"}}
    labels={f[0]:f[1] for q in service.EXPRESS for f in q.get("f",[])}
    labels.update({q["k"]:q["t"] for q in service.EXPRESS if q["type"]!="fields"})
    labels.update({"dm":"Кто принимает решение","pain_other":"Что не устраивает — своими словами"})
    inbound=await create_inbound(db,source,InboundLead(full_name=answers["name"],contact=contact,
        contact_method="Telegram" if is_telegram else "Телефон",phone=None if is_telegram else contact,
        source="Экспресс-оценка",contact_consent=True,notes="Экспресс-оценка: ответы и расчёт в карточке.",
        brief_answers=answers,brief_labels=labels,express_calculation=calculation,referer=payload.referrer,**utm),
        allow_raw_contact=True,commit=False)
    data=await demo.create_demo(db,commit=False) if create_demo else None
    workspace=await db.get(ClientWorkspace,data["workspace_id"]) if data else None
    row=ExpressAssessment(lead_id=inbound["inbound_id"],answers=answers,limit_cpl=calculation["limit_cpl"],
        current_cpl=calculation["current_cpl"],utm={**utm,"referrer":payload.referrer},
        ip_hash=ip_hash,demo_workspace_id=workspace.id if workspace else None,
        expires_at=now+timedelta(days=max(1,settings.brief_demo_ttl_days)) if workspace else None)
    db.add(row);await db.flush()
    from app.models.crm import CrmInbound
    lead=await db.get(CrmInbound,row.lead_id)
    lead.raw_payload={**lead.raw_payload,"assessment_id":row.id,"consent_at":now.isoformat(),"privacy_url":settings.brief_privacy_url}
    if data:
        workspace.name=f"Демо · {str(answers.get('company') or answers['what'])[:130]} · {workspace.id}"
        demo_project=await default_project(db,workspace.id)
        demo_project.name=answers["what"][:180]
        origin=settings.frontend_origin.rstrip("/")
        lead.raw_payload={**lead.raw_payload,"brief_demo_access":{
            "username":data["username"],"login_url":origin+"/portal/login",
            "reset_url":origin+"/portal/reset?"+urlencode({"username":data["username"]})}}
        state=dict(demo_project.portal_state or {})
        personal={"company.name":str(answers.get("company") or answers["what"]), "company.what":answers["what"],
                  "company.geo":answers.get("city",""), "econ.check":str(answers["check"]), "goals.g3":answers["goal"],
                  "mkt.leads":str(answers.get("leads","")), "mkt.cpl":str(answers.get("cpl",""))}
        knowledge=service.fallback_knowledge(personal)
        state["ai"]={**dict(state.get("ai") or {}),**{k:knowledge[k] for k in ("knowledge","tone","goal")}}
        state["baseline"]=service.baseline_from_answers(answers,express=True)
        state["brief_demo"]={"expires_at":row.expires_at.isoformat(),"assessment_id":row.id}
        demo_project.portal_state=state
        # Demo conversations must not retain the template's unrelated kitchen niche.
        from app.models.messaging import Conversation,Message
        for conv in (await db.scalars(select(Conversation).where(Conversation.project_id==demo_project.id))).all():
            lines=["Здравствуйте! Интересует ваша услуга. Расскажите об условиях.","Подскажите, какая задача для вас сейчас самая важная?","Хочу понять цены и следующий шаг."]
            for msg,line in zip((await db.scalars(select(Message).where(Message.conversation_id==conv.id).order_by(Message.id))).all(),lines):msg.text=line
            conv.last_message_preview=lines[-1]
    await db.commit()
    from app.services.notifications import flush_telegram
    flush_telegram(db)
    tasks.add_task(admin_alert,row.id)
    return {"assessment_id":row.id,"lead_id":row.lead_id,"calculation":calculation,
            "demo":{"username":data["username"],"password":data["password"],"login_url":"/portal/login"} if data else None,
            "expires_at":row.expires_at,"booking_url":settings.brief_booking_url}


async def brief_actor(request:Request, db:AsyncSession=Depends(get_db)):
    from app.core.access import session_user,portal_session_user
    check_origin(request)
    admin_user=await session_user(request,db)
    if admin_user and admin_user.role=="admin": return admin_user
    user=await portal_session_user(request,db)
    if not user: raise HTTPException(401,"Войдите в портал")
    return user


async def managed_project(db, user, project_id):
    from app.models.access import AdminUser
    if isinstance(user, AdminUser):
        project = await db.get(Project, project_id)
        workspace = await db.get(ClientWorkspace, project.workspace_id) if project else None
        if not project or not workspace or workspace.status == "deleted": raise HTTPException(404, "Проект не найден")
        return project
    project = await project_for(db, user, project_id)
    if user.role != "client_owner":
        raise HTTPException(403, "Бриф меняет владелец клиента или администратор агентства")
    if project.status == "demo":
        raise HTTPException(403, "Полный бриф доступен после подключения рабочего кабинета. Обратитесь к менеджеру")
    return project


async def owned(db, user, project_id, brief_id):
    project = await managed_project(db, user, project_id)
    row = await db.get(ClientBrief, brief_id)
    if not row or row.project_id != project.id or row.workspace_id != project.workspace_id:
        raise HTTPException(404, "Бриф не найден")
    return row, project


@router.get("/brief/projects/{project_id}")
async def drafts(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(brief_actor)):
    await managed_project(db, user, project_id)
    rows = (await db.scalars(select(ClientBrief).where(ClientBrief.project_id == project_id).order_by(ClientBrief.id.desc()))).all()
    return [output(r) for r in rows]


@router.post("/brief/projects/{project_id}")
async def save(project_id: int, payload: AnswersIn, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(brief_actor)):
    project = await managed_project(db, user, project_id)
    row = ClientBrief(workspace_id=project.workspace_id, project_id=project.id, answers=service.clean_answers(payload.answers))
    db.add(row); await db.commit(); await db.refresh(row)
    return output(row)


@router.put("/brief/projects/{project_id}/{brief_id:int}")
async def update(project_id: int, brief_id: int, payload: AnswersIn, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(brief_actor)):
    row, _ = await owned(db, user, project_id, brief_id)
    answers = service.clean_answers(payload.answers)
    if answers != row.answers:
        row.answers = answers; row.preview = None; row.status = "draft"
    return await saved_output(db, row)


@router.post("/brief/projects/{project_id}/{brief_id}/preview")
async def preview(project_id: int, brief_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(brief_actor)):
    row, project = await owned(db, user, project_id, brief_id)
    try:
        await service.apply_to_ai(db, row, project)
    except ai.AIError as exc:
        raise HTTPException(422, str(exc)) from None
    return await saved_output(db, row)


@router.post("/brief/projects/{project_id}/{brief_id}/apply")
async def apply(project_id: int, brief_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(brief_actor)):
    row, project = await owned(db, user, project_id, brief_id)
    if row.status != "applied": raise HTTPException(409, "Сначала подтвердите бриф")
    await service.apply_to_ai(db, row, project, apply=True)
    return await saved_output(db, row)


@router.post("/brief/projects/{project_id}/upload")
async def upload(project_id: int, file: UploadFile = File(...), db: AsyncSession = Depends(get_db), user: PortalUser = Depends(brief_actor)):
    project = await managed_project(db, user, project_id)
    import asyncio
    content = await file.read(5 * 1024 * 1024 + 1)
    extracted = await asyncio.to_thread(service.extract_text, content, file.filename or "")
    try:
        answers = await service.import_answers(extracted, project.workspace_id)
    except ai.AIError as exc:
        raise HTTPException(422, str(exc)) from None
    row = ClientBrief(workspace_id=project.workspace_id, project_id=project.id, answers=answers, source="upload", transcript=extracted, ai_fields=list(answers), status="review")
    db.add(row); await db.commit(); await db.refresh(row)
    return output(row)


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=100000)


@router.post("/brief/projects/{project_id}/text")
async def import_text(project_id: int, payload: TextIn, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(brief_actor)):
    project = await managed_project(db, user, project_id)
    try:
        answers = await service.import_answers(payload.text, project.workspace_id)
    except ai.AIError as exc:
        raise HTTPException(422, str(exc)) from None
    row = ClientBrief(workspace_id=project.workspace_id, project_id=project.id, answers=answers, source="call", transcript=payload.text, ai_fields=list(answers), status="review")
    db.add(row); await db.commit(); await db.refresh(row)
    return output(row)


@router.get("/brief/projects/{project_id}/demo-status")
async def demo_status(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(brief_actor)):
    from app.models.access import AdminUser
    project = await managed_project(db,user,project_id) if isinstance(user,AdminUser) else await project_for(db,user,project_id)
    return {"demo": (project.portal_state or {}).get("brief_demo"), "booking_url": settings.brief_booking_url}


@admin.get("")
async def listing(db: AsyncSession = Depends(get_db)):
    from app.models.crm import CrmInbound
    rows = (await db.execute(select(ClientBrief, CrmInbound.project_id).outerjoin(CrmInbound, ClientBrief.lead_id == CrmInbound.id)
        .order_by(ClientBrief.id.desc()).limit(200))).all()
    return [{**output(r), "lead_project_id": pid} for r,pid in rows]


@admin.get("/targets")
async def targets(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Project, ClientWorkspace.name).join(ClientWorkspace).where(
        Project.status == "active", ClientWorkspace.status == "active"))).all()
    return [{"id": p.id, "workspace_id":p.workspace_id, "name": f"{name} · {p.name}"} for p,name in rows]


@admin.get("/{brief_id}/export")
async def export(brief_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(ClientBrief, brief_id)
    if not row:
        raise HTTPException(404, "Бриф не найден")
    return Response(service.markdown(row.answers), media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="brief-{row.id}.md"'})


class TransferIn(BaseModel):
    project_id: int


@admin.post("/{brief_id}/transfer-preview")
async def transfer_preview(brief_id: int, payload: TransferIn, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    row = await db.get(ClientBrief, brief_id); project = await db.get(Project, payload.project_id)
    if not row or not project or project.status != "active":
        raise HTTPException(404, "Бриф или рабочий проект не найден")
    workspace = await db.get(ClientWorkspace, project.workspace_id)
    if not workspace or workspace.status != "active":
        raise HTTPException(404, "Рабочая компания не найдена")
    try:
        data = await service.apply_to_ai(db, row, project)
    except ai.AIError as exc:
        raise HTTPException(422, str(exc)) from None
    row.preview = {**row.preview, "target_project_id": project.id}
    return await saved_output(db, row)


@admin.post("/{brief_id}/transfer")
async def transfer(brief_id: int, payload: TransferIn, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    row = await db.get(ClientBrief, brief_id); project = await db.get(Project, payload.project_id)
    if not row or not project or project.status != "active":
        raise HTTPException(404, "Бриф или рабочий проект не найден")
    workspace = await db.get(ClientWorkspace, project.workspace_id)
    if not workspace or workspace.status != "active":
        raise HTTPException(404, "Рабочая компания не найдена")
    if (row.preview or {}).get("target_project_id") != project.id:
        raise HTTPException(409, "Согласуйте предпросмотр для выбранного проекта")
    await service.apply_to_ai(db, row, project, apply=True)
    row.project_id = project.id; row.workspace_id = project.workspace_id
    return await saved_output(db, row)




@router.post("/brief/projects/{project_id}/{brief_id}/confirm")
async def confirm(project_id:int,brief_id:int,db:AsyncSession=Depends(get_db),user=Depends(brief_actor)):
    row,project=await owned(db,user,project_id,brief_id)
    if not any(str(v).strip() for v in row.answers.values()): raise HTTPException(422,"Заполните хотя бы один ответ")
    row.status="applied"
    state=dict(project.portal_state or {})
    if not (state.get("baseline") or {}).get("fixed"):
        state["baseline"]={**service.baseline_from_answers(row.answers),**{k:v for k,v in (state.get("baseline") or {}).items() if v is not None}}
    project.portal_state=state
    return await saved_output(db,row)


@router.post("/brief/projects/{project_id}/recording")
async def recording(project_id:int,file:UploadFile=File(...),db:AsyncSession=Depends(get_db),user=Depends(brief_actor)):
    project=await managed_project(db,user,project_id)
    from app.services.call_ai import STTError
    try:
        transcript=await service.transcribe_meeting(await file.read(200*1024*1024+1),file.filename or "",project.workspace_id)
        answers=await service.import_answers(transcript,project.workspace_id)
    except (STTError,ai.AIError) as exc:
        raise HTTPException(422,str(exc)) from None
    row=ClientBrief(workspace_id=project.workspace_id,project_id=project.id,answers=answers,source="call",
                    transcript=transcript,ai_fields=list(answers),status="review")
    db.add(row)
    return await saved_output(db,row)


class BaselineIn(BaseModel):
    values:dict
    fix:bool=False


@router.get("/brief/projects/{project_id}/baseline")
async def baseline(project_id:int,db:AsyncSession=Depends(get_db),user=Depends(brief_actor)):
    # Any project reader can see result comparisons; write permission is checked separately.
    from app.models.access import AdminUser
    project=await managed_project(db,user,project_id) if isinstance(user,AdminUser) else await project_for(db,user,project_id)
    state=project.portal_state or {}
    return {"values":state.get("baseline") or service.baseline_from_answers({}),"history":state.get("baseline_history",[]),
            "can_fix":isinstance(user,AdminUser),
            "can_edit":isinstance(user,AdminUser) or (user.role=="client_owner" and not (state.get("baseline") or {}).get("fixed"))}


@router.put("/brief/projects/{project_id}/baseline")
async def save_baseline(project_id:int,payload:BaselineIn,db:AsyncSession=Depends(get_db),user=Depends(brief_actor)):
    from app.models.access import AdminUser
    project=await managed_project(db,user,project_id)
    if payload.fix and not isinstance(user,AdminUser):
        raise HTTPException(403,"Фиксировать точку А может только администратор агентства")
    await db.refresh(project,with_for_update=True)
    state=dict(project.portal_state or {});old=dict(state.get("baseline") or {})
    if old.get("fixed") and not isinstance(user,AdminUser): raise HTTPException(403,"Зафиксированную точку А меняет только администратор")
    values=payload.values
    allowed=set(service.BASELINE_FIELDS)|{"start_date","goal_value","goal_metric","goal_text"}
    if set(values)-allowed: raise HTTPException(422,"Неизвестные поля точки А")
    clean={**old}
    for k,v in values.items():
        if k in service.BASELINE_FIELDS or k=="goal_value":
            parsed=None if v in ("",None) else service.number(v)
            if v not in ("",None) and parsed is None: raise HTTPException(422,"Укажите неотрицательные числа")
            if k=="conversion" and parsed is not None and parsed>100: raise HTTPException(422,"Конверсия не может превышать 100%")
            clean[k]=parsed
        elif k=="goal_metric":
            if v not in service.GOAL_METRICS: raise HTTPException(422,"Неизвестная метрика цели")
            clean[k]=v
        elif k=="start_date":
            try: datetime.strptime(str(v),"%Y-%m-%d")
            except ValueError: raise HTTPException(422,"Укажите дату старта")
            clean[k]=v
        else: clean[k]=str(v)[:2000]
    if payload.fix and (not clean.get("start_date") or clean.get("goal_value") is None or not clean.get("goal_metric")):
        raise HTTPException(422,"Для фиксации укажите дату и числовую цель с метрикой")
    clean["fixed"]=bool(old.get("fixed") or payload.fix)
    history=list(state.get("baseline_history") or [])
    history.append({"at":datetime.now(timezone.utc).isoformat(),"actor_id":user.id,"admin":isinstance(user,AdminUser),"before":old,"after":clean})
    state.update(baseline=clean,baseline_history=history)
    project.portal_state=state;await db.commit()
    return {"values":clean,"history":history,"can_fix":isinstance(user,AdminUser),"can_edit":isinstance(user,AdminUser) or not clean["fixed"]}


@admin.get("/assessments/list")
async def assessments(db:AsyncSession=Depends(get_db)):
    from app.models.crm import CrmInbound
    rows=(await db.execute(select(ExpressAssessment,CrmInbound).join(CrmInbound,ExpressAssessment.lead_id==CrmInbound.id)
            .order_by(ExpressAssessment.id.desc()).limit(200))).all()
    return [{"id":r.id,"lead_id":r.lead_id,"project_id":lead.project_id,"answers":r.answers,"limit_cpl":r.limit_cpl,
             "current_cpl":r.current_cpl,"labels":service.EXPRESS_LABELS,"created_at":r.created_at,"status":lead.status,"demo_workspace_id":r.demo_workspace_id} for r,lead in rows]


@admin.post("/assessments/{assessment_id}/assign")
async def assign_assessment(assessment_id:int,payload:TransferIn,db:AsyncSession=Depends(get_db),user=Depends(require_admin)):
    assessment=await db.get(ExpressAssessment,assessment_id)
    project=await managed_project(db,user,payload.project_id)
    if not assessment or project.status!="active": raise HTTPException(404,"Оценка или рабочий проект не найден")
    await db.refresh(assessment,with_for_update=True)
    a=assessment.answers
    values={"company.name":str(a.get("company") or a.get("what","")), "company.what":a.get("what",""),
        "company.geo":a.get("city",""),"econ.check":str(a.get("check","")),"goals.g3":a.get("goal",""),
        "mkt.leads":"" if a.get("now_idk") else str(a.get("leads","")),
        "mkt.cpl":"" if a.get("now_idk") else str(a.get("cpl",""))}
    calculation=service.express_calculation(a)
    if calculation["conversion"] is not None: values["sales.conv"]=str(calculation["conversion"]*100)
    row=await db.scalar(select(ClientBrief).where(ClientBrief.project_id==project.id,ClientBrief.lead_id==assessment.lead_id))
    if not row:
        row=ClientBrief(workspace_id=project.workspace_id,project_id=project.id,lead_id=assessment.lead_id,answers=values,source="manual")
        db.add(row)
    state=dict(project.portal_state or {})
    if not (state.get("baseline") or {}).get("fixed"):
        state["baseline"]={**service.baseline_from_answers(a,express=True),**{k:v for k,v in (state.get("baseline") or {}).items() if v is not None}}
    state["express_assessment_id"]=assessment.id
    project.portal_state=state
    return await saved_output(db,row)


router.include_router(admin)
