import secrets
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.status import HTTP_303_SEE_OTHER

from swarmer import k8s_auth
from swarmer.config import settings
from swarmer.crypto import encrypt
from swarmer.flash import flash
from swarmer.models.workspace import Workspace

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("authenticated"):
        return RedirectResponse("/workspaces", status_code=HTTP_303_SEE_OTHER)
    openshift_auth_url = None
    if settings.openshift_oauth_url:
        state = secrets.token_urlsafe(16)
        request.session["oauth_state"] = state
        if settings.redirect_base_url:
            callback_url = f"{settings.redirect_base_url.rstrip('/')}/auth/callback"
        else:
            callback_url = str(request.url_for("oauth_callback"))
        openshift_auth_url = (
            f"{settings.openshift_oauth_url}/oauth/authorize"
            f"?client_id=swarmer&response_type=token"
            f"&redirect_uri={quote(str(callback_url), safe='')}"
            f"&state={state}"
        )
    return templates.TemplateResponse(
        request,
        "login.html",
        {"openshift_auth_url": openshift_auth_url},
    )


async def _validate_and_login(request: Request, token: str):
    identity = await k8s_auth.validate_token(token, settings.k8s_api_url, settings.k8s_in_cluster)
    if identity is None:
        flash(request, "Invalid token.", "error")
        return None

    from swarmer.database import _AsyncSessionLocal
    async with _AsyncSessionLocal() as db:
        workspaces = (await db.execute(select(Workspace))).scalars().all()
        namespaces = [ws.k8s_namespace for ws in workspaces]

    if namespaces:
        accessible = await k8s_auth.get_accessible_namespaces(
            token, namespaces, settings.k8s_api_url, settings.k8s_in_cluster
        )
        if not accessible:
            flash(request, "Token is valid but has no access to any workspace namespace.", "error")
            return None

    request.session["authenticated"] = True
    request.session["k8s_token"] = encrypt(token)
    request.session["username"] = identity.username
    return identity


@router.post("/login")
async def login(request: Request, token: str = Form(...)):
    identity = await _validate_and_login(request, "".join(token.split()))
    if identity is None:
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse("/workspaces", status_code=HTTP_303_SEE_OTHER)


@router.get("/auth/callback", name="oauth_callback", response_class=HTMLResponse)
async def oauth_callback_page(request: Request):
    return templates.TemplateResponse(request, "auth_callback.html")


@router.post("/auth/callback")
async def oauth_callback(request: Request, token: str = Form(...), state: str = Form("")):
    expected = request.session.pop("oauth_state", None)
    if not expected or state != expected:
        flash(request, "Invalid OAuth state. Please sign in again.", "error")
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    identity = await _validate_and_login(request, "".join(token.split()))
    if identity is None:
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse("/workspaces", status_code=HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
