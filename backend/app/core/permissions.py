"""Client-cabinet capabilities. Admin sessions are intentionally separate."""

from fastapi import HTTPException

CAPABILITIES = frozenset({
    "view_result", "view_analytics", "view_leads", "manage_leads",
    "view_sales", "manage_sales", "view_ads", "manage_integrations",
    "view_campaigns", "manage_campaigns", "manage_sources",
    "edit_manual_metrics", "manage_team", "manage_settings",
    "view_crm", "create_deal", "edit_deal", "move_deal", "archive_deal",
    "view_all_deals", "view_own_deals", "manage_tasks", "manage_pipeline",
    "manage_custom_fields", "change_attribution", "create_sale",
    "manage_reports", "exclude_analytics",
})
# Техническая настройка и маркетинг — только у StepToLead: админ агентства и сотрудник StepToLead
# в проекте (PortalUser.is_agency). Клиентским ролям не выдаются даже вручную.
AGENCY_ONLY = frozenset({
    "manage_integrations", "manage_sources", "edit_manual_metrics", "manage_campaigns", "change_attribution",
    "view_ads", "view_campaigns", "manage_reports",
})
CLIENT_CAPABILITIES = CAPABILITIES - AGENCY_ONLY
_VIEWER = frozenset({"view_result", "view_analytics", "view_leads", "view_sales", "view_crm", "view_all_deals"})

ROLE_DEFAULTS = {
    "client_owner": CLIENT_CAPABILITIES,
    # Роль «маркетолог клиента» упразднена: маркетинг ведёт StepToLead. Старые аккаунты — как наблюдатель.
    "client_marketer": _VIEWER,
    "sales_head": frozenset({"view_result", "view_analytics", "view_leads", "manage_leads",
                              "view_sales", "manage_sales", "view_crm", "view_all_deals", "create_deal", "edit_deal",
                              "move_deal", "archive_deal", "manage_tasks", "manage_pipeline", "manage_custom_fields", "create_sale",
                              "exclude_analytics"}),
    "sales_manager": frozenset({"view_result", "view_analytics", "view_leads", "manage_leads", "view_sales", "manage_sales",
                                   "view_crm", "view_own_deals", "create_deal", "edit_deal", "move_deal", "manage_tasks", "create_sale"}),
    "viewer": _VIEWER,
}

SECTION_CAPABILITIES = {
    "result": ("view_result", None),
    "analytics": ("view_analytics", None),
    "leads": ("view_leads", "manage_leads"),
    "crm": ("view_crm", "edit_deal"),
    "sales": ("view_sales", "manage_sales"),
    "team": ("manage_team", "manage_team"),
    "settings": ("manage_settings", "manage_settings"),
}


def is_agency(user) -> bool:
    return bool(getattr(user, "is_agency", False))


def can_setup_integrations(user) -> bool:
    """Подключения и источники: StepToLead; в демо и пробном периоде — ещё собственник клиента."""
    return is_agency(user) or (bool(getattr(user, "demo_mode", False)) and user.role == "client_owner")


def effective_permissions(user) -> set[str]:
    if is_agency(user):
        return set(CAPABILITIES)
    if getattr(user, "demo_mode", False):
        # Демо/пробный период не трогаем: прежние права, только без конструктора отчётов.
        if user.role == "client_owner":
            return set(CAPABILITIES - {"manage_reports"})
        return set(ROLE_DEFAULTS.get(user.role, ())) | ({"view_ads", "view_campaigns"} if user.role == "viewer" else set())
    if user.role == "client_owner":
        return set(CLIENT_CAPABILITIES)
    if user.permissions is not None:
        permissions = set(user.permissions) & CAPABILITIES
        if "view_leads" in permissions:
            permissions.add("view_crm")
            permissions.add("view_own_deals" if user.role == "sales_manager" else "view_all_deals")
        if "manage_leads" in permissions:
            permissions.update({"create_deal", "edit_deal", "move_deal", "manage_tasks"})
        if "manage_sales" in permissions:
            permissions.add("create_sale")
        if "manage_settings" in permissions:
            permissions.update({"manage_pipeline", "manage_custom_fields"})
        return permissions - AGENCY_ONLY
    return set(ROLE_DEFAULTS.get(user.role, ())) - AGENCY_ONLY


def require_permission(user, permission: str):
    if permission not in effective_permissions(user):
        raise HTTPException(403, f"Недостаточно прав: {permission}")


def require_actor_permission(kind: str, user, permission: str):
    if kind != "admin":
        require_permission(user, permission)
