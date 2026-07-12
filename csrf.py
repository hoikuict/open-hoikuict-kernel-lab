import hashlib
import hmac
import logging
import os
import secrets

from fastapi import HTTPException, Request, WebSocket
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

CSRF_COOKIE_NAME = "hoikuict_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
logger = logging.getLogger(__name__)


def _secret_key() -> bytes:
    configured = os.getenv("HOIKUICT_SECRET_KEY")
    if configured:
        return configured.encode("utf-8")
    return b"open-hoikuict-development-only-csrf-key"


def _signature(nonce: str) -> str:
    return hmac.new(_secret_key(), nonce.encode("ascii"), hashlib.sha256).hexdigest()


def generate_csrf_token() -> str:
    nonce = secrets.token_urlsafe(32)
    return f"{nonce}.{_signature(nonce)}"


def csrf_token_is_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    nonce, signature = token.rsplit(".", 1)
    if not nonce or not signature:
        return False
    try:
        expected = _signature(nonce)
    except (UnicodeEncodeError, ValueError):
        return False
    return hmac.compare_digest(signature, expected)


def _cookie_kwargs() -> dict[str, object]:
    return {
        "httponly": True,
        "secure": os.getenv("HOIKUICT_COOKIE_SECURE") == "1",
        "samesite": "lax",
        "path": "/",
    }


class CsrfTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        existing = request.cookies.get(CSRF_COOKIE_NAME)
        token = existing if csrf_token_is_valid(existing) else generate_csrf_token()
        request.state.csrf_token = token
        response = await call_next(request)
        if token != existing:
            response.set_cookie(
                CSRF_COOKIE_NAME,
                token,
                max_age=60 * 60 * 8,
                **_cookie_kwargs(),
            )
        return response


async def verify_csrf(request: Request = None, websocket: WebSocket = None) -> None:
    # App-level dependencies are also evaluated for WebSocket routes. WebSockets
    # use authentication/authorization and Origin validation instead of CSRF.
    if websocket is not None:
        return
    if request is None:
        raise RuntimeError("CSRF検証対象のHTTPリクエストを解決できませんでした")
    if request.method.upper() in SAFE_METHODS:
        return

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    presented = request.headers.get("X-CSRF-Token")
    content_type = request.headers.get("content-type", "").lower()
    if not presented and (
        content_type.startswith("application/x-www-form-urlencoded")
        or content_type.startswith("multipart/form-data")
    ):
        form = await request.form()
        form_value = form.get("csrf_token")
        presented = str(form_value) if form_value is not None else None

    valid = (
        csrf_token_is_valid(cookie_token)
        and presented is not None
        and hmac.compare_digest(cookie_token or "", presented)
    )
    if valid:
        return

    if os.getenv("HOIKUICT_CSRF_ENFORCE", "0") == "1":
        raise HTTPException(status_code=403, detail="CSRFトークンが不正です")
    logger.warning("CSRF validation failed for %s %s", request.method, request.url.path)


def rotate_csrf_token(response: Response) -> None:
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
