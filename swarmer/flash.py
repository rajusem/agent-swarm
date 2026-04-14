"""Simple flash message helper using the Starlette session."""
from dataclasses import dataclass
from typing import Literal

from fastapi import Request


@dataclass
class FlashMessage:
    text: str
    type: Literal["success", "danger", "warning", "info"] = "info"


def flash(request: Request, text: str, type: str = "info") -> None:
    messages = request.session.setdefault("flash_messages", [])
    messages.append({"text": text, "type": type})
