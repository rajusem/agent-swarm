import asyncio
import logging
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer import k8s
from swarmer.database import get_db
from swarmer.deps import require_auth
from swarmer.flash import flash
from swarmer.models.workspace import Workspace

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")
log = logging.getLogger(__name__)


async def _get_workspace(ws_id: int, db: AsyncSession) -> Workspace | None:
    return await db.get(Workspace, ws_id)


@router.get("/workspaces/{ws_id}/env-vars", dependencies=[Depends(require_auth)])
async def env_vars_list(request: Request, ws_id: int, db: AsyncSession = Depends(get_db)):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    try:
        env_vars = await asyncio.to_thread(k8s.get_extra_env_vars, ws.k8s_namespace)
    except Exception as exc:
        log.warning("Could not read extra env vars for %s: %s", ws.k8s_namespace, exc)
        env_vars = {}

    return templates.TemplateResponse(
        request,
        "env_vars/list.html",
        {"ws": ws, "env_vars": env_vars},
    )


@router.post("/workspaces/{ws_id}/env-vars", dependencies=[Depends(require_auth)])
async def env_vars_add(
    request: Request,
    ws_id: int,
    key: str = Form(...),
    value: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    key = key.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,254}", key):
        flash(request, "Invalid environment variable name. Must start with a letter or underscore and contain only letters, digits, and underscores (max 255 characters).", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/env-vars", status_code=302)

    try:
        await asyncio.to_thread(k8s.ensure_namespace, ws.k8s_namespace)
        await asyncio.to_thread(k8s.set_extra_env_var, ws.k8s_namespace, key, value)
        flash(request, f"Environment variable '{key}' saved.", "success")
    except Exception as exc:
        log.error("Failed to save env var for %s: %s", ws.k8s_namespace, exc)
        flash(request, f"Failed to save variable: {exc}", "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/env-vars", status_code=302)


@router.post("/workspaces/{ws_id}/env-vars/{key}/delete", dependencies=[Depends(require_auth)])
async def env_vars_delete(
    request: Request,
    ws_id: int,
    key: str,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    try:
        await asyncio.to_thread(k8s.delete_extra_env_var, ws.k8s_namespace, key)
        flash(request, f"Environment variable '{key}' deleted.", "success")
    except Exception as exc:
        log.error("Failed to delete env var for %s: %s", ws.k8s_namespace, exc)
        flash(request, f"Failed to delete variable: {exc}", "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/env-vars", status_code=302)
