"""
WebSocket endpoint that proxies a browser xterm.js terminal to
`kubectl exec -it <pod>` using a local pseudo-terminal (pty) so that
kubectl sees a real TTY and can allocate one inside the pod.
"""
import asyncio
import errno
import fcntl
import json
import logging
import os
import pty
import shlex
import struct
import termios

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import selectinload

from swarmer.database import get_db
from swarmer.models.session import Session
from swarmer.models.workspace import Workspace

router = APIRouter()
log = logging.getLogger(__name__)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Set the PTY window size so the child process sees the correct dimensions."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


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

    if not session.pod_name or session.phase != "running":
        await websocket.close(code=4003, reason="Session not running")
        return

    namespace = ws.k8s_namespace
    pod_name = session.pod_name

    # Resolve tool-specific container name and TUI command
    from swarmer.agent_tools.registry import get as get_tool
    tool = get_tool(session.agent_tool)
    container_name = tool.get_container_name()

    # ---------- Open a PTY pair ----------
    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, rows, cols)
    # Separate pipe for kubectl stderr so error messages aren't lost if the
    # PTY closes before xterm can display them.
    stderr_r, stderr_w = os.pipe()
    proc = None
    loop = asyncio.get_running_loop()

    try:
        tui_cmd_parts = [tool.get_tui_binary()]
        if session.model:
            tui_cmd_parts.extend(["--model", session.model])
        if session.resume:
            tui_cmd_parts.append("--continue")
        tui_shell = "export PATH=\"$HOME/.local/bin:$PATH\" && exec " + " ".join(shlex.quote(p) for p in tui_cmd_parts)
        proc = await asyncio.create_subprocess_exec(
            "kubectl", "exec", "-it", pod_name,
            "-n", namespace,
            "-c", container_name,
            "--", "sh", "-c", tui_shell,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=stderr_w,
        )
        os.close(slave_fd)
        slave_fd = -1
        os.close(stderr_w)
        stderr_w = -1

        # Non-blocking reads so asyncio add_reader works.
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        flags = fcntl.fcntl(stderr_r, fcntl.F_GETFL)
        fcntl.fcntl(stderr_r, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        read_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def _pty_readable() -> None:
            try:
                data = os.read(master_fd, 4096)
                if data:
                    loop.call_soon_threadsafe(read_queue.put_nowait, data)
                # empty read from PTY — treat as EOF
                else:
                    loop.call_soon_threadsafe(read_queue.put_nowait, None)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    # Slave side closed: kubectl / opencode exited.
                    loop.call_soon_threadsafe(read_queue.put_nowait, None)
                # EAGAIN / EWOULDBLOCK: no data yet — spurious wakeup, ignore.

        def _stderr_readable() -> None:
            try:
                data = os.read(stderr_r, 4096)
                if data:
                    # Prefix stderr output in red so it's visible in the terminal.
                    msg = b"\r\n\x1b[31m" + data + b"\x1b[0m"
                    loop.call_soon_threadsafe(read_queue.put_nowait, msg)
            except OSError:
                pass

        loop.add_reader(master_fd, _pty_readable)
        loop.add_reader(stderr_r, _stderr_readable)

        async def read_loop() -> None:
            """Forward pty master output → browser."""
            try:
                while True:
                    chunk = await read_queue.get()
                    if chunk is None:
                        break
                    await websocket.send_bytes(chunk)
            except Exception:
                pass

        async def write_loop() -> None:
            """Forward browser keystrokes → pty master; handle resize messages."""
            try:
                while True:
                    msg = await websocket.receive()
                    if msg.get("bytes"):
                        os.write(master_fd, msg["bytes"])
                    elif msg.get("text"):
                        try:
                            payload = json.loads(msg["text"])
                            if payload.get("type") == "resize":
                                _set_winsize(master_fd, payload["rows"], payload["cols"])
                        except Exception:
                            pass
            except WebSocketDisconnect:
                pass
            except Exception:
                pass

        read_task = asyncio.create_task(read_loop())
        write_task = asyncio.create_task(write_loop())

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
        log.error("TUI proxy error: %s", exc)
    finally:
        try:
            loop.remove_reader(master_fd)
        except Exception:
            pass
        try:
            loop.remove_reader(stderr_r)
        except Exception:
            pass
        for fd in (slave_fd, stderr_w, master_fd, stderr_r):
            if fd >= 0:
                try:
                    os.close(fd)
                except Exception:
                    pass
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass
