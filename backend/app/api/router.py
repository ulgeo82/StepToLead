from fastapi import APIRouter, Depends
from app.core.access import require_admin
from app.api.routes import access, growth

from app.api.routes import campaigns, leads, proxies, telegram_accounts, telegram_parser, marketing, portal
from app.api.routes import automation

internal_router = APIRouter(dependencies=[Depends(require_admin)])
internal_router.include_router(campaigns.router)
internal_router.include_router(leads.router)
internal_router.include_router(telegram_accounts.router)
internal_router.include_router(telegram_parser.router)
internal_router.include_router(proxies.router)
internal_router.include_router(automation.router)
internal_router.include_router(marketing.router)
api_router = APIRouter()
api_router.include_router(access.router)
api_router.include_router(growth.public_router)
api_router.include_router(growth.admin_router)
api_router.include_router(portal.auth_router)
api_router.include_router(portal.inbound_router)
api_router.include_router(portal.admin_router)
api_router.include_router(portal.portal_router)
api_router.include_router(internal_router)
