import hashlib
import hmac
import os
import time
from uuid import uuid4

from fastapi import HTTPException, Request, Response

from security_config import is_production, kiosk_access_mode

KIOSK_DEVICE_COOKIE = "hoikuict_kiosk_device"
KIOSK_DEVICE_MAX_AGE = 60 * 60 * 24 * 365


def _secret() -> bytes:
    secret = os.getenv("HOIKUICT_SECRET_KEY", "")
    activation_token = os.getenv("HOIKUICT_KIOSK_TOKEN", "")
    if not secret or not activation_token:
        raise RuntimeError("HOIKUICT_SECRET_KEY と HOIKUICT_KIOSK_TOKEN が必要です")
    return hashlib.sha256(f"{secret}\0{activation_token}".encode("utf-8")).digest()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).hexdigest()


def issue_kiosk_device_cookie(response: Response) -> None:
    payload = f"{uuid4().hex}.{int(time.time())}"
    value = f"{payload}.{_sign(payload)}"
    response.set_cookie(
        KIOSK_DEVICE_COOKIE,
        value,
        max_age=KIOSK_DEVICE_MAX_AGE,
        httponly=True,
        secure=os.getenv("HOIKUICT_COOKIE_SECURE") == "1",
        samesite="strict",
        path="/guardian",
    )


def kiosk_device_cookie_is_valid(value: str | None) -> bool:
    if not value:
        return False
    try:
        device_id, issued_raw, signature = value.split(".", 2)
        issued_at = int(issued_raw)
        payload = f"{device_id}.{issued_raw}"
        age = int(time.time()) - issued_at
        return (
            len(device_id) == 32
            and 0 <= age <= KIOSK_DEVICE_MAX_AGE
            and hmac.compare_digest(signature, _sign(payload))
        )
    except (RuntimeError, TypeError, ValueError):
        return False


def require_kiosk_access(request: Request) -> None:
    mode = kiosk_access_mode()
    if mode == "open":
        if not is_production() and (
            os.getenv("HOIKUICT_ENV") == "development"
            or os.getenv("HOIKUICT_ALLOW_OPEN_KIOSK") == "1"
        ):
            return
        raise HTTPException(status_code=404, detail="Not Found")
    if mode == "token" and kiosk_device_cookie_is_valid(
        request.cookies.get(KIOSK_DEVICE_COOKIE)
    ):
        return
    raise HTTPException(status_code=404, detail="Not Found")


def require_kiosk_activation_mode() -> None:
    if kiosk_access_mode() != "token":
        raise HTTPException(status_code=404, detail="Not Found")


def kiosk_activation_token_is_valid(candidate: str) -> bool:
    expected = os.getenv("HOIKUICT_KIOSK_TOKEN", "")
    return bool(expected and hmac.compare_digest(candidate, expected))
