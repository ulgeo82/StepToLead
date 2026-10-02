"""Read-only production diagnosis: python - < deploy/diagnose_tilda.py."""
import asyncio
import json
import sys

from sqlalchemy import inspect, text

from app.db import SessionLocal, engine


def payload(row):
    value = row.raw_payload
    return json.loads(value) if isinstance(value, str) else value or {}


async def main():
    tranid = sys.argv[1] if len(sys.argv) > 1 else "8713176482"
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
    missing = set(("tilda_connections", "tilda_receipts", "crm_inbound")) - set(tables)
    if missing:
        print(json.dumps({"missing_tables": sorted(missing), "adapter_schema_ready": False}))
        await engine.dispose()
        return
    async with SessionLocal() as db:
        connections = (await db.execute(text("SELECT id, organization_id, project_id, website_id, inbound_source_id, is_active, last_received_at FROM tilda_connections"))).mappings().all()
        print(json.dumps({"integrations": [{"integration_id": row.id,
            "organization_id": row.organization_id, "project_id": row.project_id,
            "website_id": row.website_id, "inbound_source_id": row.inbound_source_id,
            "active": row.is_active, "last_received_at": str(row.last_received_at)}
            for row in connections]}, ensure_ascii=False))
        for row in connections:
            source = (await db.execute(text("SELECT workspace_id, project_id, active FROM lead_inbound_sources WHERE id=:id"),
                                      {"id": row.inbound_source_id})).mappings().first()
            print(json.dumps({"integration_id": row.id, "source_scope": {
                "organization_id": source.workspace_id, "project_id": source.project_id,
                "active": source.active} if source else None}))
        rows = (await db.execute(text("SELECT id, workspace_id, project_id, status, external_id, received_at, raw_payload, website_session_id FROM crm_inbound WHERE external_id LIKE :tranid"),
                                 {"tranid": f"%{tranid}%"})).mappings().all()
        print(json.dumps({"tranid_search": tranid, "crm_requests": [{"inbound_id": row.id,
            "organization_id": row.workspace_id, "project_id": row.project_id, "status": row.status,
            "external_id": row.external_id, "received_at": str(row.received_at),
            "contact": payload(row).get("contact"), "contact_method": payload(row).get("contact_method"),
            "website_session_key": payload(row).get("website_session_key"),
            "website_session_id": row.website_session_id} for row in rows]}, ensure_ascii=False))
        receipts = (await db.execute(text("SELECT tilda_connection_id, tranid, inbound_id FROM tilda_receipts WHERE tranid LIKE :tranid"),
                                     {"tranid": f"%{tranid}%"})).mappings().all()
        print(json.dumps({"receipts": [{"integration_id": row.tilda_connection_id,
            "tranid": row.tranid, "inbound_id": row.inbound_id} for row in receipts]}))
    await engine.dispose()


asyncio.run(main())
