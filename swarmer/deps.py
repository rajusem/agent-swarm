from fastapi import Request


class NotAuthenticated(Exception):
    """Raised by require_auth when the session cookie is missing or invalid."""


def require_auth(request: Request) -> None:
    """FastAPI dependency — raises NotAuthenticated if no valid session exists.

    Register the exception handler in main.py:
        @app.exception_handler(NotAuthenticated)
        async def _(...):
            return RedirectResponse("/login")
    """
    if not request.session.get("authenticated"):
        raise NotAuthenticated()
