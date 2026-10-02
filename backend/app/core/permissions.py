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
})

ROLE_DEFAULTS = {
    "client_owner": CAPABILITIES,
    "client_marketer": frozenset({"view_result", "view_analytics", "view_leads", "view_sales",
                                    "view_ads", "view_campaigns", "manage_campaigns", "view_crm", "view_all_deals"}),
    "sales_head": frozenset({"view_result", "view_analytics", "view_leads", "manage_leads",
                              "view_sales", "manage_sales", "view_crm", "view_all_deals", "create_deal", "edit_deal",
                              "move_deal", "archive_deal", "manage_tasks", "manage_pipeline", "manage_custom_fields", "create_sale"}),
    "sales_manager": frozenset({"view_result", "view_analytics", "view_leads", "manage_leads", "view_sales", "manage_sales",
                                   "view_crm", "view_own_deals", "create_deal", "edit_deal", "move_deal", "manage_tasks", "create_sale"}),
    "viewer": frozenset({"view_result", "view_analytics", "view_leads", "view_sales",
                          "view_ads", "view_campaigns", "view_crm", "view_all_deals"}),
}

SECTION_CAPABILITIES = {
    "result": ("view_result", None),
    "analytics": ("view_analytics", None),
    "leads": ("view_leads", "manage_leads"),
    "crm": ("view_crm", "edit_deal"),
    "sales": ("view_sales", "manage_sales"),
    "ads": ("view_ads", "manage_integrations"),
    "campaigns": ("view_campaigns", "manage_campaigns"),
    "team": ("manage_team", "manage_team"),
    "settings": ("manage_settings", "manage_settings"),
}


def effective_permissions(user) -> set[str]:
    if user.role == "client_owner":
        return set(CAPABILITIES)
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
        return permissions
    permissions = set(ROLE_DEFAULTS.get(user.role, ()))
    if user.manage_sources:
        permissions.add("manage_sources")
        permissions.add("edit_manual_metrics")
    if user.manage_integrations:
        permissions.add("manage_integrations")
    return permissions


def require_permission(user, permission: str):
    if permission not in effective_permissions(user):
        raise HTTPException(403, f"Недостаточно прав: {permission}")


def require_actor_permission(kind: str, user, permission: str):
    if kind != "admin":
        require_permission(user, permission)
