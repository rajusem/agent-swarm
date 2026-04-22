from fastapi import Request

from swarmer.crypto import decrypt


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


def get_user_token(request: Request) -> str:
    """Return the decrypted K8s bearer token from the session.

    Raises NotAuthenticated if the session is not authenticated.
    """
    if not request.session.get("authenticated"):
        raise NotAuthenticated()
    token = decrypt(request.session.get("k8s_token", ""))
    if not token:
        raise NotAuthenticated()
    return token
