"""Additive upgrade for installations created before project-scoped reporting.

The application previously used create_all without a migration tool. Keep old
rows intact and put each existing workspace's data into one default project.
"""
from sqlalchemy import inspect, text
from app.models.crm import CrmContact, CrmDeal


def upgrade_existing_schema(connection):
    from app.models.ai import AiUsage
    AiUsage.__table__.create(connection, checkfirst=True)
    from app.models.brief import ClientBrief, ExpressAssessment
    ClientBrief.__table__.create(connection, checkfirst=True)
    ExpressAssessment.__table__.create(connection, checkfirst=True)
    inspector = inspect(connection)
    columns = {
        "client_briefs": {"transcript": "TEXT", "ai_fields": "JSON"},
        "client_workspaces": {"plan": "VARCHAR(16)", "legal_name": "VARCHAR(240)", "contact_email": "VARCHAR(254)",
                              "contact_phone": "VARCHAR(64)", "website": "VARCHAR(500)",
                              "timezone": "VARCHAR(80) NOT NULL DEFAULT 'Europe/Moscow'",
                              "currency": "VARCHAR(12) NOT NULL DEFAULT 'RUB'", "logo_data": "TEXT"},
        "projects": {"website": "VARCHAR(500)", "description": "TEXT",
                     "status": "VARCHAR(24) NOT NULL DEFAULT 'active'", "timezone": "VARCHAR(80)",
                     "meeting_enabled": "BOOLEAN NOT NULL DEFAULT false", "portal_state": "JSON"},
        "ad_connections": {"project_id": "INTEGER REFERENCES projects(id)",
                           "vk_client_id_encrypted": "TEXT", "vk_client_secret_encrypted": "TEXT",
                           "vk_refresh_token_encrypted": "TEXT", "vk_access_expires_at": "TIMESTAMP WITH TIME ZONE",
                           "config": "JSON"},
        "ad_hypotheses": {"project_id": "INTEGER REFERENCES projects(id)"},
        "portal_users": {"reset_code_hash": "VARCHAR(64)", "reset_expires_at": "TIMESTAMP WITH TIME ZONE", "reset_attempts": "INTEGER NOT NULL DEFAULT 0", "manage_sources": "BOOLEAN NOT NULL DEFAULT false",
                         "manage_integrations": "BOOLEAN NOT NULL DEFAULT false",
                         "phone": "VARCHAR(64)", "permissions": "JSON", "last_activity_at": "TIMESTAMP WITH TIME ZONE",
                         "telegram_chat_id": "VARCHAR(32)", "telegram_username": "VARCHAR(64)",
                         "telegram_link_code_hash": "VARCHAR(64)",
                         "telegram_link_expires_at": "TIMESTAMP WITH TIME ZONE", "telegram_state": "JSON"},
        "project_notification_rules": {"telegram": "BOOLEAN NOT NULL DEFAULT false",
                                       "recipient_user_ids": "JSON",
                                       "notify_assignee": "BOOLEAN NOT NULL DEFAULT true"},
        "portal_project_access": {"manage_integrations": "BOOLEAN NOT NULL DEFAULT false"},
        "client_leads": {
            "project_id": "INTEGER REFERENCES projects(id)",
            "source_id": "INTEGER REFERENCES project_sources(id)",
            "qualified_at": "TIMESTAMP",
            "meeting_at": "TIMESTAMP WITH TIME ZONE",
            "telegram": "VARCHAR(120)",
            "lost_reason": "TEXT",
            "quality": "VARCHAR(16)",
            "quality_reason": "VARCHAR(120)",
        },
        "client_sales": {"comment": "TEXT", "deal_id": "INTEGER REFERENCES crm_deals(id)"},
        "lead_inbound_sources": {"project_id": "INTEGER REFERENCES projects(id)",
                                 "auto_accept": "BOOLEAN NOT NULL DEFAULT false"},
        "project_sources": {"category": "VARCHAR(80)", "status": "VARCHAR(24) NOT NULL DEFAULT 'active'",
                            "data_mode": "VARCHAR(20)", "created_by_id": "INTEGER REFERENCES portal_users(id)",
                            "metadata": "JSON"},
        "crm_contacts": {"phone_normalized": "VARCHAR(32)", "email_normalized": "VARCHAR(254)",
                         "position": "VARCHAR(120)", "notes": "TEXT", "tags": "JSON"},
        "crm_deals": {"tags": "JSON", "stage_entered_at": "TIMESTAMP WITH TIME ZONE",
                      "last_activity_at": "TIMESTAMP WITH TIME ZONE",
                      "first_response_at": "TIMESTAMP WITH TIME ZONE", "automation_state": "JSON"},
        "crm_inbound": {"website_session_id": "INTEGER REFERENCES website_sessions(id)",
                        "escalated_at": "TIMESTAMP WITH TIME ZONE"},
        "website_sessions": {"engaged": "BOOLEAN NOT NULL DEFAULT false",
                             "cta_clicked": "BOOLEAN NOT NULL DEFAULT false",
                             "form_started": "BOOLEAN NOT NULL DEFAULT false",
                             "form_succeeded": "BOOLEAN NOT NULL DEFAULT false",
                             "click_type": "VARCHAR(12)", "ym_client_id": "VARCHAR(32)"},
        "calls": {"ai_status": "VARCHAR(12)"},
        "admin_users": {"telegram_chat_id": "VARCHAR(32)", "telegram_username": "VARCHAR(64)",
                        "telegram_link_code_hash": "VARCHAR(64)", "telegram_link_expires_at": "TIMESTAMP WITH TIME ZONE"},
        "website_sites": {"widget": "JSON"},
        "lg_touches": {"mailbox_id": "INTEGER REFERENCES lg_mailboxes(id)", "address": "VARCHAR(254)",
                       "label": "VARCHAR(16)", "label_source": "VARCHAR(10)", "summary": "VARCHAR(300)",
                       "handled_at": "TIMESTAMP WITH TIME ZONE"},
    }
    for table, additions in columns.items():
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, declaration in additions.items():
            if name not in existing:
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    if "crm_deals" in inspector.get_table_names():
        connection.execute(text("UPDATE crm_deals SET stage_entered_at = COALESCE(updated_at, created_at) "
                                "WHERE stage_entered_at IS NULL"))
        connection.execute(text("UPDATE crm_deals SET last_activity_at = COALESCE(updated_at, created_at) "
                                "WHERE last_activity_at IS NULL"))
    if "crm_inbound" in inspector.get_table_names():
        indexes = {index["name"] for index in inspector.get_indexes("crm_inbound")}
        if "ix_crm_inbound_website_session_id" not in indexes:
            connection.exec_driver_sql("CREATE INDEX ix_crm_inbound_website_session_id ON crm_inbound (website_session_id)")
    if "website_events" in inspector.get_table_names():
        event_indexes = {index["name"] for index in inspector.get_indexes("website_events")}
        if "ix_website_event_project_element_session" not in event_indexes:
            connection.exec_driver_sql("CREATE INDEX ix_website_event_project_element_session ON website_events (project_id, element_name, session_id)")

    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql("ALTER TABLE client_sales ALTER COLUMN amount DROP NOT NULL")
        connection.exec_driver_sql("ALTER TABLE client_leads ALTER COLUMN value DROP NOT NULL")
    connection.execute(text(
        "UPDATE client_sales SET amount=NULL WHERE amount=0 AND confirmed_by_id IS NULL "
        "AND lead_id IN (SELECT id FROM client_leads WHERE value IS NULL)"
    ))

    workspace_rows = connection.execute(text("SELECT id, name FROM client_workspaces")).all()
    for workspace_id, name in workspace_rows:
        project_id = connection.execute(text(
            "SELECT id FROM projects WHERE workspace_id=:workspace_id ORDER BY is_default DESC, id LIMIT 1"
        ), {"workspace_id": workspace_id}).scalar()
        if project_id is None:
            project_id = connection.execute(text(
                "INSERT INTO projects (workspace_id, name, is_default, status, meeting_enabled, portal_state) "
                "VALUES (:workspace_id, :name, true, 'active', false, '{}') RETURNING id"
            ), {"workspace_id": workspace_id, "name": name}).scalar_one()
        for table in ("ad_connections", "ad_hypotheses", "client_leads", "lead_inbound_sources"):
            connection.execute(text(
                f"UPDATE {table} SET project_id=:project_id "
                "WHERE workspace_id=:workspace_id AND project_id IS NULL"
            ), {"project_id": project_id, "workspace_id": workspace_id})
        connection.execute(text(
            "INSERT INTO portal_project_access (user_id, project_id, manage_sources, manage_integrations) "
            "SELECT u.id, :project_id, u.manage_sources, u.manage_integrations FROM portal_users u "
            "WHERE u.workspace_id=:workspace_id AND NOT EXISTS "
            "(SELECT 1 FROM portal_project_access a WHERE a.user_id=u.id AND a.project_id=:project_id)"
        ), {"project_id": project_id, "workspace_id": workspace_id})

    connection.execute(text(
        "UPDATE client_leads SET qualified_at=updated_at "
        "WHERE qualified_at IS NULL AND status IN ('qualified', 'proposal', 'won')"
    ))
    won_rows = connection.execute(text(
        "SELECT l.id, l.project_id, l.value, l.updated_at FROM client_leads l "
        "WHERE l.status='won' AND l.project_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM client_sales s WHERE s.lead_id=l.id)"
    )).all()
    for lead_id, project_id, amount, occurred_at in won_rows:
        connection.execute(text(
            "INSERT INTO client_sales (lead_id, project_id, amount, occurred_at) "
            "VALUES (:lead_id, :project_id, :amount, :occurred_at)"
        ), {"lead_id": lead_id, "project_id": project_id, "amount": amount, "occurred_at": occurred_at})

    # Keep existing Lead/Sale rows as analytics facts. The CRM wraps them with
    # contacts and deals; repeated startups only insert missing wrappers.
    for workspace_id, project_id in connection.execute(text("SELECT workspace_id, id FROM projects")).all():
        pipeline_id = connection.execute(text(
            "SELECT id FROM crm_pipelines WHERE project_id=:p AND is_default=true ORDER BY id LIMIT 1"
        ), {"p": project_id}).scalar()
        if pipeline_id is None:
            pipeline_id = connection.execute(text(
                "INSERT INTO crm_pipelines (workspace_id, project_id, name, is_default) "
                "VALUES (:w, :p, 'Основная воронка', true) RETURNING id"
            ), {"w": workspace_id, "p": project_id}).scalar_one()
        defaults = [("Новый лид", "LEAD", "#006BFD"), ("Квалифицирован", "QUALIFIED", "#8555E8"),
                    ("Продажа", "WON", "#15A86B"), ("Отказ", "LOST", "#EF4A59")]
        for position, (name, analytics_type, color) in enumerate(defaults):
            exists = connection.execute(text(
                "SELECT id FROM crm_stages WHERE pipeline_id=:p AND analytics_type=:t LIMIT 1"
            ), {"p": pipeline_id, "t": analytics_type}).scalar()
            if exists is None:
                connection.execute(text(
                    "INSERT INTO crm_stages (pipeline_id, name, analytics_type, color, position, required_fields) "
                    "VALUES (:p, :n, :t, :c, :o, '[]')"
                ), {"p": pipeline_id, "n": name, "t": analytics_type, "c": color, "o": position})
        stages = dict(connection.execute(text(
            "SELECT analytics_type, id FROM crm_stages WHERE pipeline_id=:p ORDER BY position"
        ), {"p": pipeline_id}).all())
        rows = connection.execute(text(
            "SELECT l.id, l.full_name, l.phone, l.email, l.telegram, l.assigned_to_id, l.value, "
            "l.source_id, l.status, l.created_at FROM client_leads l "
            "WHERE l.project_id=:p AND NOT EXISTS (SELECT 1 FROM crm_deals d WHERE d.lead_id=l.id)"
        ), {"p": project_id}).all()
        for lead_id, name, phone, email, telegram, owner, value, source_id, status, created_at in rows:
            contact_id = connection.execute(CrmContact.__table__.insert().values(
                workspace_id=workspace_id, project_id=project_id, name=name,
                phones=[phone] if phone else [], emails=[email] if email else [], telegram=telegram,
                phone_normalized="".join(ch for ch in phone if ch.isdigit()) if phone else None,
                email_normalized=email.lower() if email else None
            ).returning(CrmContact.id)).scalar_one()
            kind = "WON" if status == "won" else "LOST" if status == "lost" else "QUALIFIED" if status in ("qualified", "proposal") else "LEAD"
            deal_id = connection.execute(text(
                "INSERT INTO crm_deals (workspace_id, project_id, contact_id, lead_id, pipeline_id, stage_id, "
                "responsible_user_id, name, amount, source_id, origin, custom_fields, attribution_snapshot, created_at) "
                "VALUES (:w, :p, :c, :l, :pipeline, :stage, :owner, :n, :amount, :source, 'IMPORT', '{}', '{}', :created) RETURNING id"
            ), {"w": workspace_id, "p": project_id, "c": contact_id, "l": lead_id, "pipeline": pipeline_id,
                "stage": stages[kind], "owner": owner, "n": name, "amount": value, "source": source_id,
                "created": created_at}).scalar_one()
            attribution = connection.execute(text(
                "SELECT external_campaign_id, external_ad_id, utm_source, utm_medium, utm_campaign, "
                "utm_content, utm_term, landing_url FROM client_lead_attributions WHERE lead_id=:l LIMIT 1"
            ), {"l": lead_id}).mappings().first()
            if attribution:
                snapshot = {key: value for key, value in attribution.items() if value is not None}
                connection.execute(CrmDeal.__table__.update().where(CrmDeal.id == deal_id).values(
                    attribution_snapshot=snapshot))
            connection.execute(text("UPDATE client_sales SET deal_id=:d WHERE lead_id=:l AND deal_id IS NULL"),
                               {"d": deal_id, "l": lead_id})
        connection.execute(text(
            "UPDATE client_sales SET deal_id=(SELECT d.id FROM crm_deals d WHERE d.lead_id=client_sales.lead_id) "
            "WHERE project_id=:p AND deal_id IS NULL"
        ), {"p": project_id})
    for contact_id, phones, emails in connection.execute(
        text("SELECT id, phones, emails FROM crm_contacts WHERE phone_normalized IS NULL OR email_normalized IS NULL")
    ).all():
        import json
        phones = json.loads(phones) if isinstance(phones, str) else phones
        emails = json.loads(emails) if isinstance(emails, str) else emails
        first_phone = (phones or [None])[0]
        first_email = (emails or [None])[0]
        connection.execute(text("UPDATE crm_contacts SET phone_normalized=:phone, email_normalized=:email WHERE id=:id"),
                           {"id": contact_id, "phone": "".join(c for c in first_phone if c.isdigit()) if first_phone else None,
                            "email": first_email.lower() if first_email else None})
