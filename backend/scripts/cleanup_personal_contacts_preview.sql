-- Проверка только чтением. Подставить workspace из settings.agency_workspace_id:
-- psql: \set agency_workspace_id 123
-- Точные количества с учётом ВСЕХ фактических FK выдаёт python -m scripts.cleanup_personal_contacts.

-- В текущей схеме FK на lg_contacts: lg_touches.contact_id, lg_enrollments.contact_id.
SELECT conrelid::regclass AS referencing_table, pg_get_constraintdef(oid) AS reference
FROM pg_constraint
WHERE contype = 'f' AND confrelid = 'lg_contacts'::regclass;

WITH candidates AS (
    SELECT c.id,
        EXISTS (SELECT 1 FROM lg_touches t WHERE t.contact_id = c.id
            OR (t.company_id = c.company_id AND lower(t.address) = lower(c.value_norm))) AS touches,
        EXISTS (SELECT 1 FROM lg_enrollments e WHERE e.contact_id = c.id) AS enrollments,
        (co.crm_contact_id IS NOT NULL OR co.crm_deal_id IS NOT NULL) AS crm,
        EXISTS (SELECT 1 FROM lg_dnc d WHERE d.workspace_id = co.workspace_id AND (
            (d.kind = c.kind AND lower(d.value_norm) = lower(c.value_norm))
            OR (d.kind = 'company' AND d.value_norm = co.id::text)
            OR (d.kind = 'domain' AND d.value_norm = co.domain))) AS dnc
    FROM lg_contacts c
    JOIN lg_companies co ON co.id = c.company_id
    WHERE c.is_personal IS TRUE AND co.workspace_id = :agency_workspace_id
)
SELECT count(*) AS total,
    count(*) FILTER (WHERE NOT (touches OR enrollments OR crm OR dnc)) AS would_delete,
    count(*) FILTER (WHERE touches OR enrollments OR crm OR dnc) AS retained,
    count(*) FILTER (WHERE touches) AS lg_touches,
    count(*) FILTER (WHERE enrollments) AS lg_enrollments,
    count(*) FILTER (WHERE crm) AS crm,
    count(*) FILTER (WHERE dnc) AS lg_dnc,
    0 AS text_excerpt_cleaned
FROM candidates;
