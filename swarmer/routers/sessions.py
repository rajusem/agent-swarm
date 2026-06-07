import asyncio
import logging
import re
import shlex
import uuid
from datetime import datetime

import httpx

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
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
from swarmer.github import fetch_repo_info as _fetch_repo_info
from swarmer.github import list_repos_for_pat as _list_repos_for_pat
from swarmer.github_url_validator import GitHubURLError, validate_github_url
from swarmer.models.github_pat import GitHubPAT
from swarmer.models.opencode_secret import OpencodeSecret
from swarmer.models.session import CRON_PRESETS, Session
from swarmer.models.session_repo import SessionRepo
from swarmer.models.workspace import Workspace
from swarmer.models.workspace_prompt import WorkspacePrompt, WorkspacePromptSource

log = logging.getLogger(__name__)

_INVALID_REF_RE = re.compile(
    r"[\x00-\x1f\x7f ~^:?*\[\\]"
    r"|\.\.+"
    r"|@\{"
    r"|\.$"
    r"|\.lock$"
    r"|//"
)


def _is_valid_ref_name(name: str) -> bool:
    """Check whether *name* is a legal git ref component."""
    if not name or name.startswith("/") or name.endswith("/") or name.endswith("."):
        return False
    return _INVALID_REF_RE.search(name) is None


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

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")
templates.env.filters['ansi_to_html'] = ansi_to_html


def _current_user(request: Request) -> str:
    """Return the K8s username from the session, or '' if not set."""
    return request.session.get("username", "")


async def _visible_pats(ws_id: int, db: AsyncSession, user_id: str = "") -> list:
    """Return PATs visible to the given user (own + shared + legacy)."""
    filters = [GitHubPAT.workspace_id == ws_id]
    if user_id:
        filters.append(
            or_(
                GitHubPAT.user_id == user_id,
                GitHubPAT.shared == True,  # noqa: E712
                GitHubPAT.user_id == "",
            )
        )
    result = await db.execute(
        select(GitHubPAT).where(*filters).order_by(GitHubPAT.name)
    )
    return list(result.scalars().all())


async def _get_workspace(ws_id: int, db: AsyncSession) -> Workspace | None:
    return await db.get(Workspace, ws_id)


async def _get_prompt_sources(ws_id: int, db: AsyncSession) -> list[WorkspacePromptSource]:
    """Return all prompt sources and their prompts for this workspace."""
    result = await db.execute(
        select(WorkspacePromptSource)
        .where(WorkspacePromptSource.workspace_id == ws_id)
        .options(selectinload(WorkspacePromptSource.prompts))
        .order_by(WorkspacePromptSource.name)
    )
    return list(result.scalars().all())


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

async def _list_sessions_data(ws_id: int, db: AsyncSession):
    result = await db.execute(
        select(Session)
        .where(Session.workspace_id == ws_id)
        .options(selectinload(Session.github_pat), selectinload(Session.repos))
        .order_by(Session.name)
    )
    return result.scalars().all()


async def _sync_session_phases(sessions, ws, db: AsyncSession):
    dirty = False
    for s in sessions:
        # OpenShell sessions manage their own phase via _run_openshell_agent
        if s.phase in ("pending", "running") and s.pod_name and not s.sandbox_name:
            live_phase, _ = await asyncio.to_thread(k8s.get_pod_status, s.pod_name, ws.k8s_namespace)
            if live_phase != s.phase:
                s.phase = live_phase
                dirty = True
    if dirty:
        await db.commit()


@router.get("/workspaces/{ws_id}/sessions", dependencies=[Depends(require_auth)])
async def session_list(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    sessions = await _list_sessions_data(ws_id, db)
    await _sync_session_phases(sessions, ws, db)

    _tools = all_tools()
    _avail = await asyncio.gather(
        *[k8s.get_image_available(t.get_image(), ws.k8s_namespace) for t in _tools]
    )
    return templates.TemplateResponse(
        request,
        "sessions/list.html",
        {
            "ws": ws,
            "sessions": sessions,
            "mode_label": _session_mode_label,
            "mode_badge": _session_mode_badge_class,
            "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
        },
    )


@router.get(
    "/workspaces/{ws_id}/sessions/rows",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def session_list_rows(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return HTMLResponse("")

    sessions = await _list_sessions_data(ws_id, db)
    await _sync_session_phases(sessions, ws, db)

    _tools = all_tools()
    _avail = await asyncio.gather(
        *[k8s.get_image_available(t.get_image(), ws.k8s_namespace) for t in _tools]
    )

    queued_ids = [s.id for s in sessions if s.phase == "queued"]
    queue_positions: dict[int, tuple[int, int]] = {}
    for sid in queued_ids:
        queue_positions[sid] = await _get_queue_position(sid, db)

    capacity = await _get_capacity_summary(ws_id, db)

    return templates.TemplateResponse(
        request,
        "sessions/_list_rows.html",
        {
            "ws": ws,
            "sessions": sessions,
            "mode_label": _session_mode_label,
            "mode_badge": _session_mode_badge_class,
            "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
            "queue_positions": queue_positions,
            "capacity": capacity,
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
    pats = await _visible_pats(ws_id, db, user_id=_current_user(request))
    _tools = all_tools()
    try:
        default_agent_tool = get_tool(settings.default_agent_tool).name
    except ValueError:
        default_agent_tool = "opencode"
    model_options = await _get_model_options(ws_id, db, default_agent_tool)
    _avail = await asyncio.gather(
        *[k8s.get_image_available(t.get_image(), ws.k8s_namespace) for t in _tools]
    )
    from swarmer.routers.mcp_servers import get_enabled_mcp_servers
    mcp_servers = await get_enabled_mcp_servers(ws_id, db, user_id=_current_user(request))
    prompt_sources = await _get_prompt_sources(ws_id, db)
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
            "mcp_servers": mcp_servers,
            "prompt_sources": prompt_sources,
        },
    )


@router.post("/workspaces/{ws_id}/sessions", dependencies=[Depends(require_auth)])
async def session_create(
    ws_id: int,
    request: Request,
    name: str = Form(...),
    github_pat_id: str = Form(""),
    prompt_id: str = Form(""),
    instruction_prompt: str = Form(""),
    persist: bool = Form(False),
    mode: str = Form("prompt"),
    model: str = Form(""),
    agent_tool: str = Form("opencode"),
    working_branch: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    pat_id = int(github_pat_id) if github_pat_id else None
    pid = None
    if prompt_id:
        try:
            pid = int(prompt_id)
            # Verify prompt ownership
            from swarmer.models.workspace_prompt import WorkspacePrompt, WorkspacePromptSource
            prompt = await db.get(WorkspacePrompt, pid)
            if not prompt:
                flash(request, "Selected prompt not found.", "danger")
                return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/new", status_code=302)

            # WorkspacePrompt -> WorkspacePromptSource -> Workspace
            # We need to load the source to check workspace_id
            result = await db.execute(
                select(WorkspacePromptSource).where(WorkspacePromptSource.id == prompt.source_id)
            )
            source = result.scalar_one_or_none()
            if not source or source.workspace_id != ws_id:
                flash(request, "Selected prompt does not belong to this workspace.", "danger")
                return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/new", status_code=302)
        except ValueError:
            flash(request, "Invalid prompt selection.", "danger")
            return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/new", status_code=302)

    if mode not in ("tui", "server", "prompt"):
        mode = "prompt"

    try:
        agent_tool = get_tool(agent_tool).name
    except ValueError:
        agent_tool = "opencode"

    if not model.strip():
        opts = await _get_model_options(ws_id, db, agent_tool)
        model = opts[0]["value"] if opts else ""

    wb = working_branch.strip()
    if wb and not _is_valid_ref_name(wb):
        flash(request, "Invalid working branch name.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/new", status_code=302)

    session = Session(
        workspace_id=ws_id,
        github_pat_id=pat_id,
        prompt_id=pid,
        name=name.strip(),
        mode=mode,
        model=model.strip(),
        persist=persist,
        instruction_prompt=instruction_prompt.strip(),
        agent_tool=agent_tool,
        working_branch=wb,
    )
    # Gather MCP server checkbox selections from the multi-value form field
    form_data = await request.form()
    selected_mcp_ids = [int(v) for v in form_data.getlist("mcp_server_ids") if str(v).isdigit()]
    if selected_mcp_ids:
        session.enabled_mcp_ids = selected_mcp_ids
    else:
        session.mcp_server_ids = "none"
    db.add(session)
    try:
        await db.commit()
        await db.refresh(session)
        if not session.working_branch:
            import secrets as _secrets
            session.working_branch = f"swarmer/session-{session.id}-{_secrets.token_hex(4)}"
            await db.commit()
    except IntegrityError:
        await db.rollback()
        pats = await _visible_pats(ws_id, db, user_id=_current_user(request))
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
        options=[
            selectinload(Session.github_pat),
            selectinload(Session.repos),
            selectinload(Session.prompt),
        ],
    )
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    pats = await _visible_pats(ws_id, db, user_id=_current_user(request))

    # Fetch live K8s detail for the initial page render and sync phase
    status_detail = ""
    if session.pod_name:
        live_phase, status_detail = await asyncio.to_thread(k8s.get_pod_status, session.pod_name, ws.k8s_namespace)
        if live_phase != session.phase:
            session.phase = live_phase
            await db.commit()

    # Generate one-time TUI token after K8s sync so phase is current
    tui_token = None
    if session.mode == "tui" and session.phase == "running":
        tui_token = str(uuid.uuid4())
        tokens = list(request.session.get("tui_tokens", []))
        tokens.append(tui_token)
        request.session["tui_tokens"] = tokens

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
    from swarmer.routers.mcp_servers import get_enabled_mcp_servers
    mcp_servers = await get_enabled_mcp_servers(ws_id, db, user_id=_current_user(request))
    prompt_sources = await _get_prompt_sources(ws_id, db)
    queue_position = None
    if session.phase == "queued":
        queue_position = await _get_queue_position(session.id, db)
    capacity = await _get_capacity_summary(ws_id, db)
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
            "queue_position": queue_position,
            "capacity": capacity,
            "model_options": model_options,
            "repo_info": repo_info,
            "agent_tools": _tools,
            "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
            "patch_filename": _patch_filename(session),
            "cron_presets": CRON_PRESETS,
            "mcp_servers": mcp_servers,
            "prompt_sources": prompt_sources,
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
    prompt_id: str = Form(""),
    instruction_prompt: str = Form(""),
    persist: bool = Form(False),
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

    if prompt_id:
        try:
            pid = int(prompt_id)
            from swarmer.models.workspace_prompt import WorkspacePrompt, WorkspacePromptSource
            prompt = await db.get(WorkspacePrompt, pid)
            if not prompt:
                flash(request, "Selected prompt not found.", "danger")
                return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

            result = await db.execute(
                select(WorkspacePromptSource).where(WorkspacePromptSource.id == prompt.source_id)
            )
            source = result.scalar_one_or_none()
            if not source or source.workspace_id != ws_id:
                flash(request, "Selected prompt does not belong to this workspace.", "danger")
                return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)
            session.prompt_id = pid
        except ValueError:
            flash(request, "Invalid prompt selection.", "danger")
            return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)
    else:
        session.prompt_id = None

    session.instruction_prompt = instruction_prompt.strip()
    session.persist = persist
    if mode in ("tui", "server", "prompt"):
        session.mode = mode
    session.model = model.strip()
    try:
        session.agent_tool = get_tool(agent_tool).name
    except ValueError:
        pass

    form_data = await request.form()
    if "working_branch" in form_data:
        branch_val = form_data["working_branch"].strip()
        if branch_val and not _is_valid_ref_name(branch_val):
            flash(request, "Invalid working branch name.", "danger")
            return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)
        session.working_branch = branch_val

    selected_mcp_ids = [int(v) for v in form_data.getlist("mcp_server_ids") if str(v).isdigit()]
    if selected_mcp_ids:
        session.enabled_mcp_ids = selected_mcp_ids
    else:
        session.mcp_server_ids = "none"

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        flash(request, f"A session named '{name}' already exists in this workspace.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    flash(request, "Session updated.", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Launch / Stop
# ============================================================

async def _resolve_session_prompt(session: Session, db: AsyncSession) -> str:
    """Resolve the session prompt using layered composition.
    
    Layers:
    1. Additional Instructions (session.instruction_prompt)
    2. Git-referenced prompt content (session.prompt_id)
    """
    base_prompt = ""
    if session.prompt_id:
        p = await db.get(WorkspacePrompt, session.prompt_id)
        if p:
            base_prompt = p.content

    additional = session.instruction_prompt.strip() if session.instruction_prompt else ""
    if additional and base_prompt:
        resolved_prompt = additional + "\n\n" + base_prompt
    elif additional:
        resolved_prompt = additional
    else:
        resolved_prompt = base_prompt
    return resolved_prompt


async def _count_running_sessions(db: AsyncSession) -> int:
    """Count sessions globally with pods: pending or running only."""
    result = await db.execute(
        select(func.count()).select_from(Session).where(
            Session.phase.in_(["pending", "running"])
        )
    )
    return result.scalar_one()


async def _get_queue_position(session_id: int, db: AsyncSession) -> tuple[int, int]:
    """Return (1-based position, total queued) for a queued session, global FIFO by created_at."""
    result = await db.execute(
        select(Session.id, Session.created_at)
        .where(Session.phase == "queued")
        .order_by(Session.created_at)
    )
    rows = result.all()
    total = len(rows)
    for i, (sid, _) in enumerate(rows):
        if sid == session_id:
            return i + 1, total
    return 0, total


async def _get_capacity_summary(workspace_id: int, db: AsyncSession) -> dict:
    """Return workspace-scoped capacity info for the sessions list display."""
    global_running = await _count_running_sessions(db)

    ws_active_result = await db.execute(
        select(func.count()).select_from(Session).where(
            Session.workspace_id == workspace_id,
            Session.phase.in_(["pending", "running"]),
        )
    )
    ws_active = ws_active_result.scalar_one()

    ws_queued_result = await db.execute(
        select(func.count()).select_from(Session).where(
            Session.workspace_id == workspace_id,
            Session.phase == "queued",
        )
    )
    ws_queued = ws_queued_result.scalar_one()

    max_agents = settings.max_concurrent_agents
    if max_agents <= 0:
        slots_available = None
    else:
        slots_available = max(0, max_agents - global_running)

    return {
        "ws_active": ws_active,
        "slots_available": slots_available,
        "ws_queued": ws_queued,
        "max": max_agents,
    }


def _build_expected_hosts(model: str, repos_data: list[dict], tool_name: str, mode: str) -> set[str]:
    """Return the set of hostnames this session is expected to reach.

    Only draft policy chunks matching these hosts will be auto-approved.
    Anything outside this set is left pending for human review.
    """
    hosts: set[str] = set()

    # AI provider endpoints based on model prefix
    provider = model.split("/")[0] if "/" in model else ""
    if provider in ("google", "google-vertex-anthropic"):
        hosts.add("generativelanguage.googleapis.com")
        hosts.add("*.aiplatform.googleapis.com")
        hosts.add("oauth2.googleapis.com")
    if provider in ("google-vertex-anthropic", "vertexai"):
        hosts.add("*.aiplatform.googleapis.com")
    if provider == "anthropic":
        hosts.add("api.anthropic.com")
    if provider in ("gemini",):
        hosts.add("generativelanguage.googleapis.com")
    if provider == "openai":
        hosts.add("api.openai.com")

    # OpenCode fetches model metadata from models.dev
    if tool_name == "opencode":
        hosts.add("models.dev")
        hosts.add("opencode.ai")

    # GitHub access for each attached repo
    if repos_data:
        hosts.add("github.com")
        hosts.add("api.github.com")
        # Raw content for public repos
        hosts.add("raw.githubusercontent.com")
        hosts.add("objects.githubusercontent.com")

    return hosts


def _extract_vertex_model(model: str) -> str:
    """Extract the bare model ID from a google-vertex-anthropic model string.

    "google-vertex-anthropic/claude-sonnet-4-6@default" → "claude-sonnet-4-6"
    Falls back to the raw model string if it doesn't match the expected format.
    """
    if "/" in model:
        model = model.split("/", 1)[1]
    if "@" in model:
        model = model.split("@", 1)[0]
    return model


async def _wait_vertex_provider_ready(provider_name: str, timeout: float = 30.0) -> None:
    """Wait for the gateway to complete the initial ADC token refresh for a Vertex AI provider.

    The gateway refreshes the token asynchronously after configure_vertex_provider.
    Creating the sandbox before the first refresh means the provider may not be able
    to intercept aiplatform.googleapis.com calls with a valid token yet.
    """
    from swarmer import openshell_client
    from openshell._proto import openshell_pb2
    import time as _time

    client = openshell_client._get_client()

    def _poll():
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                sr = client._stub.GetProviderRefreshStatus(
                    openshell_pb2.GetProviderRefreshStatusRequest(provider=provider_name),
                    timeout=5,
                )
                statuses = {c.credential_key: c.status for c in sr.credentials}
                if statuses.get("gcloud_adc_token") == "configured":
                    return
            except Exception:
                pass
            _time.sleep(1)
        log.warning("_wait_vertex_provider_ready: timed out after %.0fs for %s", timeout, provider_name)

    await asyncio.to_thread(_poll)


async def _do_launch(session: Session, ws: Workspace, db: AsyncSession, user_id: str = "") -> None:
    """Core launch logic shared by the HTTP endpoint and the background scheduler."""
    if user_id == "unknown":
        raise ValueError("Session expired — please log in again")

    if settings.max_concurrent_agents > 0:
        running = await _count_running_sessions(db)
        if running >= settings.max_concurrent_agents:
            session.phase = "queued"
            session.status_detail = f"Waiting for capacity ({running}/{settings.max_concurrent_agents} active)"
            await db.commit()
            return

    import secrets as _secrets
    suffix = _secrets.token_hex(4)

    from swarmer.models.opencode_secret import OpencodeSecret
    _user_filter = [OpencodeSecret.workspace_id == session.workspace_id]
    if user_id:
        _user_filter.append(
            or_(
                OpencodeSecret.user_id == user_id,
                OpencodeSecret.shared == True,  # noqa: E712
                OpencodeSecret.user_id == "",
            )
        )
    oc_result = await db.execute(
        select(OpencodeSecret).where(*_user_filter)
    )
    _oc_all = oc_result.scalars().all()
    oc_secret = None
    if user_id:
        for s in _oc_all:
            if s.user_id == user_id:
                oc_secret = s
                break
    if oc_secret is None and _oc_all:
        oc_secret = _oc_all[0]
    has_adc = oc_secret.has_adc if oc_secret else False
    has_gemini = bool(oc_secret and oc_secret.google_api_key_enc)

    # Fetch enabled & authenticated MCP servers for this workspace
    from swarmer.routers.mcp_servers import get_enabled_mcp_servers
    all_ws_mcp = await get_enabled_mcp_servers(session.workspace_id, db, user_id=user_id)
    ws_mcp_servers = [s for s in all_ws_mcp if s.auth_status != "expired"]

    # Filter to only the MCP servers enabled for this specific session
    # mcp_servers=None  → no MCP configured in workspace (skip override)
    # mcp_servers=[]    → user explicitly disabled all (override with clean config)
    # mcp_servers=[...] → user selected specific servers
    if not all_ws_mcp:
        mcp_servers = None
    elif session.mcp_server_ids == "none":
        mcp_servers = []
    elif session.enabled_mcp_ids:
        enabled_ids = set(session.enabled_mcp_ids)
        mcp_servers = [s for s in ws_mcp_servers if s.id in enabled_ids]
    else:
        # No MCP selection stored — default to all workspace-enabled servers
        mcp_servers = ws_mcp_servers

    # Resolve prompt using layered composition
    resolved_prompt = await _resolve_session_prompt(session, db)

    await _do_launch_openshell(
        session=session,
        ws=ws,
        db=db,
        suffix=suffix,
        oc_secret=oc_secret,
        has_adc=has_adc,
        has_gemini=has_gemini,
        mcp_servers=mcp_servers,
        resolved_prompt=resolved_prompt,
    )


async def _do_launch_openshell(
    session: Session,
    ws: Workspace,
    db: AsyncSession,
    suffix: str,
    oc_secret,
    has_adc: bool,
    has_gemini: bool,
    mcp_servers: list | None,
    resolved_prompt: str,
) -> None:
    """Launch a session via the OpenShell sandbox API."""
    from swarmer import openshell_client
    from swarmer.openshell_policy import build_session_policy

    tool = get_tool(session.agent_tool)

    # Release the DB connection before long-running gRPC operations. The route
    # handler's session holds an autobegin transaction from earlier SELECTs in
    # _do_launch(); committing here ends that transaction and returns the
    # connection to the pool so the scheduler can write without being blocked.
    # session attributes remain valid because expire_on_commit=False.
    await db.commit()

    # 1. Collect MCP env vars (non-credential; AI creds go through provider API)
    env_vars = await openshell_client.create_provider(
        session=session,
        workspace_secret=oc_secret,
        github_pat=session.github_pat,
        mcp_servers=mcp_servers or [],
    )

    # 1b. Create/update gateway providers for each available credential.
    #     Must happen BEFORE sandbox creation: provider names go into SandboxSpec.providers
    #     so the supervisor can call GetSandboxProviderEnvironment at startup and receive
    #     the injected env vars (GOOGLE_API_KEY, ANTHROPIC_API_KEY, GH_TOKEN, etc.).
    provider_names: list[str] = []
    inference_env: dict[str, str] = {}
    ws_id = session.workspace_id
    if oc_secret and oc_secret.anthropic_api_key:
        pname = f"swarmer-ws-{ws_id}-claude-code"
        await openshell_client.ensure_provider(pname, "claude-code", {}, credentials={"api_key": oc_secret.anthropic_api_key})
        provider_names.append(pname)
    if oc_secret and oc_secret.google_api_key:
        pname = f"swarmer-ws-{ws_id}-google-ai-studio"
        await openshell_client.ensure_provider(pname, "google-ai-studio", {}, credentials={
                "GOOGLE_API_KEY": oc_secret.google_api_key,
                "GOOGLE_GENERATIVE_AI_API_KEY": oc_secret.google_api_key,
            })
        provider_names.append(pname)
    # Vertex AI (ADC) provider registration kept for future inference.local support.
    # inference.local routing is not active; Vertex AI sessions are not supported yet.
    if oc_secret and oc_secret.has_adc and oc_secret.has_vertex:
        pname = f"swarmer-ws-{ws_id}-google-vertex-ai"
        _project = oc_secret.google_cloud_project or ""
        _location = oc_secret.vertex_location or ""
        try:
            await openshell_client.ensure_provider(
                pname, "google-vertex-ai",
                config={"VERTEX_AI_PROJECT_ID": _project, "VERTEX_AI_REGION": _location},
                credentials={"gcloud_adc_token": "__placeholder__"},
            )
            await openshell_client.configure_vertex_provider(
                pname,
                adc_json=oc_secret.application_default_credentials,
                project=_project,
                location=_location,
            )
            provider_names.append(pname)
        except Exception:
            log.warning("Vertex AI provider setup failed (non-fatal)", exc_info=True)
    _has_github_repos = any("github.com" in (r.repo_url or "") for r in (session.repos or []))
    if session.github_pat or _has_github_repos:
        pname = f"swarmer-ws-{ws_id}-github"
        if session.github_pat:
            pat_token = getattr(session.github_pat, "token", None) or getattr(session.github_pat, "pat", "")
            _creds: dict[str, str] = {"api_token": pat_token}
        else:
            # Gateway requires non-empty credentials; use a placeholder so the provider
            # is registered and the sandbox gets network access to github.com for public clones.
            _creds = {"api_token": "public-repo-access"}
        await openshell_client.ensure_provider(pname, "github", {}, credentials=_creds)
        provider_names.append(pname)

    # 2. Build policy YAML (pure computation, no I/O)
    policy = build_session_policy(
        session=session,
        repos=list(session.repos or []),
        mcp_servers=list(mcp_servers or []),
        agent_tool=session.agent_tool,
        model=session.model or "",
    )

    # Resolve model before committing so main_cmd can be built later
    if session.model and tool.is_valid_model(session.model):
        model = session.model
    else:
        model = tool.get_default_model(has_adc, has_gemini)
    model = model.strip("\r\n")  # strip any stray line endings before embedding in shell commands

    # When routing through inference.local, rewrite the model to use the plain
    # anthropic/ provider. OpenCode's google-vertex-anthropic provider invokes

    # Capture serialisable data for the background task before committing.
    # ORM objects cannot be used across DB sessions.
    import json as _json
    mcp_patch: dict = {}
    if mcp_servers:
        config_data = tool.build_config_data(secret=oc_secret, mcp_servers=mcp_servers)
        config_json = config_data.get(f"{tool.name}.json", "{}")
        try:
            mcp_patch = _json.loads(config_json).get("mcp", {})
        except Exception:
            pass

    repos_data = [
        {"url": r.repo_url, "local_path": r.local_path, "branch": r.branch}
        for r in (session.repos or [])
    ]
    git_username = (session.github_pat.github_username or "") if session.github_pat else ""
    pat_token = (session.github_pat.pat or "") if session.github_pat else ""
    agents_md = ""
    if session.mode in ("tui", "server"):
        repo_context = k8s_sess._build_repo_context(list(session.repos or []), base_path="/sandbox")
        agents_md = (resolved_prompt or "") + repo_context
    # main_cmd is used for tui/server modes. For prompt mode, _setup_openshell_sandbox
    # builds the command from scratch to write the prompt via stdin (no newlines in args).
    main_cmd = tool.build_main_cmd(session, model, resolved_prompt=resolved_prompt)
    resolved_prompt_safe = resolved_prompt or ""
    model_setup_cmd = tool.build_model_setup_cmd(model).replace("/workspace/", "/sandbox/")
    share_cmd = tool.build_share_setup_cmd().replace("/workspace/", "/sandbox/")

    # Mark pending and commit — HTTP handler returns immediately; browser unblocks.
    session.phase = "pending"
    session.last_output = ""
    session.status_detail = ""   # clear stale status from any previous run
    session.run_started_at = datetime.utcnow()
    session.run_completed_at = None
    await db.commit()

    # All slow gRPC work (create_sandbox wait_ready, exec setup) runs in the background.
    asyncio.create_task(
        _setup_openshell_sandbox(
            session_id=session.id,
            provider_names=provider_names,
            env_vars=env_vars,
            policy=policy,
            image=tool.get_image(),
            tool_name=tool.name,
            model=model,
            model_setup_cmd=model_setup_cmd,
            share_cmd=share_cmd,
            mcp_patch=mcp_patch,
            repos_data=repos_data,
            git_username=git_username,
            pat_token=pat_token,
            working_branch=session.working_branch or "",
            agents_md=agents_md,
            mode=session.mode,
            main_cmd=main_cmd,
            resolved_prompt=resolved_prompt_safe,
            inference_env=inference_env,
        ),
        name=f"openshell-setup-{session.id}",
    )


async def _setup_openshell_sandbox(
    session_id: int,
    provider_names: list[str],
    env_vars: dict,
    policy,
    image: str,
    tool_name: str,
    model: str,
    model_setup_cmd: str,
    share_cmd: str,
    mcp_patch: dict,
    repos_data: list[dict],
    git_username: str,
    pat_token: str,
    working_branch: str,
    agents_md: str,
    mode: str,
    main_cmd: str,
    resolved_prompt: str = "",
    inference_env: dict | None = None,
) -> None:
    """Background task: create sandbox and run all setup steps, then launch agent."""
    from swarmer import openshell_client
    from swarmer.database import get_db as _get_db
    from swarmer.models.session import Session as _Session

    async def _update_db(**fields) -> None:
        async for _db in _get_db():
            _s = await _db.get(_Session, session_id)
            if _s:
                for k, v in fields.items():
                    setattr(_s, k, v)
                await _db.commit()
            break

    try:
        # Create sandbox (slow — wait_ready can take 30-120 s)
        ref = await openshell_client.create_sandbox(
            image=image,
            env_vars=env_vars,
            policy=policy,
            provider_names=provider_names,
        )
        await _update_db(sandbox_name=ref.name)

        # Check if the session was stopped while sandbox was being created (race condition).
        # If so, delete the sandbox and exit cleanly rather than starting the agent.
        async def _session_phase() -> str:
            async for _db in _get_db():
                _s = await _db.get(_Session, session_id)
                return _s.phase if _s else "unknown"
            return "unknown"

        current_phase = await _session_phase()
        if current_phase != "pending":
            log.info("_setup_openshell_sandbox: session %d no longer pending (phase=%s), cleaning up sandbox %s",
                     session_id, current_phase, ref.name)
            try:
                await openshell_client.delete_sandbox(ref.name)
            except Exception:
                pass
            return

        # Write a schema-valid tool config to /sandbox/{tool}.json.
        # The container image ships an opencode.json with an outdated LSP schema
        # (missing required 'extensions' field). Always overwrite it with a valid
        # config that includes enabled_providers and any MCP config.
        from swarmer.agent_tools.registry import get as _get_tool
        _tool = _get_tool(tool_name)
        _is_inference_local = bool(inference_env and inference_env.get("ANTHROPIC_BASE_URL"))
        _mcp_list = [
            type("_MCP", (), {"slug": k, **v})()
            for k, v in (mcp_patch.get("mcp", {}) or {}).items()
        ] if mcp_patch else []
        _config_data = _tool.build_config_data(
            mcp_servers=_mcp_list,
            use_inference_local=_is_inference_local,
        )
        _config_json = _config_data.get(f"{tool_name}.json", "{}")
        await openshell_client.write_agent_config(
            sandbox_name=ref.name,
            tool_name=tool_name,
            config_json=_config_json,
        )

        # For github.com repos, probe connectivity first so the supervisor generates
        # a draft network policy for github.com, which we immediately approve.
        # This must happen BEFORE the real clone — the sandbox starts with no
        # outbound policy (draft-approval workflow) and the probe forces a deny-event
        # that the supervisor converts into an approvable chunk.
        _github_repos = [rd for rd in repos_data if "github.com" in rd["url"]]
        if _github_repos:
            await _update_db(status_detail="Probing GitHub connectivity…")
            try:
                await openshell_client.exec_command(
                    ref.name,
                    ["sh", "-c", "curl -s -o /dev/null -m 10 https://github.com; true"],
                    client=None,
                    timeout_seconds=15,
                )
            except Exception:
                pass
            await asyncio.sleep(12)  # supervisor needs ~10s to analyze the denied connection
            _git_hosts: set[str] = {
                "github.com",
                "api.github.com",
                "raw.githubusercontent.com",
                "objects.githubusercontent.com",
            }
            await openshell_client.approve_draft_policy_chunks(ref.name, expected_hosts=_git_hosts)

        # Clone repos via exec_command (one call per repo, exit code checked).
        # PAT is embedded directly in the clone URL so auth is self-contained
        # and does not depend on $GH_TOKEN gateway injection.
        if repos_data:
            for rd in repos_data:
                local_path = rd["local_path"]
                repo_url = rd["url"]
                if pat_token and git_username and "github.com" in repo_url:
                    auth_url = repo_url.replace("https://", f"https://{git_username}:{pat_token}@")
                else:
                    auth_url = repo_url
                clone_cmd = f"cd /sandbox && git clone {shlex.quote(auth_url)} {shlex.quote(local_path)}"
                result = await openshell_client.exec_command(
                    ref.name, ["sh", "-c", clone_cmd], client=None
                )
                if getattr(result, "exit_code", 0) != 0:
                    log.warning(
                        "sandbox setup: git clone failed for %s (exit %s): %s",
                        local_path,
                        getattr(result, "exit_code", "?"),
                        getattr(result, "stderr", ""),
                    )
            await openshell_client.exec_command(
                ref.name, ["sh", "-c", "git config --global --add safe.directory '*'"], client=None
            )
            if working_branch:
                for rd in repos_data:
                    branch_cmd = (
                        f"cd /sandbox/{rd['local_path']} && "
                        f"git checkout -b {shlex.quote(working_branch)} 2>/dev/null "
                        f"|| git checkout {shlex.quote(working_branch)}"
                    )
                    await openshell_client.exec_command(ref.name, ["sh", "-c", branch_cmd], client=None)

        # Share/state dir setup runs FIRST so the $HOME/.local/share/<tool> symlink
        # is in place before model_setup_cmd writes the model config through it.
        # Both commands need HOME=/sandbox to match the agent's runtime environment.
        if share_cmd.strip():
            clean_share = share_cmd.rstrip().rstrip(";").rstrip()
            await openshell_client.exec_command(
                ref.name, ["sh", "-c", f"export HOME=/sandbox; {clean_share}"], client=None
            )

        # Model selection config
        if model_setup_cmd.strip():
            clean_cmd = model_setup_cmd.rstrip().rstrip("&").rstrip()
            await openshell_client.exec_command(
                ref.name, ["sh", "-c", f"export HOME=/sandbox; {clean_cmd}"], client=None
            )

        # AGENTS.md for tui/server modes
        if agents_md:
            await openshell_client.write_agents_md(sandbox_name=ref.name, content=agents_md)

        # Pre-flight probe: run a quick opencode command so the supervisor observes
        # the denied AI API connections and generates draft policy chunks.
        # Then approve expected chunks so the actual agent run can reach the API.
        # Without this probe, approval runs before any chunks exist.
        import asyncio as _asyncio
        await _update_db(status_detail="Configuring network policy…")
        _probe_prompt = shlex.quote("Reply with one word: ready")
        _probe_bin = {"opencode": "opencode run", "crush": "crush run"}.get(tool_name, "opencode run")
        _inf_prefix = (" ".join(f"{k}={shlex.quote(v)}" for k, v in (inference_env or {}).items()) + " ") if inference_env else ""
        _probe_cmd = f"{_inf_prefix}HOME=/sandbox {_probe_bin} --model {shlex.quote(model)} {_probe_prompt} 2>/dev/null; true"
        try:
            await openshell_client.exec_command(
                ref.name, ["sh", "-c", _probe_cmd], client=None,
                timeout_seconds=30,
            )
        except Exception:
            pass  # probe failure is non-fatal; approval may still find existing chunks
        await _asyncio.sleep(12)  # supervisor needs ~10s to submit denial analysis

        expected = _build_expected_hosts(model, repos_data, tool_name, mode)
        await openshell_client.approve_draft_policy_chunks(ref.name, expected_hosts=expected)

        # For prompt mode, write the prompt to /sandbox/.prompt.txt via stdin
        # so we avoid newline-in-arg gateway rejection. Build the agent command
        # from scratch (not from pre-built main_cmd which may embed newlines).
        if mode == "prompt" and resolved_prompt:
            await openshell_client.exec_command(
                ref.name, ["sh", "-c", "cat > /sandbox/.prompt.txt"],
                client=None, stdin=resolved_prompt.encode(),
            )
            # Base command without any embedded prompt — shell reads from file
            _tool_bin = {"opencode": "opencode run", "crush": "crush run"}.get(tool_name, "opencode run")
            _model_arg = shlex.quote(model) if model else ""
            agent_cmd = f"HOME=/sandbox {_tool_bin} --model {_model_arg} \"$(</sandbox/.prompt.txt)\""
        elif mode == "prompt" and not resolved_prompt:
            # No prompt at all — just launch without message
            agent_cmd = f"HOME=/sandbox {main_cmd}"
        else:
            agent_cmd = f"HOME=/sandbox {main_cmd}"

        # Prepend inference proxy env vars when OpenShell routes VertexAI through inference.local.
        if inference_env:
            _env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in inference_env.items())
            agent_cmd = f"{_env_prefix} {agent_cmd}"

        # Launch agent
        asyncio.create_task(
            _run_openshell_agent(
                session_id=session_id,
                sandbox_name=ref.name,
                cmd=["sh", "-c", agent_cmd],
                mode=mode,
                agent_tool=tool_name,
            ),
            name=f"openshell-agent-{session_id}",
        )

    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("_setup_openshell_sandbox failed for session %d", session_id)
        await _update_db(phase="failed", run_completed_at=datetime.utcnow())


async def _run_openshell_agent(
    session_id: int,
    sandbox_name: str,
    cmd: list[str],
    mode: str,
    agent_tool: str,
) -> None:
    """Background task: starts the agent in the sandbox and tracks completion."""
    from swarmer import openshell_client
    from swarmer.database import get_db as _get_db
    from swarmer.models.session import Session as _Session

    async def _update_db(**fields) -> None:
        async for _db in _get_db():
            _s = await _db.get(_Session, session_id)
            if _s:
                for k, v in fields.items():
                    setattr(_s, k, v)
                await _db.commit()
            break

    try:
        if mode != "server":
            # Server mode stays "pending" until expose_service succeeds and the
            # service_url is stored — the Chat tab only becomes accessible then.
            await _update_db(phase="running")

        if mode == "prompt":
            result = await openshell_client.exec_command(sandbox_name, cmd, client=None,
                                                         timeout_seconds=300)
            exit_code = getattr(result, "exit_code", None)
            stdout = getattr(result, "stdout", "") or ""
            stderr = getattr(result, "stderr", "") or ""
            phase = "succeeded" if exit_code == 0 else "failed"

            # OpenCode stores the response in its SQLite DB, not stdout.
            # Extract the last assistant message after the run completes.
            if agent_tool == "opencode":
                output = await openshell_client.read_opencode_response(sandbox_name) or stdout or stderr
            else:
                output = stdout or stderr

            new_sandbox_name: str | None = sandbox_name
            if phase == "succeeded":
                try:
                    await openshell_client.delete_sandbox(sandbox_name)
                    new_sandbox_name = None
                except Exception:
                    log.warning("Auto-cleanup of sandbox %s failed", sandbox_name, exc_info=True)

            await _update_db(
                phase=phase,
                last_output=output,
                status_detail="",  # clear any stale status from previous runs
                run_completed_at=datetime.utcnow(),
                sandbox_name=new_sandbox_name,
            )
        else:
            if mode == "server":
                # Launch server agent as a background nohup process so exec() returns
                # immediately; the sandbox stays alive serving HTTP.
                await openshell_client.start_agent(sandbox_name, cmd)

            # TUI mode: the sandbox is ready; the TUI WebSocket handler starts the
            # agent interactively via exec_interactive when the user connects.

            # For server mode, expose the agent HTTP port so the chat proxy can reach it
            if mode == "server":
                from swarmer.agent_tools.registry import get as _get_tool
                _tool = _get_tool(agent_tool)
                port = _tool.get_server_port() or 4096
                try:
                    await _update_db(status_detail="Waiting for server to start…")
                    await asyncio.sleep(8)  # let the server process start listening
                    service_url = await openshell_client.expose_service(
                        sandbox_name, "agent", port
                    )
                    # Transition to running and store the URL atomically so the
                    # Chat tab is only accessible once it's reachable.
                    await _update_db(phase="running", service_url=service_url, status_detail="")
                except Exception:
                    log.exception(
                        "ExposeService failed for session %d sandbox %s",
                        session_id, sandbox_name,
                    )
                    await _update_db(
                        phase="failed",
                        status_detail="Failed to expose service URL — check server logs",
                        run_completed_at=datetime.utcnow(),
                    )

    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("_run_openshell_agent failed for session %d", session_id)
        await _update_db(
            phase="failed",
            status_detail="OpenShell agent startup failed",
            run_completed_at=datetime.utcnow(),
        )


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
    prompt_id: str = Form(""),
    instruction_prompt: str = Form(""),
    persist: bool = Form(False),
    mode: str = Form(""),
    model: str = Form(""),
    redirect_to: str = Form(""),
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
        # Persist prompt_id selection
        if prompt_id:
            try:
                pid = int(prompt_id)
                p = await db.get(WorkspacePrompt, pid)
                if p:
                    result = await db.execute(
                        select(WorkspacePromptSource).where(WorkspacePromptSource.id == p.source_id)
                    )
                    source = result.scalar_one_or_none()
                    if source and source.workspace_id == ws_id:
                        session.prompt_id = pid
                    else:
                        session.prompt_id = None
                else:
                    session.prompt_id = None
            except ValueError:
                session.prompt_id = None
        else:
            session.prompt_id = None
        session.instruction_prompt = instruction_prompt.strip()
        session.persist = persist
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

    try:
        await _do_launch(session, ws, db, user_id=request.session.get("username", ""))
        if session.phase == "queued":
            flash(request, f"Session queued — {session.status_detail}", "info")
    except Exception as exc:
        log.error("session_launch failed for session %d: %s", sid, exc, exc_info=True)
        flash(request, f"Launch failed: {exc}", "danger")

    dest = (
        f"/workspaces/{ws_id}/sessions"
        if redirect_to == "list"
        else f"/workspaces/{ws_id}/sessions/{sid}"
    )
    return RedirectResponse(url=dest, status_code=302)


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

    if session.phase == "queued":
        session.phase = "idle"
        session.status_detail = ""
        await db.commit()
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    if session.sandbox_name:
        from swarmer import openshell_client
        if session.service_url:
            try:
                await openshell_client.delete_service(session.sandbox_name, "agent")
            except Exception as exc:
                log.warning("DeleteService failed for session %d: %s", sid, exc)
        try:
            await openshell_client.delete_sandbox(session.sandbox_name)
        except Exception as exc:
            flash(request, f"Sandbox deletion failed: {exc}", "warning")
        session.sandbox_name = None
        session.service_url = None

    session.run_completed_at = datetime.utcnow()
    session.phase = "stopped"
    session.pod_name = None
    await db.commit()
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Schedule / Unschedule
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/schedule",
    dependencies=[Depends(require_auth)],
)
async def session_schedule(
    ws_id: int,
    sid: int,
    request: Request,
    cron_expr: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    from croniter import croniter

    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    cron_expr = cron_expr.strip()
    if not cron_expr:
        flash(request, "Cron expression is required.", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#schedule", status_code=302)

    if len(cron_expr) > 128:
        flash(request, "Cron expression is too long (max 128 characters).", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#schedule", status_code=302)

    if session.mode != "prompt":
        flash(request, "Scheduling is only supported for prompt-mode sessions.", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#schedule", status_code=302)

    if not croniter.is_valid(cron_expr):
        flash(request, f"Invalid cron expression: {cron_expr}", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#schedule", status_code=302)

    session.cron_schedule = cron_expr
    session.cron_next_run = croniter(cron_expr, datetime.utcnow()).get_next(datetime)
    await db.commit()

    flash(request, f"Schedule set: {session.cron_label or cron_expr}. Next run: {session.cron_next_run.strftime('%b %d %H:%M UTC')}", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#schedule", status_code=302)


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/unschedule",
    dependencies=[Depends(require_auth)],
)
async def session_unschedule(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    session.cron_schedule = ""
    session.cron_next_run = None

    # Clean up CronJob-persistent secrets when schedule is removed
    if session.k8s_secret_names:
        try:
            k8s.cleanup_session_secrets(ws.k8s_namespace, session)
        except Exception:
            log.warning("Failed to clean up cron secrets for session %d", sid, exc_info=True)

    await db.commit()

    flash(request, "Schedule cancelled.", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#schedule", status_code=302)


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
    queue_position = None

    if session.phase == "queued":
        queue_position = await _get_queue_position(session.id, db)
    elif session.pod_name and session.phase in ("pending", "running"):
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
            "queue_position": queue_position,
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

    if session.sandbox_name:
        # OpenShell session — delete sandbox; skip K8s PVC/Secret cleanup
        from swarmer import openshell_client
        if session.service_url:
            try:
                await openshell_client.delete_service(session.sandbox_name, "agent")
            except Exception as exc:
                log.warning("DeleteService failed for session %d: %s", sid, exc)
        try:
            await openshell_client.delete_sandbox(session.sandbox_name)
        except Exception as exc:
            flash(request, f"Sandbox deletion failed: {exc}", "warning")
    else:
        # K8s session — clean up PVC and Secrets if present
        if session.pvc_name:
            try:
                k8s_sess.delete_session_pvc(ws.k8s_namespace, session.pvc_name)
            except Exception as exc:
                flash(request, f"PVC deletion failed: {exc}", "warning")

        if session.k8s_secret_names:
            try:
                k8s.cleanup_session_secrets(ws.k8s_namespace, session)
            except Exception as exc:
                flash(request, f"Secret cleanup failed: {exc}", "warning")

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
    if session.is_active:
        return HTMLResponse("", status_code=409)

    try:
        validate_github_url(repo_url.strip())
    except GitHubURLError as exc:
        log.warning(
            "repo_add: rejected URL with embedded token for session %s: %s",
            sid,
            exc.redacted_url,
        )
        flash(request, f"Repository URL rejected: {exc.reason}", "danger")
        return HTMLResponse("", status_code=422, headers={"HX-Trigger": "repoAddError"})

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

    return HTMLResponse("", headers={"HX-Trigger": "repoListChanged"})


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
    session_check = await db.get(Session, sid)
    if session_check is None or session_check.workspace_id != ws_id:
        return HTMLResponse("")
    if session_check.is_active:
        return HTMLResponse("", status_code=409)

    repo = await db.get(SessionRepo, rid)
    if repo and repo.session_id == sid:
        await db.delete(repo)
        await db.commit()

    session = await db.get(Session, sid, options=[selectinload(Session.repos), selectinload(Session.github_pat)])
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("")
    return HTMLResponse("", headers={"HX-Trigger": "repoListChanged"})


# ============================================================
# Patch generation / download
# ============================================================

def _patch_filename(session: Session) -> str:
    """Build a safe filename for the downloadable patch."""
    safe_name = re.sub(r'[^\w\-.]', '_', session.name)[:80]
    return f"session-{session.id}-{safe_name}.patch"


def _prefix_diff_paths(diff: str, prefix: str) -> str:
    """Rewrite diff header paths to include the repo subdirectory prefix."""
    lines = diff.split("\n")
    out: list[str] = []
    for line in lines:
        if line.startswith("diff --git "):
            line = re.sub(r" a/", f" a/{prefix}/", line, count=1)
            line = re.sub(r" b/", f" b/{prefix}/", line, count=1)
        elif line.startswith("--- a/"):
            line = f"--- a/{prefix}/{line[6:]}"
        elif line.startswith("+++ b/"):
            line = f"+++ b/{prefix}/{line[6:]}"
        out.append(line)
    return "\n".join(out)


def _exec_in_pod(
    pod_name: str,
    namespace: str,
    workdir: str,
    command: list[str],
    container: str = "opencode",
) -> str:
    """Run a command in a running pod and return its stdout.

    Raises RuntimeError if the command exits with a non-zero status.
    """
    from kubernetes import client
    from kubernetes.stream import stream

    v1 = client.CoreV1Api()
    full_cmd = ["sh", "-c", f"cd {shlex.quote(workdir)} && {shlex.join(command)}"]
    resp = stream(
        v1.connect_get_namespaced_pod_exec,
        pod_name,
        namespace,
        command=full_cmd,
        container=container,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _preload_content=False,
    )
    stdout_data = ""
    stderr_data = ""
    while resp.is_open():
        resp.update(timeout=5)
        if resp.peek_stdout():
            stdout_data += resp.read_stdout()
        if resp.peek_stderr():
            stderr_data += resp.read_stderr()
    resp.close()

    rc = resp.returncode
    if rc and rc != 0:
        raise RuntimeError(
            f"Command failed in pod {pod_name} ({namespace}) "
            f"workdir={workdir} rc={rc}: {stderr_data.strip()}"
        )
    return stdout_data


async def _build_commit_msg(patch: str, workspace_id: int, db: AsyncSession) -> str:
    """Use an LLM to generate a commit message from the diff."""
    truncated = patch[:8000] if len(patch) > 8000 else patch

    oc_result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == workspace_id)
    )
    oc = oc_result.scalar_one_or_none()
    if not oc:
        return _fallback_commit_msg(patch)

    try:
        if oc.has_adc and oc.google_cloud_project:
            return await _llm_commit_msg_vertex(truncated, oc)
        elif oc.anthropic_api_key:
            return await _llm_commit_msg_anthropic(truncated, oc.anthropic_api_key)
        elif oc.google_api_key:
            return await _llm_commit_msg_gemini(truncated, oc.google_api_key)
    except Exception as exc:
        log.warning("LLM commit msg generation failed: %s", exc)

    return _fallback_commit_msg(patch)


_COMMIT_MSG_PROMPT = (
    "Write a concise git commit message for the following diff. "
    "Use imperative mood (e.g. 'Add', 'Fix', 'Update'). "
    "First line is the subject (max 72 chars), then a blank line, "
    "then 1-3 bullet points summarizing the key changes. "
    "Output ONLY the commit message, nothing else.\n\n"
)


async def _get_vertex_access_token(oc_secret) -> str:
    """Exchange ADC credentials for a short-lived GCP access token.

    Returns the Bearer access token string, or "" on failure.
    Called at session launch time so the token is fresh (~1 h TTL).
    """
    import json as _json
    import google.auth.transport.requests

    try:
        adc_info = _json.loads(oc_secret.application_default_credentials)
        adc_type = adc_info.get("type", "")

        if adc_type == "service_account":
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(
                adc_info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        elif adc_type == "authorized_user":
            from google.oauth2.credentials import Credentials as UserCredentials
            creds = UserCredentials(
                token=None,
                refresh_token=adc_info["refresh_token"],
                token_uri="https://oauth2.googleapis.com/token",
                client_id=adc_info["client_id"],
                client_secret=adc_info["client_secret"],
            )
        else:
            return ""

        await asyncio.to_thread(creds.refresh, google.auth.transport.requests.Request())
        return creds.token or ""
    except Exception:
        log.warning("_get_vertex_access_token: failed to exchange ADC for access token", exc_info=True)
        return ""


async def _llm_commit_msg_vertex(patch: str, oc: OpencodeSecret) -> str:
    """Call Vertex AI Anthropic Claude to generate a commit message."""
    import json
    import google.auth.transport.requests

    adc_info = json.loads(oc.application_default_credentials)
    adc_type = adc_info.get("type", "")

    if adc_type == "service_account":
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(
            adc_info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    elif adc_type == "authorized_user":
        from google.oauth2.credentials import Credentials as UserCredentials
        creds = UserCredentials(
            token=None,
            refresh_token=adc_info["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=adc_info["client_id"],
            client_secret=adc_info["client_secret"],
        )
    else:
        raise ValueError(f"Unsupported ADC type: {adc_type}")

    creds.refresh(google.auth.transport.requests.Request())
    token = creds.token

    project = oc.google_cloud_project
    location = oc.vertex_location or "global"
    model = "claude-haiku-4-5@20251001"

    if location == "global":
        host = "aiplatform.googleapis.com"
    else:
        host = f"{location}-aiplatform.googleapis.com"
    url = (
        f"https://{host}/v1/"
        f"projects/{project}/locations/{location}/publishers/anthropic/models/{model}:rawPredict"
    )

    body = {
        "anthropic_version": "vertex-2023-10-16",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": _COMMIT_MSG_PROMPT + patch}],
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=body, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()


async def _llm_commit_msg_anthropic(patch: str, api_key: str) -> str:
    """Call Anthropic API directly."""
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": _COMMIT_MSG_PROMPT + patch}],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            json=body,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()


async def _llm_commit_msg_gemini(patch: str, api_key: str) -> str:
    """Call Google Gemini API."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    body = {"contents": [{"parts": [{"text": _COMMIT_MSG_PROMPT + patch}]}]}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def _fallback_commit_msg(patch: str) -> str:
    """Simple fallback when no LLM is available."""
    files = []
    for line in patch.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                files.append(parts[1])
    if not files:
        return ""
    if len(files) == 1:
        return f"Update {files[0]}"
    elif len(files) <= 3:
        return f"Update {', '.join(files)}"
    return f"Update {len(files)} files"


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/generate-patch",
    dependencies=[Depends(require_auth)],
)
async def session_generate_patch(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(
        Session,
        sid,
        options=[selectinload(Session.github_pat), selectinload(Session.repos), selectinload(Session.prompt)],
    )

    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    if not session.repos:
        flash(request, "No repos attached — nothing to diff.", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    if not session.pod_name or session.phase != "running":
        flash(request, "Session must be running to generate a patch.", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    try:
        tool = get_tool(session.agent_tool)
        container_name = tool.get_container_name()
    except ValueError:
        container_name = "opencode"

    diff_parts: list[str] = []
    failures: list[str] = []
    for repo in session.repos:
        try:
            if session.working_branch:
                diff_cmd = ["git", "diff", f"origin/{repo.branch}"]
            else:
                diff_cmd = ["git", "diff"]
            diff = await asyncio.to_thread(
                _exec_in_pod,
                session.pod_name,
                ws.k8s_namespace,
                f"/workspace/{repo.local_path}",
                diff_cmd,
                container_name,
            )
            if diff.strip():
                diff_parts.append(diff)
        except Exception as exc:
            log.warning("git diff failed for repo %s: %s", repo.local_path, exc)
            failures.append(f"{repo.local_path}: {exc}")

    if failures:
        flash(request, f"Diff failed for: {'; '.join(failures)}", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#patch", status_code=302)

    # Capture the base commit SHA so the "Apply locally" instructions pin the exact ref
    base_ref = ""
    if diff_parts and session.repos:
        repo = session.repos[0]
        try:
            base_ref = await asyncio.to_thread(
                _exec_in_pod,
                session.pod_name,
                ws.k8s_namespace,
                f"/workspace/{repo.local_path}",
                ["git", "rev-parse", f"origin/{repo.branch}"],
                container_name,
            )
            base_ref = base_ref.strip()
        except Exception:
            base_ref = ""

    raw_patch = "\n".join(diff_parts) if diff_parts else ""
    session.patch_output = "\n".join(ln.rstrip() for ln in raw_patch.split("\n"))
    session.patch_base_ref = base_ref
    session.commit_msg = await _build_commit_msg(session.patch_output, ws_id, db) if session.patch_output.strip() else ""
    await db.commit()

    if not session.patch_output.strip():
        flash(request, "No changes detected.", "info")
    else:
        flash(request, "Patch generated.", "success")

    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#patch", status_code=302)


@router.get(
    "/workspaces/{ws_id}/sessions/{sid}/download-patch",
    dependencies=[Depends(require_auth)],
)
async def session_download_patch(
    ws_id: int,
    sid: int,
    db: AsyncSession = Depends(get_db),
):
    from starlette.responses import Response

    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id or not session.patch_output:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    filename = _patch_filename(session)
    return Response(
        content=session.patch_output,
        media_type="text/x-patch",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""},
    )


@router.get(
    "/workspaces/{ws_id}/sessions/{sid}/repos/items",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def repo_items(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Return the _repo_items.html partial for #repo-list innerHTML refresh."""
    result = await db.execute(
        select(Session)
        .where(Session.id == sid)
        .options(selectinload(Session.repos), selectinload(Session.github_pat))
    )
    session = result.scalar_one_or_none()
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("")
    pat_token = session.github_pat.pat if session.github_pat else None
    repo_info = await _fetch_repo_info(session.repos, pat_token)
    return templates.TemplateResponse(
        request,
        "sessions/_repo_items.html",
        {"ws_id": ws_id, "session": session, "repo_info": repo_info},
    )


@router.get(
    "/workspaces/{ws_id}/sessions/{sid}/repos/pick",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def repo_pick(
    ws_id: int,
    sid: int,
    pat_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Return an HTMX partial listing all repos for the selected PAT."""
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("")
    if session.is_active:
        return HTMLResponse("", status_code=409)

    pat = await db.get(GitHubPAT, pat_id)
    if pat is None or pat.workspace_id != ws_id:
        return templates.TemplateResponse(
            request,
            "sessions/_repo_picker.html",
            {"error": "PAT not found.", "repos": [], "truncated": False,
             "ws_id": ws_id, "session": session},
        )

    result = await _list_repos_for_pat(pat)
    if isinstance(result, str):
        return templates.TemplateResponse(
            request,
            "sessions/_repo_picker.html",
            {"error": result, "repos": [], "truncated": False,
             "ws_id": ws_id, "session": session},
        )

    truncated = len(result) >= 500
    return templates.TemplateResponse(
        request,
        "sessions/_repo_picker.html",
        {"repos": result, "truncated": truncated, "error": None,
         "ws_id": ws_id, "session": session},
    )
