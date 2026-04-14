"""
Reverse-proxy router for server-mode sessions.

A persistent `kubectl port-forward` subprocess is started on demand and
reused for the lifetime of the session (recreated if the process dies or the
pod is replaced) — the same mechanism the TUI terminal uses for kubectl exec.

Dev mode (K8S_IN_CLUSTER=false):
    The port-forward binds to localhost on the same machine as the user's
    browser.  Accessing /chat redirects the browser directly to
    http://localhost:{port}/ so the SPA runs at the root — no sub-path
    mangling of asset URLs, API calls, or WebSocket connections.

In-cluster mode (K8S_IN_CLUSTER=true):
    Swarmer proxies all HTTP/WebSocket traffic through /chat/{path} using
    httpx + websockets, and rewrites absolute HTML asset paths so they
    resolve through the proxy prefix.
"""
import asyncio
import contextlib
import logging
import socket

import httpx
import websockets
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.config import settings
from swarmer.database import get_db
from swarmer.deps import require_auth
from swarmer.models.session import Session
from swarmer.models.workspace import Workspace

router = APIRouter()
log = logging.getLogger(__name__)

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
})

# Active port-forwards: {session_id: (process, local_port, pod_name)}
_portforwards: dict[int, tuple[asyncio.subprocess.Process, int, str]] = {}
_pf_lock = asyncio.Lock()


# ── Port-forward lifecycle ────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


async def _wait_for_port(port: int, timeout: float = 8.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return True
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.15)
    return False


async def _acquire_portforward(session_id: int, pod_name: str, namespace: str) -> int:
    """Return a local port connected via kubectl port-forward to the session pod."""
    async with _pf_lock:
        existing = _portforwards.get(session_id)
        if existing:
            proc, port, stored_pod = existing
            if proc.returncode is None and stored_pod == pod_name:
                return port
            with contextlib.suppress(Exception):
                proc.terminate()
            del _portforwards[session_id]

        local_port = _free_port()
        proc = await asyncio.create_subprocess_exec(
            "kubectl", "port-forward",
            f"pod/{pod_name}",
            f"{local_port}:4096",
            "-n", namespace,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _portforwards[session_id] = (proc, local_port, pod_name)

        if not await _wait_for_port(local_port):
            with contextlib.suppress(Exception):
                proc.terminate()
            del _portforwards[session_id]
            raise RuntimeError(f"kubectl port-forward to {pod_name}:4096 did not become ready")

        log.info("Port-forward started: session %d → 127.0.0.1:%d", session_id, local_port)
        return local_port


# ── HTML rewriting (in-cluster only) ─────────────────────────────────────────

def _rewrite_html(content: bytes, prefix: str) -> bytes:
    """Rewrite absolute asset paths in HTML so they resolve through the proxy."""
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return content

    base_tag = f'<base href="{prefix}/">'
    if "<head>" in text:
        text = text.replace("<head>", f"<head>{base_tag}", 1)
    elif "<HEAD>" in text:
        text = text.replace("<HEAD>", f"<HEAD>{base_tag}", 1)

    for old, new in [
        ('src="/',    f'src="{prefix}/'),
        ("src='/",    f"src='{prefix}/"),
        ('href="/',   f'href="{prefix}/'),
        ("href='/",   f"href='{prefix}/"),
        ('action="/', f'action="{prefix}/'),
    ]:
        text = text.replace(old, new)

    return text.encode("utf-8")


# ── Session lookup helper ─────────────────────────────────────────────────────

async def _load(ws_id: int, sid: int, db: AsyncSession):
    ws_obj = await db.get(Workspace, ws_id)
    session = await db.get(Session, sid)
    return ws_obj, session


def _session_ok(ws_obj, session, ws_id: int) -> str | None:
    """Return an error string if the session can't be proxied, else None."""
    if ws_obj is None or session is None or session.workspace_id != ws_id:
        return "Not found"
    if session.mode != "server" or not session.is_active or not session.pod_name:
        return "Session is not running in server mode"
    return None


# ── Entry point: /chat (no trailing slash) ────────────────────────────────────

async def _proxy_root(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    ws_obj, session = await _load(ws_id, sid, db)
    err = _session_ok(ws_obj, session, ws_id)
    if err:
        return Response(err, status_code=503 if "running" in err else 404, media_type="text/plain")

    local_port = await _acquire_portforward(session.id, session.pod_name, ws_obj.namespace)

    if not settings.k8s_in_cluster:
        # Dev mode: browser and port-forward are on the same machine.
        # Redirect the browser directly to localhost:{port}/ — the SPA loads
        # at the root with no sub-path, so all its asset/API/WS URLs work.
        return RedirectResponse(url=f"http://localhost:{local_port}/", status_code=302)

    # In-cluster: redirect to /chat/ so the sub-path proxy can serve assets.
    return RedirectResponse(
        url=f"/workspaces/{ws_id}/sessions/{sid}/chat/",
        status_code=302,
    )


# ── Sub-path proxy (in-cluster) ───────────────────────────────────────────────

async def _chat_http_proxy(
    ws_id: int,
    sid: int,
    request: Request,
    path: str,
    db: AsyncSession,
) -> Response:
    ws_obj, session = await _load(ws_id, sid, db)
    err = _session_ok(ws_obj, session, ws_id)
    if err:
        return Response(err, status_code=503 if "running" in err else 404, media_type="text/plain")

    try:
        local_port = await _acquire_portforward(session.id, session.pod_name, ws_obj.namespace)
    except Exception as exc:
        log.warning("Could not establish port-forward for session %d: %s", sid, exc)
        return Response(f"Could not connect to session: {exc}", status_code=503, media_type="text/plain")

    query = str(request.url.query)
    upstream_url = f"http://127.0.0.1:{local_port}/{path}"
    if query:
        upstream_url += f"?{query}"

    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    fwd_headers["x-opencode-directory"] = "/workspace"

    try:
        async with httpx.AsyncClient() as client:
            upstream_resp = await client.request(
                method=request.method,
                url=upstream_url,
                headers=fwd_headers,
                content=await request.body(),
                follow_redirects=False,
                timeout=30.0,
            )
    except httpx.ConnectError as exc:
        async with _pf_lock:
            _portforwards.pop(sid, None)
        return Response(f"Could not connect to session: {exc}", status_code=503, media_type="text/plain")

    content_type = upstream_resp.headers.get("content-type", "")
    content = upstream_resp.content

    if "text/html" in content_type:
        prefix = f"/workspaces/{ws_id}/sessions/{sid}/chat"
        content = _rewrite_html(content, prefix)

    resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in _HOP_BY_HOP}
    resp_headers.pop("content-length", None)

    return Response(
        content=content,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=content_type or None,
    )


async def _proxy_path(
    ws_id: int,
    sid: int,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    return await _chat_http_proxy(ws_id, sid, request, path, db)


_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

router.add_api_route(
    "/workspaces/{ws_id}/sessions/{sid}/chat",
    _proxy_root,
    methods=_PROXY_METHODS,
    dependencies=[Depends(require_auth)],
)
router.add_api_route(
    "/workspaces/{ws_id}/sessions/{sid}/chat/{path:path}",
    _proxy_path,
    methods=_PROXY_METHODS,
    dependencies=[Depends(require_auth)],
)


# ── WebSocket proxy ───────────────────────────────────────────────────────────

@router.websocket("/workspaces/{ws_id}/sessions/{sid}/chat/{path:path}")
async def chat_ws_proxy(
    websocket: WebSocket,
    ws_id: int,
    sid: int,
    path: str,
):
    await websocket.accept()

    async for db in get_db():
        ws_obj = await db.get(Workspace, ws_id)
        session = await db.get(Session, sid)
        break

    if (
        ws_obj is None
        or session is None
        or session.workspace_id != ws_id
        or session.mode != "server"
        or not session.is_active
        or not session.pod_name
    ):
        await websocket.close(code=4004, reason="Session unavailable")
        return

    try:
        local_port = await _acquire_portforward(session.id, session.pod_name, ws_obj.namespace)
    except Exception as exc:
        log.warning("Port-forward failed for WS session %d: %s", sid, exc)
        await websocket.close(code=4004, reason="Could not connect to session")
        return

    query = websocket.url.query
    upstream_url = f"ws://127.0.0.1:{local_port}/{path}"
    if query:
        upstream_url += f"?{query}"

    try:
        async with websockets.connect(upstream_url) as upstream_ws:
            async def client_to_upstream() -> None:
                try:
                    while True:
                        data = await websocket.receive()
                        if "text" in data:
                            await upstream_ws.send(data["text"])
                        elif "bytes" in data:
                            await upstream_ws.send(data["bytes"])
                except (WebSocketDisconnect, Exception):
                    pass

            async def upstream_to_client() -> None:
                try:
                    async for message in upstream_ws:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except Exception:
                    pass

            tasks = [
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    except Exception as exc:
        log.warning("Chat WS proxy error for session %d: %s", sid, exc)
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()
