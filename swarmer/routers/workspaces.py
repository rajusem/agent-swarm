import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from swarmer import k8s
from swarmer import k8s_auth
from swarmer.config import settings
from swarmer.database import get_db
from swarmer.deps import NotAuthenticated, get_user_token, require_auth
from swarmer.flash import flash
from swarmer.models.workspace import Workspace

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")


def _derive_namespace(display_name: str) -> str:
    slug = display_name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")[:63]


# ---------- Workspace list ----------

@router.get("/workspaces", dependencies=[Depends(require_auth)])
async def workspace_list(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Workspace))
    all_workspaces = result.scalars().all()

    try:
        user_token = get_user_token(request)
    except NotAuthenticated:
        user_token = None

    if user_token:
        ns_to_ws = {ws.k8s_namespace: ws for ws in all_workspaces}
        accessible_ns = await k8s_auth.get_accessible_namespaces(
            user_token, list(ns_to_ws), settings.k8s_api_url, settings.k8s_in_cluster
        )
        if accessible_ns:
            workspaces = sorted([ns_to_ws[ns] for ns in accessible_ns], key=lambda w: w.display_name)
        else:
            # Token expired or user has no namespace-level RBAC — show all
            workspaces = sorted(all_workspaces, key=lambda w: w.display_name)
    else:
        workspaces = sorted(all_workspaces, key=lambda w: w.display_name)

    return templates.TemplateResponse(
        request,
        "workspaces/list.html",
        {"workspaces": workspaces},
    )


# ---------- Namespace preview (HTMX) ----------

@router.get(
    "/workspaces/preview-namespace",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def preview_namespace(name: str = ""):
    return HTMLResponse(_derive_namespace(name) or "&nbsp;")


# ---------- Create ----------

@router.get("/workspaces/new", dependencies=[Depends(require_auth)])
async def workspace_new(request: Request):
    return templates.TemplateResponse(
        request,
        "workspaces/new.html",
    )


@router.post("/workspaces", dependencies=[Depends(require_auth)])
async def workspace_create(
    request: Request,
    display_name: str = Form(...),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    namespace = _derive_namespace(display_name)
    if not namespace:
        return templates.TemplateResponse(
            request,
            "workspaces/new.html",
            {"error": "Display name must contain at least one alphanumeric character."},
            status_code=422,
        )

    ws = Workspace(
        display_name=display_name.strip(),
        namespace=namespace,
        description=description.strip(),
    )
    db.add(ws)
    try:
        await db.commit()
        await db.refresh(ws)
    except IntegrityError:
        await db.rollback()
        return templates.TemplateResponse(
            request,
            "workspaces/new.html",
            {
                "error": f"A workspace with namespace '{namespace}' already exists.",
                "display_name": display_name,
                "description": description,
            },
            status_code=422,
        )

    # Best-effort: create K8s namespace and agent config for all registered tools
    eff_ns = k8s.effective_namespace(namespace)
    try:
        if not settings.k8s_namespace:
            k8s.ensure_namespace(eff_ns)
        from swarmer.agent_tools.registry import all_tools
        for tool in all_tools():
            k8s.apply_agent_config(eff_ns, agent_tool=tool.name)
    except Exception as exc:
        flash(request, f"Workspace created but K8s setup failed: {exc}", "warning")

    flash(request, f"Workspace '{ws.display_name}' created.", "success")
    return RedirectResponse(url=f"/workspaces/{ws.id}", status_code=302)


# ---------- Detail ----------

@router.get("/workspaces/{ws_id}", dependencies=[Depends(require_auth)])
async def workspace_detail(
    ws_id: int, db: AsyncSession = Depends(get_db)
):
    ws = await db.get(Workspace, ws_id)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)


# ---------- Edit ----------

@router.get("/workspaces/{ws_id}/edit", dependencies=[Depends(require_auth)])
async def workspace_edit_form(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await db.get(Workspace, ws_id)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)
    return templates.TemplateResponse(
        request,
        "workspaces/edit.html",
        {"ws": ws},
    )


@router.post("/workspaces/{ws_id}/edit", dependencies=[Depends(require_auth)])
async def workspace_update(
    ws_id: int,
    request: Request,
    display_name: str = Form(...),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    ws = await db.get(Workspace, ws_id)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)
    ws.display_name = display_name.strip()
    ws.description = description.strip()
    await db.commit()
    flash(request, "Workspace updated.", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}", status_code=302)


# ---------- Delete ----------

@router.get(
    "/workspaces/{ws_id}/delete",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def workspace_delete_confirm(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    """Return an HTMX partial: the inline delete confirmation box."""
    ws = await db.get(Workspace, ws_id)
    if ws is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request,
        "workspaces/_delete_confirm.html",
        {"ws": ws, "error": None},
    )


@router.post("/workspaces/{ws_id}/delete", dependencies=[Depends(require_auth)])
async def workspace_delete(
    ws_id: int,
    request: Request,
    confirm_name: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    ws = await db.get(Workspace, ws_id)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    if confirm_name != ws.display_name:
        return templates.TemplateResponse(
            request,
            "workspaces/_delete_confirm.html",
            {
                "ws": ws,
                "error": "Name does not match. Please type the workspace name exactly.",
            },
        )

    # Delete K8s namespace first; abort if it fails for a non-404 reason
    try:
        if not settings.k8s_namespace:
            k8s.delete_namespace(ws.k8s_namespace)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "workspaces/_delete_confirm.html",
            {
                "ws": ws,
                "error": f"Kubernetes error: {exc}",
            },
        )

    await db.delete(ws)
    await db.commit()
    flash(request, f"Workspace '{ws.display_name}' deleted.", "success")
    return RedirectResponse(url="/workspaces", status_code=302)
