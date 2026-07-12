import os
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi import Depends, FastAPI, File, Request, Response, UploadFile, WebSocket
from fastapi.testclient import TestClient

from auth import (
    Role,
    StaffSessionSubject,
    StaffUser,
    configure_staff_auth_backend,
    get_current_staff_user,
    get_optional_current_staff_user,
    reset_auth_backends,
    staff_auth_http_exception_handler,
)
from csrf import CSRF_COOKIE_NAME, CsrfTokenMiddleware, verify_csrf
from kiosk_security import KIOSK_DEVICE_COOKIE, kiosk_device_cookie_is_valid
from routers.guardian import router as guardian_router
import routers.staff_auth as staff_auth_module
from security_config import validate_runtime_security, websocket_runtime_available
from starlette.exceptions import HTTPException as StarletteHTTPException
from testing_helpers import authenticate_mock_staff, configure_test_environment


class SecurityControlTests(unittest.TestCase):
    def setUp(self):
        configure_test_environment()

    def test_staff_cookie_is_required_for_protected_dependency(self):
        app = FastAPI()

        @app.get("/optional")
        def optional(current_user=Depends(get_optional_current_staff_user)):
            return {"authenticated": current_user is not None}

        @app.get("/protected")
        def protected(current_user=Depends(get_current_staff_user)):
            return {"role": current_user.role.value}

        with TestClient(app) as client:
            self.assertEqual(client.get("/optional").json(), {"authenticated": False})
            self.assertEqual(client.get("/protected").status_code, 401)
            authenticate_mock_staff(client)
            self.assertEqual(client.get("/protected").status_code, 200)
            with patch.dict(os.environ, {"HOIKUICT_ENABLE_MOCK_AUTH": "0"}):
                self.assertEqual(client.get("/protected").status_code, 401)

    def test_unauthenticated_browser_get_redirects_to_mock_login(self):
        app = FastAPI()
        app.add_exception_handler(StarletteHTTPException, staff_auth_http_exception_handler)

        @app.get("/protected")
        def protected(current_user=Depends(get_current_staff_user)):
            return {"role": current_user.role.value}

        with TestClient(app) as client:
            browser_response = client.get(
                "/protected?page=2",
                headers={"Accept": "text/html"},
                follow_redirects=False,
            )
            self.assertEqual(browser_response.status_code, 303)
            self.assertEqual(
                browser_response.headers["location"],
                "/staff/login",
            )
            api_response = client.get("/protected", headers={"Accept": "application/json"})
            self.assertEqual(api_response.status_code, 401)
            with patch.dict(os.environ, {"HOIKUICT_ENABLE_MOCK_AUTH": "0"}):
                disabled_response = client.get(
                    "/protected",
                    headers={"Accept": "text/html"},
                    follow_redirects=False,
                )
                self.assertEqual(disabled_response.status_code, 401)

    def test_csrf_first_get_token_can_be_used_immediately(self):
        app = FastAPI(dependencies=[Depends(verify_csrf)])
        app.add_middleware(CsrfTokenMiddleware)

        @app.get("/form")
        def form(request: Request):
            return {"token": request.state.csrf_token}

        @app.post("/save")
        def save():
            return {"saved": True}

        @app.post("/upload")
        async def upload(file: UploadFile = File(...)):
            return {"size": len(await file.read())}

        with patch.dict(
            os.environ,
            {"HOIKUICT_CSRF_ENFORCE": "1", "HOIKUICT_SECRET_KEY": "s" * 40},
        ):
            with TestClient(app) as client:
                first = client.get("/form")
                token = first.json()["token"]
                self.assertEqual(client.cookies.get(CSRF_COOKIE_NAME), token)
                accepted = client.post("/save", headers={"X-CSRF-Token": token})
                self.assertEqual(accepted.status_code, 200)
                uploaded = client.post(
                    "/upload",
                    data={"csrf_token": token},
                    files={"file": ("sample.txt", b"sample", "text/plain")},
                )
                self.assertEqual(uploaded.status_code, 200)
                self.assertEqual(uploaded.json(), {"size": 6})
            with TestClient(app) as client:
                self.assertEqual(client.post("/save").status_code, 403)

    def test_app_level_csrf_dependency_skips_websockets(self):
        app = FastAPI(dependencies=[Depends(verify_csrf)])

        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("connected")
            await websocket.close()

        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                self.assertEqual(websocket.receive_text(), "connected")

    def test_production_configuration_fails_closed(self):
        with patch.dict(os.environ, {"HOIKUICT_ENV": "production"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "productionセキュリティ設定"):
                validate_runtime_security()

    def test_missing_websocket_driver_fails_at_startup(self):
        with patch("security_config.find_spec", return_value=None):
            self.assertFalse(websocket_runtime_available())
            with self.assertRaisesRegex(RuntimeError, "WebSocketドライバー"):
                validate_runtime_security()

    def test_kiosk_activation_uses_post_body_and_signed_device_cookie(self):
        app = FastAPI(dependencies=[Depends(verify_csrf)])
        app.add_middleware(CsrfTokenMiddleware)
        app.include_router(guardian_router)
        settings = {
            "HOIKUICT_ENV": "development",
            "HOIKUICT_KIOSK_ACCESS_MODE": "token",
            "HOIKUICT_KIOSK_TOKEN": "device-secret",
            "HOIKUICT_SECRET_KEY": "k" * 40,
            "HOIKUICT_CSRF_ENFORCE": "1",
        }
        with patch.dict(os.environ, settings):
            with TestClient(app) as client:
                initial = client.get("/guardian/activate")
                self.assertEqual(initial.status_code, 200)
                csrf_token = client.cookies.get(CSRF_COOKIE_NAME)
                activated = client.post(
                    "/guardian/activate",
                    data={"kiosk_token": "device-secret", "csrf_token": csrf_token},
                    follow_redirects=False,
                )
                self.assertEqual(activated.status_code, 303)
                device_cookie = client.cookies.get(KIOSK_DEVICE_COOKIE)
                self.assertTrue(kiosk_device_cookie_is_valid(device_cookie))
                with patch.dict(os.environ, {"HOIKUICT_KIOSK_TOKEN": "rotated"}):
                    self.assertFalse(kiosk_device_cookie_is_valid(device_cookie))

    def test_disabled_kiosk_hides_activation_route(self):
        app = FastAPI()
        app.include_router(guardian_router)
        with patch.dict(
            os.environ,
            {"HOIKUICT_ENV": "development", "HOIKUICT_KIOSK_ACCESS_MODE": "disabled"},
        ):
            with TestClient(app) as client:
                self.assertEqual(client.get("/guardian/activate").status_code, 404)
                self.assertEqual(client.get("/guardian/").status_code, 404)

    def test_external_backend_controls_session_and_hides_mock_login(self):
        class ExternalBackend:
            mode = "external"
            cookie_name = "external_staff_session"

            def resolve_principal(self, connection):
                if connection.cookies.get(self.cookie_name) != "active":
                    return None
                return StaffUser(
                    role=Role.ADMIN,
                    name="外部認証職員",
                    user_id=UUID("00000000-0000-0000-0000-000000000099"),
                )

            def establish_session(self, response, subject):
                del subject
                response.set_cookie(self.cookie_name, "active", httponly=True)

            def clear_session(self, response):
                response.delete_cookie(self.cookie_name)

        backend = ExternalBackend()
        configure_staff_auth_backend(backend)
        try:
            app = FastAPI()
            app.include_router(staff_auth_module.mock_login_router)

            @app.post("/session")
            def establish(response: Response):
                backend.establish_session(
                    response,
                    StaffSessionSubject(
                        user_id=UUID("00000000-0000-0000-0000-000000000099"),
                        display_name="外部認証職員",
                        role=Role.ADMIN,
                    ),
                )
                return {"ok": True}

            @app.delete("/session")
            def clear(response: Response):
                backend.clear_session(response)
                return {"ok": True}

            @app.get("/protected")
            def protected(current_user=Depends(get_current_staff_user)):
                return {"name": current_user.name}

            with TestClient(app) as client:
                self.assertEqual(client.get("/staff/login").status_code, 404)
                self.assertEqual(client.get("/protected").status_code, 401)
                self.assertEqual(client.post("/session").status_code, 200)
                self.assertEqual(client.get("/protected").json()["name"], "外部認証職員")
                self.assertEqual(client.delete("/session").status_code, 200)
                self.assertEqual(client.get("/protected").status_code, 401)
        finally:
            reset_auth_backends()


if __name__ == "__main__":
    unittest.main()
