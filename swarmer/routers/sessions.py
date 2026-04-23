import asyncio
import logging
import re
import uuid

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from swarmer import k8s, k8s_session as k8s_sess
from swarmer.agent_tools.registry import get as get_tool, all_tools
from swarmer.config import settings
from swarmer.database import get_db
from swarmer.deps import require_auth
from swarmer.ansi import ansi_to_html
from swarmer.flash import flash
from swarmer.models.github_pat import GitHubPAT
from swarmer.models.opencode_secret import OpencodeSecret
from swarmer.models.session import Session
from swarmer.models.session_repo import SessionRepo
from swarmer.models.workspace import Workspace

log = logging.getLogger(__name__)


async def _get_model_options(
    ws_id: int, db: AsyncSession, agent_tool: str = "opencode"
) -> list[dict]:
    """Return the available model choices for this workspace's sessions."""
    tool = get_tool(agent_tool)
    result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == ws_id)
    )
    oc = result.scalar_one_or_none()
    return tool.get_model_options(oc)

def _github_slug(url: str) -> str | None:
    """Extract 'owner/repo' from a GitHub URL, or None if not a GitHub URL."""
    m = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


async def _fetch_repo_info(repos: list, pat: str | None) -> dict:
    """Return per-repo visibility and push-access info via the GitHub API.

    Result shape: {repo_id: {"is_public": bool|None, "can_push": bool|None}}
    None means the check could not be performed (non-GitHub URL, API error, etc.)
    """
    headers = {"Accept": "application/vnd.github+json"}
    if pat:
        headers["Authorization"] = f"token {pat}"

    async def _check(client: httpx.AsyncClient, repo) -> tuple[int, dict]:
        slug = _github_slug(repo.repo_url)
        if not slug:
            return repo.id, {"is_public": None, "can_push": None}
        try:
            r = await client.get(
                f"https://api.github.com/repos/{slug}", headers=headers
            )
            if r.status_code == 200:
                data = r.json()
                perms = data.get("permissions", {})
                return repo.id, {
                    "is_public": not data.get("private", True),
                    "can_push": perms.get("push"),
                }
            # 404 with no auth → private repo that the token can't see
            return repo.id, {"is_public": None, "can_push": False if pat else None}
        except Exception:
            return repo.id, {"is_public": None, "can_push": None}

    async with httpx.AsyncClient(timeout=5) as client:
        results = await asyncio.gather(*[_check(client, r) for r in repos])
    return dict(results)


router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")
templates.env.filters['ansi_to_html'] = ansi_to_html


async def _get_workspace(ws_id: int, db: AsyncSession) -> Workspace | None:
    return await db.get(Workspace, ws_id)


# ============================================================
# Model options (HTMX partial — reloads when agent tool changes)
# ============================================================

@router.get(
    "/workspaces/{ws_id}/sessions/model-options",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def model_options_partial(
    ws_id: int,
    request: Request,
    agent_tool: str = "opencode",
    selected_model: str = "",
    db: AsyncSession = Depends(get_db),
):
    model_options = await _get_model_options(ws_id, db, agent_tool)
    return templates.TemplateResponse(
        request,
        "sessions/_model_select.html",
        {
            "model_options": model_options,
            "selected_model": selected_model,
        },
    )


def _session_mode_label(session: Session) -> str:
    """Return a human-readable mode label for the session."""
    return {"tui": "TUI", "server": "Server", "prompt": "Prompt"}.get(session.mode, session.mode)


def _session_mode_badge_class(session: Session) -> str:
    return {"tui": "primary", "server": "info", "prompt": "secondary"}.get(session.mode, "secondary")


# ============================================================
# Session list
# ============================================================

@router.get("/workspaces/{ws_id}/sessions", dependencies=[Depends(require_auth)])
async def session_list(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    result = await db.execute(
        select(Session)
        .where(Session.workspace_id == ws_id)
        .options(selectinload(Session.github_pat), selectinload(Session.repos))
        .order_by(Session.name)
    )
    sessions = result.scalars().all()

    # Sync K8s phase for any sessions the DB still thinks are active
    dirty = False
    for s in sessions:
        if s.phase in ("pending", "running") and s.pod_name:
            live_phase, _ = await asyncio.to_thread(k8s.get_pod_status, s.pod_name, ws.k8s_namespace)
            if live_phase != s.phase:
                s.phase = live_phase
                dirty = True
    if dirty:
        await db.commit()

    return templates.TemplateResponse(
        request,
        "sessions/list.html",
        {
            "ws": ws,
            "sessions": sessions,
            "mode_label": _session_mode_label,
            "mode_badge": _session_mode_badge_class,
        },
    )


# ============================================================
# Create
# ============================================================

@router.get("/workspaces/{ws_id}/sessions/new", dependencies=[Depends(require_auth)])
async def session_new(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)
    pats_result = await db.execute(
        select(GitHubPAT).where(GitHubPAT.workspace_id == ws_id).order_by(GitHubPAT.name)
    )
    pats = pats_result.scalars().all()
    _tools = all_tools()
    try:
        default_agent_tool = get_tool(settings.default_agent_tool).name
    except ValueError:
        default_agent_tool = "opencode"
    model_options = await _get_model_options(ws_id, db, default_agent_tool)
    _avail = await asyncio.gather(
        *[k8s.get_image_available(t.get_image(), ws.k8s_namespace) for t in _tools]
    )
    return templates.TemplateResponse(
        request,
        "sessions/new.html",
        {
            "ws": ws,
            "pats": pats,
            "model_options": model_options,
            "selected_model": "",
            "agent_tools": _tools,
            "default_agent_tool": default_agent_tool,
            "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
        },
    )


@router.post("/workspaces/{ws_id}/sessions", dependencies=[Depends(require_auth)])
async def session_create(
    ws_id: int,
    request: Request,
    name: str = Form(...),
    github_pat_id: str = Form(""),
    instruction_prompt: str = Form(""),
    persist: bool = Form(False),
    resume: bool = Form(False),
    mode: str = Form("prompt"),
    model: str = Form(""),
    agent_tool: str = Form("opencode"),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    pat_id = int(github_pat_id) if github_pat_id else None
    if mode not in ("tui", "server", "prompt"):
        mode = "prompt"

    try:
        agent_tool = get_tool(agent_tool).name
    except ValueError:
        agent_tool = "opencode"

    if not model.strip():
        opts = await _get_model_options(ws_id, db, agent_tool)
        model = opts[0]["value"] if opts else ""

    session = Session(
        workspace_id=ws_id,
        github_pat_id=pat_id,
        name=name.strip(),
        mode=mode,
        model=model.strip(),
        persist=persist,
        resume=resume,
        instruction_prompt=instruction_prompt.strip(),
        agent_tool=agent_tool,
    )
    db.add(session)
    try:
        await db.commit()
        await db.refresh(session)
    except IntegrityError:
        await db.rollback()
        pats_result = await db.execute(
            select(GitHubPAT).where(GitHubPAT.workspace_id == ws_id)
        )
        pats = pats_result.scalars().all()
        _tools = all_tools()
        try:
            default_agent_tool = get_tool(settings.default_agent_tool).name
        except ValueError:
            default_agent_tool = "opencode"
        model_options = await _get_model_options(ws_id, db, default_agent_tool)
        _avail = await asyncio.gather(
            *[k8s.get_image_available(t.get_image(), ws.k8s_namespace) for t in _tools]
        )
        return templates.TemplateResponse(
            request,
            "sessions/new.html",
            {
                "ws": ws,
                "pats": pats,
                "error": f"A session named '{name}' already exists in this workspace.",
                "form": {"name": name, "instruction_prompt": instruction_prompt},
                "model_options": model_options,
                "selected_model": model,
                "agent_tools": _tools,
                "default_agent_tool": default_agent_tool,
                "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
            },
            status_code=422,
        )

    await db.commit()

    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{session.id}", status_code=302)


# ============================================================
# Detail
# ============================================================

@router.get("/workspaces/{ws_id}/sessions/{sid}", dependencies=[Depends(require_auth)])
async def session_detail(
    ws_id: int, sid: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(
        Session,
        sid,
        options=[selectinload(Session.github_pat), selectinload(Session.repos)],
    )
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    # Generate one-time TUI token for TUI-mode sessions
    tui_token = None
    if session.mode == "tui" and session.phase == "running":
        tui_token = str(uuid.uuid4())
        tokens = list(request.session.get("tui_tokens", []))
        tokens.append(tui_token)
        request.session["tui_tokens"] = tokens

    pats_result = await db.execute(
        select(GitHubPAT).where(GitHubPAT.workspace_id == ws_id).order_by(GitHubPAT.name)
    )
    pats = pats_result.scalars().all()

    # Fetch live K8s detail for the initial page render and sync phase
    status_detail = ""
    if session.pod_name:
        live_phase, status_detail = await asyncio.to_thread(k8s.get_pod_status, session.pod_name, ws.k8s_namespace)
        if live_phase != session.phase:
            session.phase = live_phase
            await db.commit()

    model_options = await _get_model_options(ws_id, db, session.agent_tool)
    pat_token = session.github_pat.pat if session.github_pat else None
    repo_info = await _fetch_repo_info(session.repos, pat_token)

    _tools = all_tools()
    _avail = await asyncio.gather(
        *[k8s.get_image_available(t.get_image(), ws.k8s_namespace) for t in _tools]
    )
    try:
        canonical_agent_tool = get_tool(session.agent_tool).name
    except ValueError:
        canonical_agent_tool = session.agent_tool
    return templates.TemplateResponse(
        request,
        "sessions/detail.html",
        {
            "ws": ws,
            "ws_id": ws_id,
            "session": session,
            "canonical_agent_tool": canonical_agent_tool,
            "pats": pats,
            "tui_token": tui_token,
            "mode_label": _session_mode_label(session),
            "mode_badge": _session_mode_badge_class(session),
            "status_detail": status_detail,
            "model_options": model_options,
            "repo_info": repo_info,
            "agent_tools": _tools,
            "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
        },
    )


# ============================================================
# Edit
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/edit",
    dependencies=[Depends(require_auth)],
)
async def session_edit(
    ws_id: int,
    sid: int,
    request: Request,
    name: str = Form(...),
    github_pat_id: str = Form(""),
    instruction_prompt: str = Form(""),
    persist: bool = Form(False),
    resume: bool = Form(False),
    mode: str = Form("prompt"),
    model: str = Form(""),
    agent_tool: str = Form("opencode"),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    if session.is_active:
        flash(request, "Cannot edit a running session. Stop it first.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    session.name = name.strip()
    session.github_pat_id = int(github_pat_id) if github_pat_id else None
    session.instruction_prompt = instruction_prompt.strip()
    session.persist = persist
    session.resume = resume
    if mode in ("tui", "server", "prompt"):
        session.mode = mode
    session.model = model.strip()
    try:
        session.agent_tool = get_tool(agent_tool).name
    except ValueError:
        pass
    await db.commit()
    flash(request, "Session updated.", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Launch / Stop
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/launch",
    dependencies=[Depends(require_auth)],
)
async def session_launch(
    ws_id: int,
    sid: int,
    request: Request,
    agent_tool: str = Form(""),
    save_config: str = Form(""),
    name: str = Form(""),
    github_pat_id: str = Form(""),
    instruction_prompt: str = Form(""),
    persist: bool = Form(False),
    resume: bool = Form(False),
    mode: str = Form(""),
    model: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(
        Session,
        sid,
        options=[selectinload(Session.github_pat), selectinload(Session.repos)],
    )
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    if session.is_active:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    if save_config:
        if name.strip():
            session.name = name.strip()
        session.github_pat_id = int(github_pat_id) if github_pat_id else None
        session.instruction_prompt = instruction_prompt.strip()
        session.persist = persist
        session.resume = resume
        if mode in ("tui", "server", "prompt"):
            session.mode = mode
        if model.strip():
            session.model = model.strip()

    if agent_tool:
        try:
            canonical = get_tool(agent_tool).name
            if canonical != session.agent_tool:
                session.agent_tool = canonical
                session.model = ""  # stale model from previous tool may be incompatible
        except ValueError:
            pass

    # Generate a shared suffix so the pod and PVC share the same identifier
    import secrets as _secrets
    suffix = _secrets.token_hex(4)

    # Ensure PVC exists; if it was deleted (persist=False), a new one is created
    # with the same suffix as the pod about to be launched.
    try:
        pvc_name = k8s_sess.ensure_session_pvc(ws.k8s_namespace, session.id, suffix, session.pvc_name)
        if pvc_name != session.pvc_name:
            session.pvc_name = pvc_name
    except Exception as exc:
        flash(request, f"PVC error: {exc}", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    # Check whether the workspace has an ADC JSON stored (affects pod spec)
    from swarmer.models.opencode_secret import OpencodeSecret
    oc_result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == ws_id)
    )
    oc_secret = oc_result.scalar_one_or_none()
    has_adc    = oc_secret.has_adc if oc_secret else False
    has_gemini = bool(oc_secret and oc_secret.google_api_key_enc)

    # Build and create pod
    try:
        pod_spec = k8s_sess.build_session_pod(
            session=session,
            namespace=ws.k8s_namespace,
            image=settings.agent_image,
            suffix=suffix,
            image_pull_secret=k8s.PULL_SECRET_NAME,
            has_adc=has_adc,
            has_gemini=has_gemini,
            privileged=session.privileged,
            agent_tool=session.agent_tool,
        )
        from kubernetes import client as k8s_client

        v1 = k8s_client.CoreV1Api()

        if session.pod_name:
            k8s.delete_pod(session.pod_name, ws.k8s_namespace)

        pod = v1.create_namespaced_pod(ws.k8s_namespace, pod_spec)
        session.pod_name = pod.metadata.name
        session.phase = "pending"

        # Create a Service (and OpenShift Route) for server-mode sessions
        if session.mode == "server":
            tool = get_tool(session.agent_tool)
            port = tool.get_server_port() or 4096
            svc_name = k8s_sess.create_session_service(session.id, ws.k8s_namespace, port=port)
            k8s.create_session_route(session.id, ws.k8s_namespace, svc_name, port)

        await db.commit()
        if session.mode in ("prompt", "server"):
            from swarmer import log_poller
            log_poller.start_log_poller(session.id, session.pod_name, ws.k8s_namespace, session.mode)
    except Exception as exc:
        log.error("session_launch failed for session %d: %s", sid, exc, exc_info=True)
        flash(request, f"Launch failed: {exc}", "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/stop",
    dependencies=[Depends(require_auth)],
)
async def session_stop(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    if session.pod_name:
        from swarmer import log_poller
        log_poller.stop_log_poller(sid)
        try:
            k8s.delete_pod(session.pod_name, ws.k8s_namespace)
            if session.mode == "server":
                k8s.delete_service(f"session-{session.id}-svc", ws.k8s_namespace)
                k8s.delete_session_route(session.id, ws.k8s_namespace)
        except Exception as exc:
            flash(request, f"Pod deletion failed: {exc}", "warning")

    if not session.persist and session.pvc_name:
        try:
            k8s_sess.delete_session_pvc(ws.k8s_namespace, session.pvc_name)
            session.pvc_name = None
        except Exception as exc:
            flash(request, f"PVC deletion failed: {exc}", "warning")

    session.phase = "stopped"
    session.pod_name = None
    await db.commit()
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Status polling (HTMX)
# ============================================================

@router.get(
    "/workspaces/{ws_id}/sessions/{sid}/status",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def session_status(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None:
        return HTMLResponse("")

    # Sync live pod phase into the DB so the badge reflects actual K8s state.
    # The log_poller handles this for prompt mode; for TUI/server we do it here
    # on each poll so the JS handler can detect the running→Active transition.
    status_detail = session.status_detail
    if session.pod_name and session.phase in ("pending", "running"):
        live_phase, live_detail = await asyncio.to_thread(
            k8s.get_pod_status, session.pod_name, ws.k8s_namespace
        )
        if live_phase != session.phase or live_detail != session.status_detail:
            session.phase = live_phase
            session.status_detail = live_detail
            await db.commit()
        status_detail = live_detail

    return templates.TemplateResponse(
        request,
        "sessions/_status_badge.html",
        {
            "ws": ws,
            "session": session,
            "mode_label": _session_mode_label(session),
            "status_detail": status_detail,
        },
    )


# ============================================================
# Last-output polling (HTMX)
# ============================================================

@router.get(
    "/workspaces/{ws_id}/sessions/{sid}/last-output",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def session_last_output(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request,
        "sessions/_last_output.html",
        {"ws_id": ws_id, "session": session},
    )


# ============================================================
# Clear output
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/clear-output",
    dependencies=[Depends(require_auth)],
)
async def session_clear_output(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    session.last_output = ""
    await db.commit()
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Delete
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/delete",
    dependencies=[Depends(require_auth)],
)
async def session_delete(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    if session.is_active:
        flash(request, "Stop the session before deleting it.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    if session.pvc_name:
        try:
            k8s_sess.delete_session_pvc(ws.k8s_namespace, session.pvc_name)
        except Exception as exc:
            flash(request, f"PVC deletion failed: {exc}", "warning")

    await db.delete(session)
    await db.commit()
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)


# ============================================================
# Set name (inline rename on detail page)
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/set-name",
    dependencies=[Depends(require_auth)],
)
async def session_set_name(
    ws_id: int,
    sid: int,
    request: Request,
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    if session.is_active:
        flash(request, "Cannot rename a running session. Stop it first.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    session.name = name.strip()
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        flash(request, f"A session named '{name}' already exists in this workspace.", "danger")
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Set mode (inline dropdown on detail page)
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/set-mode",
    dependencies=[Depends(require_auth)],
)
async def session_set_mode(
    ws_id: int,
    sid: int,
    request: Request,
    mode: str = Form("run"),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    if session.is_active:
        flash(request, "Cannot change mode while session is active. Stop it first.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    if mode in ("tui", "server", "prompt"):
        session.mode = mode
    await db.commit()
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Set model (server / TUI modes — works while running)
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/set-model",
    dependencies=[Depends(require_auth)],
)
async def session_set_model(
    ws_id: int,
    sid: int,
    request: Request,
    model: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    session.model = model.strip()
    await db.commit()

    if session.is_active and session.pod_name:
        try:
            tool = get_tool(session.agent_tool)
            tool.exec_model_update(session.pod_name, ws.k8s_namespace, session.model)
            flash(request, "Model applied to running pod.", "success")
        except Exception as exc:
            flash(request, f"Model saved but could not apply to running pod: {exc}", "warning")
    else:
        flash(request, "Model saved; will apply on next launch.", "success")

    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Git Repos
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/repos",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def repo_add(
    ws_id: int,
    sid: int,
    request: Request,
    repo_url: str = Form(...),
    branch: str = Form("main"),
    local_path: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, sid, options=[selectinload(Session.repos)])
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("")

    if not local_path:
        local_path = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")

    repo = SessionRepo(
        session_id=sid,
        repo_url=repo_url.strip(),
        branch=branch.strip() or "main",
        local_path=local_path.strip(),
    )
    db.add(repo)
    await db.commit()

    result = await db.execute(
        select(Session)
        .where(Session.id == sid)
        .options(selectinload(Session.repos), selectinload(Session.github_pat))
    )
    session = result.scalar_one()
    pat_token = session.github_pat.pat if session.github_pat else None
    repo_info = await _fetch_repo_info(session.repos, pat_token)
    return templates.TemplateResponse(
        request,
        "sessions/_repo_list.html",
        {"ws_id": ws_id, "session": session, "repo_info": repo_info},
    )


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/repos/{rid}/delete",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def repo_delete(
    ws_id: int,
    sid: int,
    rid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    repo = await db.get(SessionRepo, rid)
    if repo and repo.session_id == sid:
        await db.delete(repo)
        await db.commit()

    session = await db.get(Session, sid, options=[selectinload(Session.repos), selectinload(Session.github_pat)])
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("")
    pat_token = session.github_pat.pat if session.github_pat else None
    repo_info = await _fetch_repo_info(session.repos, pat_token)
    return templates.TemplateResponse(
        request,
        "sessions/_repo_list.html",
        {"ws_id": ws_id, "session": session, "repo_info": repo_info},
    )
