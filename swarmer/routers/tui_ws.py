"""
WebSocket endpoint that proxies a browser xterm.js terminal to a session pod
using the Kubernetes Python client exec stream (no kubectl subprocess needed).
For OpenShell sandbox sessions, uses the ExecSandboxInteractive gRPC stream.
"""
import asyncio
import json
import logging
import shlex
import threading

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import selectinload

from swarmer.database import get_db
from swarmer.models.session import Session
from swarmer.models.workspace import Workspace

router = APIRouter()
log = logging.getLogger(__name__)

_STDIN_CHANNEL = 0
_RESIZE_CHANNEL = 4


@router.websocket("/ws/{ws_id}/sessions/{sid}/tui")
async def session_tui(
    websocket: WebSocket,
    ws_id: int,
    sid: int,
    cols: int = 80,
    rows: int = 24,
):
    await websocket.accept()

    # ---------- Authenticate via one-time token ----------
    try:
        token = await asyncio.wait_for(websocket.receive_text(), timeout=10)
    except asyncio.TimeoutError:
        await websocket.close(code=4001, reason="Auth timeout")
        return

    session_data = websocket.session
    tui_tokens: list = session_data.get("tui_tokens", [])
    if token not in tui_tokens:
        log.warning("TUI WS: invalid/missing token for session %d (have %d tokens)", sid, len(tui_tokens))
        await websocket.close(code=4001, reason="Invalid token")
        return
    tui_tokens.remove(token)
    session_data["tui_tokens"] = tui_tokens

    # ---------- Load session from DB ----------
    async for db in get_db():
        session = await db.get(
            Session,
            sid,
            options=[selectinload(Session.workspace)],
        )
        ws = await db.get(Workspace, ws_id)
        break

    if session is None or ws is None or session.workspace_id != ws_id:
        await websocket.close(code=4002, reason="Session not found")
        return

    is_openshell = bool(session.sandbox_name)

    if session.phase != "running" or (not is_openshell and not session.pod_name):
        log.warning(
            "TUI WS: session %d not running (phase=%s, pod=%s, sandbox=%s)",
            sid, session.phase, session.pod_name, session.sandbox_name,
        )
        await websocket.close(code=4003, reason="Session not running")
        return

    from swarmer.agent_tools.registry import get as get_tool
    tool = get_tool(session.agent_tool)

    tui_cmd_parts = [tool.get_tui_binary()]
    # Rewrite google-vertex-anthropic models to anthropic/ format so OpenCode
    # uses the anthropic provider (routed via inference.local) instead of trying
    # to load the google-vertex-anthropic provider which is not in enabled_providers.
    _tui_model = session.model or ""
    if _tui_model.startswith("google-vertex-anthropic/"):
        from swarmer.routers.sessions import _extract_vertex_model
        _tui_model = f"anthropic/{_extract_vertex_model(_tui_model)}"
    if _tui_model and hasattr(tool, 'get_tui_model_args'):
        tui_cmd_parts.extend(tool.get_tui_model_args(_tui_model))
    elif _tui_model and tool.name != "crush":
        tui_cmd_parts.extend(["--model", _tui_model])
    cmd_base = " ".join(shlex.quote(p) for p in tui_cmd_parts)
    tui_shell = (
        f"export HOME=/sandbox PATH=\"/sandbox/.local/bin:$PATH\" && "
        f"{{ {cmd_base} --continue || exec {cmd_base}; }}"
    )

    loop = asyncio.get_running_loop()
    read_q: asyncio.Queue[bytes | None] = asyncio.Queue()
    stop_event = threading.Event()

    if is_openshell:
        await _run_openshell_tui(
            websocket=websocket,
            session=session,
            tui_shell=tui_shell,
            cols=cols,
            rows=rows,
            loop=loop,
            read_q=read_q,
            stop_event=stop_event,
        )
    else:
        await _run_k8s_tui(
            websocket=websocket,
            session=session,
            ws=ws,
            tool=tool,
            tui_shell=tui_shell,
            cols=cols,
            rows=rows,
            loop=loop,
            read_q=read_q,
            stop_event=stop_event,
        )


async def _run_openshell_tui(
    websocket: WebSocket,
    session: Session,
    tui_shell: str,
    cols: int,
    rows: int,
    loop,
    read_q: asyncio.Queue,
    stop_event: threading.Event,
) -> None:
    from swarmer import openshell_client
    from openshell._proto import openshell_pb2

    sandbox_name = session.sandbox_name

    # Resolve sandbox_id synchronously (brief blocking call)
    try:
        sandbox_id = await openshell_client._sandbox_id(sandbox_name, openshell_client._get_client())
    except Exception as exc:
        log.error("TUI: sandbox_id lookup failed for %s: %s", sandbox_name, exc)
        await websocket.close(code=4004, reason="Sandbox lookup failed")
        return

    command = ["sh", "-c", tui_shell]

    try:
        client = openshell_client._get_client()
        response_stream, input_q = openshell_client.exec_interactive(
            sandbox_name=sandbox_name,
            sandbox_id=sandbox_id,
            command=command,
            cols=cols,
            rows=rows,
            client=client,
        )
    except Exception as exc:
        log.error("TUI: exec_interactive failed for sandbox %s: %s", sandbox_name, exc)
        await websocket.close(code=4004, reason="Exec failed")
        return

    def _stream_reader() -> None:
        """Drain the gRPC response stream into read_q (background thread)."""
        try:
            for event in response_stream:
                if stop_event.is_set():
                    break
                which = event.WhichOneof("payload")
                if which == "stdout":
                    data = event.stdout.data
                    if data:
                        loop.call_soon_threadsafe(read_q.put_nowait, data)
                elif which == "stderr":
                    data = event.stderr.data
                    if data:
                        chunk = b"\r\n\x1b[31m" + data + b"\x1b[0m"
                        loop.call_soon_threadsafe(read_q.put_nowait, chunk)
                elif which == "exit":
                    break
        except Exception as exc:
            if not stop_event.is_set():
                log.error("TUI gRPC stream reader error for sandbox %s: %s", sandbox_name, exc)
        finally:
            loop.call_soon_threadsafe(read_q.put_nowait, None)

    reader_thread = threading.Thread(target=_stream_reader, daemon=True)
    reader_thread.start()

    async def read_loop() -> None:
        try:
            while True:
                chunk = await read_q.get()
                if chunk is None:
                    break
                await websocket.send_bytes(chunk)
        except Exception:
            pass

    async def write_loop() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("bytes"):
                    input_q.put(openshell_pb2.ExecSandboxInput(stdin=msg["bytes"]))
                elif msg.get("text"):
                    try:
                        payload = json.loads(msg["text"])
                        if payload.get("type") == "resize":
                            input_q.put(openshell_pb2.ExecSandboxInput(
                                resize=openshell_pb2.ExecSandboxWindowResize(
                                    cols=payload.get("cols", 80),
                                    rows=payload.get("rows", 24),
                                )
                            ))
                    except Exception:
                        pass
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    read_task = asyncio.create_task(read_loop())
    write_task = asyncio.create_task(write_loop())

    try:
        done, pending = await asyncio.wait(
            [read_task, write_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    except Exception as exc:
        log.error("TUI proxy error for sandbox %s: %s", sandbox_name, exc)
    finally:
        stop_event.set()
        input_q.put(None)  # stop the gRPC request generator
        reader_thread.join(timeout=2.0)
        try:
            await websocket.close()
        except Exception:
            pass


async def _run_k8s_tui(
    websocket: WebSocket,
    session: Session,
    ws: Workspace,
    tool,
    tui_shell: str,
    cols: int,
    rows: int,
    loop,
    read_q: asyncio.Queue,
    stop_event: threading.Event,
) -> None:
    namespace = ws.k8s_namespace
    pod_name = session.pod_name
    container_name = tool.get_container_name()

    from kubernetes import client as k8s_client
    from kubernetes.stream import stream as k8s_stream

    v1 = k8s_client.CoreV1Api()
    try:
        exec_resp = k8s_stream(
            v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            container=container_name,
            command=["sh", "-c", tui_shell],
            stderr=True,
            stdin=True,
            stdout=True,
            tty=True,
            _preload_content=False,
        )
    except Exception as exc:
        log.error("TUI exec stream open failed for pod %s: %s", pod_name, exc)
        try:
            await websocket.close(code=4004, reason="Exec failed")
        except Exception:
            pass
        return

    # Send initial terminal size (channel 4 = resize)
    try:
        exec_resp.write_channel(
            _RESIZE_CHANNEL, json.dumps({"Width": cols, "Height": rows})
        )
    except Exception:
        pass

    def _stream_reader() -> None:
        """Pump pod stdout/stderr into read_q (runs in a background thread)."""
        try:
            while not stop_event.is_set() and exec_resp.is_open():
                exec_resp.update(timeout=0.1)
                if exec_resp.peek_stdout():
                    data = exec_resp.read_stdout()
                    if data:
                        chunk = data if isinstance(data, bytes) else data.encode("utf-8", errors="replace")
                        loop.call_soon_threadsafe(read_q.put_nowait, chunk)
                if exec_resp.peek_stderr():
                    data = exec_resp.read_stderr()
                    if data:
                        chunk = data if isinstance(data, bytes) else data.encode("utf-8", errors="replace")
                        loop.call_soon_threadsafe(read_q.put_nowait, b"\r\n\x1b[31m" + chunk + b"\x1b[0m")
        except Exception as exc:
            if not stop_event.is_set():
                log.error("TUI stream reader error for pod %s: %s", pod_name, exc)
        finally:
            if not stop_event.is_set():
                log.info("TUI stream reader: exec closed for pod %s", pod_name)
            loop.call_soon_threadsafe(read_q.put_nowait, None)

    reader_thread = threading.Thread(target=_stream_reader, daemon=True)
    reader_thread.start()

    async def read_loop() -> None:
        try:
            while True:
                chunk = await read_q.get()
                if chunk is None:
                    break
                await websocket.send_bytes(chunk)
        except Exception:
            pass

    async def write_loop() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("bytes"):
                    # Raw bytes from xterm.js → pod stdin (channel 0)
                    exec_resp.write_channel(_STDIN_CHANNEL, msg["bytes"])
                elif msg.get("text"):
                    try:
                        payload = json.loads(msg["text"])
                        if payload.get("type") == "resize":
                            exec_resp.write_channel(
                                _RESIZE_CHANNEL,
                                json.dumps({
                                    "Width": payload.get("cols", 80),
                                    "Height": payload.get("rows", 24),
                                }),
                            )
                    except Exception:
                        pass
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    read_task = asyncio.create_task(read_loop())
    write_task = asyncio.create_task(write_loop())

    try:
        done, pending = await asyncio.wait(
            [read_task, write_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    except Exception as exc:
        log.error("TUI proxy error for pod %s: %s", pod_name, exc)
    finally:
        stop_event.set()
        try:
            exec_resp.close()
        except Exception:
            pass
        reader_thread.join(timeout=2.0)
        try:
            await websocket.close()
        except Exception:
            pass
