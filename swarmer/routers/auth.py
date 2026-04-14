from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from swarmer import auth as auth_module
from swarmer.config import settings
from swarmer.deps import require_auth

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")


@router.get("/login")
async def login_page(request: Request):
    if request.session.get("authenticated"):
        return RedirectResponse(url="/workspaces", status_code=302)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": None}
    )


@router.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    error = None
    try:
        stored_hash = auth_module.load_hash(settings.auth_hash_file)
        if auth_module.verify_password(password, stored_hash):
            request.session["authenticated"] = True
            return RedirectResponse(url="/workspaces", status_code=302)
        error = "Invalid password."
    except FileNotFoundError:
        error = "Auth not configured. Run: make setup-auth"
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": error}, status_code=401
    )


@router.post("/logout")
async def logout(request: Request, _auth=Depends(require_auth)):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
