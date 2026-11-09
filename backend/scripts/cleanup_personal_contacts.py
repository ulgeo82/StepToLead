"""Явная очистка личных контактов только агентского workspace.

По умолчанию dry-run; --apply удаляет в одной транзакции. Повторный запуск безопасен.
Отката нет: удалённые персональные данные не восстанавливаются. Не вызывается при старте.
text_excerpt сохраняется: его использует fit_ai.build_prompt для ИИ-оценки компании.
"""
from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import MetaData, Table, and_, delete, exists, func, inspect, or_, select, text


def cleanup(connection, workspace_id: int, *, apply: bool = False) -> dict:
    if not workspace_id:
        raise ValueError("Не задан workspace агентства — очистка запрещена")
    metadata = MetaData()
    inspector = inspect(connection)
    names = inspector.get_table_names()
    contacts = Table("lg_contacts", metadata, autoload_with=connection)
    companies = Table("lg_companies", metadata, autoload_with=connection)
    references = {}
    # Проверяем реальные FK установленной БД, а не только две известные модели.
    for name in names:
        for fk in inspector.get_foreign_keys(name):
            if fk["referred_table"] != "lg_contacts":
                continue
            table = Table(name, metadata, autoload_with=connection, extend_existing=True)
            condition = and_(*(table.c[left] == contacts.c[right]
                               for left, right in zip(fk["constrained_columns"], fk["referred_columns"])))
            references.setdefault(name, []).append(exists(select(1).select_from(table).where(condition)))
    if "lg_touches" in names:
        touches = Table("lg_touches", metadata, autoload_with=connection, extend_existing=True)
        references.setdefault("lg_touches", []).append(exists(select(1).where(
            touches.c.company_id == contacts.c.company_id,
            func.lower(touches.c.address) == func.lower(contacts.c.value_norm))))
    # CRM использует crm_contacts, не lg_contacts. Бережно оставляем контакты переданных в CRM компаний.
    references["crm"] = [or_(companies.c.crm_deal_id.is_not(None), companies.c.crm_contact_id.is_not(None))]
    if "lg_dnc" in names:
        dnc = Table("lg_dnc", metadata, autoload_with=connection, extend_existing=True)
        references["lg_dnc"] = [exists(select(1).where(
            dnc.c.workspace_id == workspace_id,
            or_(and_(dnc.c.kind == contacts.c.kind, func.lower(dnc.c.value_norm) == func.lower(contacts.c.value_norm)),
                and_(dnc.c.kind == "company", dnc.c.value_norm == func.cast(companies.c.id, contacts.c.value_norm.type)),
                and_(dnc.c.kind == "domain", dnc.c.value_norm == companies.c.domain))))]
    if apply and connection.dialect.name == "postgresql":
        preparer = connection.dialect.identifier_preparer
        locked = sorted({"lg_contacts", "lg_companies", "lg_dnc", *references} & set(names))
        connection.execute(text("LOCK TABLE " + ", ".join(preparer.quote(name) for name in locked)
                                + " IN SHARE ROW EXCLUSIVE MODE"))
    scope = and_(contacts.c.is_personal.is_(True), companies.c.workspace_id == workspace_id)
    joined = contacts.join(companies, companies.c.id == contacts.c.company_id)
    predicates = {name: or_(*conditions) for name, conditions in references.items()}

    def count(condition):
        return connection.scalar(select(func.count()).select_from(joined).where(scope, condition))

    protected = or_(*predicates.values())
    removable = select(contacts.c.id).select_from(joined).where(scope, ~protected)
    report = {"mode": "apply" if apply else "dry-run", "workspace_id": workspace_id,
              "would_delete": count(~protected), "retained": count(protected),
              "references": {name: count(condition) for name, condition in predicates.items()},
              "text_excerpt_cleaned": 0, "deleted": 0,
              "text_excerpt": "Сохранён: используется fit_ai.build_prompt для ИИ-оценки",
              "warning": "Отката нет: удалённые персональные данные не восстанавливаются"}
    if apply:
        report["deleted"] = connection.execute(delete(contacts).where(contacts.c.id.in_(removable))).rowcount
    return report


async def main(apply=False):
    from app.core.config import settings
    from app.db import engine
    try:
        async with engine.begin() as connection:
            report = await connection.run_sync(lambda conn: cleanup(conn, settings.agency_workspace_id, apply=apply))
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Применить необратимую очистку")
    asyncio.run(main(parser.parse_args().apply))
