from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketing import ClientWorkspace, Project


async def default_project(db: AsyncSession, workspace_id: int) -> Project:
    project = await db.scalar(select(Project).where(Project.workspace_id == workspace_id)
                              .order_by(Project.is_default.desc(), Project.id).limit(1))
    if project:
        return project
    workspace = await db.get(ClientWorkspace, workspace_id)
    if workspace is None:
        raise ValueError("Компания не найдена")
    project = Project(workspace_id=workspace_id, name=workspace.name, is_default=True)
    db.add(project)
    await db.flush()
    return project
