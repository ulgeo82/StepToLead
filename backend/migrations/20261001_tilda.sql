-- Tilda adapter metadata; CRM leads and inbound queue remain unchanged.
CREATE TABLE IF NOT EXISTS tilda_connections (
    id SERIAL PRIMARY KEY,
    public_id VARCHAR(64) NOT NULL UNIQUE,
    secret_hash VARCHAR(64) NOT NULL,
    organization_id INTEGER NOT NULL REFERENCES client_workspaces(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    website_id INTEGER NOT NULL REFERENCES website_sites(id) ON DELETE CASCADE,
    inbound_source_id INTEGER NOT NULL UNIQUE REFERENCES lead_inbound_sources(id),
    allowed_form_id VARCHAR(100) NOT NULL DEFAULT 'form3645799701',
    form_name VARCHAR(180) NOT NULL DEFAULT 'Рассчитаем потенциал продвижения',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_received_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_tilda_connections_public_id ON tilda_connections(public_id);
CREATE INDEX IF NOT EXISTS ix_tilda_connections_organization_id ON tilda_connections(organization_id);
CREATE INDEX IF NOT EXISTS ix_tilda_connections_project_id ON tilda_connections(project_id);
CREATE INDEX IF NOT EXISTS ix_tilda_connections_website_id ON tilda_connections(website_id);

CREATE TABLE IF NOT EXISTS tilda_receipts (
    id SERIAL PRIMARY KEY,
    tilda_connection_id INTEGER NOT NULL REFERENCES tilda_connections(id) ON DELETE CASCADE,
    tranid VARCHAR(180) NOT NULL,
    inbound_id INTEGER NOT NULL REFERENCES crm_inbound(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_tilda_connection_tranid UNIQUE(tilda_connection_id, tranid)
);
CREATE INDEX IF NOT EXISTS ix_tilda_receipts_connection_id ON tilda_receipts(tilda_connection_id);
CREATE INDEX IF NOT EXISTS ix_tilda_receipts_inbound_id ON tilda_receipts(inbound_id);
