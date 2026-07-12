import os
from importlib.util import find_spec
from urllib.parse import urlsplit

from starlette.websockets import WebSocket


def deployment_environment() -> str:
    return (os.getenv("HOIKUICT_ENV") or "production").strip().lower()


def is_production() -> bool:
    return deployment_environment() == "production"


def allowed_origins() -> set[str]:
    raw = os.getenv("HOIKUICT_ALLOWED_ORIGINS", "")
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


def websocket_origin_allowed(websocket: WebSocket) -> bool:
    origin = (websocket.headers.get("origin") or "").strip().rstrip("/")
    configured = allowed_origins()
    if configured:
        return origin in configured
    if is_production():
        return False
    if not origin:
        return True
    parsed = urlsplit(origin)
    return bool(parsed.scheme in {"http", "https"} and parsed.netloc == websocket.headers.get("host"))


def kiosk_access_mode() -> str:
    return (os.getenv("HOIKUICT_KIOSK_ACCESS_MODE") or "disabled").strip().lower()


def websocket_runtime_available() -> bool:
    return find_spec("websockets") is not None or find_spec("wsproto") is not None


def validate_runtime_security() -> None:
    environment = deployment_environment()
    if environment not in {"development", "production", "test"}:
        raise RuntimeError("HOIKUICT_ENV は development / production / test のいずれかです")
    if not websocket_runtime_available():
        raise RuntimeError(
            "WebSocketドライバーがありません。プロジェクトの仮想環境で "
            "pip install -r requirements.txt を実行してください"
        )
    mode = kiosk_access_mode()
    if mode not in {"disabled", "token", "open"}:
        raise RuntimeError("HOIKUICT_KIOSK_ACCESS_MODE が不正です")
    if mode == "token" and not os.getenv("HOIKUICT_KIOSK_TOKEN"):
        raise RuntimeError("tokenモードでは HOIKUICT_KIOSK_TOKEN が必要です")
    if not is_production():
        return

    errors: list[str] = []
    if os.getenv("HOIKUICT_ENABLE_MOCK_AUTH") == "1":
        errors.append("モック認証を無効にしてください")
    if os.getenv("HOIKUICT_ENABLE_MOCK_ROLE_OVERRIDE") == "1":
        errors.append("モックrole上書きを無効にしてください")
    if os.getenv("HOIKUICT_COOKIE_SECURE") != "1":
        errors.append("HOIKUICT_COOKIE_SECURE=1 が必要です")
    if os.getenv("HOIKUICT_CSRF_ENFORCE") != "1":
        errors.append("HOIKUICT_CSRF_ENFORCE=1 が必要です")
    if len(os.getenv("HOIKUICT_SECRET_KEY", "")) < 32:
        errors.append("32文字以上の HOIKUICT_SECRET_KEY が必要です")
    if not allowed_origins():
        errors.append("HOIKUICT_ALLOWED_ORIGINS が必要です")
    if mode == "open":
        errors.append("productionではguardian openモードを使用できません")
    if errors:
        raise RuntimeError("productionセキュリティ設定が不正です: " + "; ".join(errors))
