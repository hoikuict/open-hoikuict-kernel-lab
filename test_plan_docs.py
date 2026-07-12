import unittest
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from auth import MOCK_CALENDAR_USER_COOKIE, MOCK_ROLE_COOKIE, MOCK_STAFF_NAME_COOKIE
from models import Classroom
from plan_docs.routers.bunrei import router as bunrei_router
from plan_docs.routers.documents import router as documents_router
from plan_docs.routers.home import router as home_router
from plan_docs.routers.plans import router as plans_router
import plan_docs.auth_adapter as plan_docs_auth
from testing_helpers import configure_test_environment


class PlanDocsIntegrationTests(unittest.TestCase):
    def setUp(self):
        configure_test_environment()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        with Session(self.engine) as session:
            session.add(Classroom(name="ひよこ組", display_order=1))
            session.add(Classroom(name="うさぎ組", display_order=2))
            session.commit()

        self.app = FastAPI()
        self.app.include_router(home_router, prefix="/plans")
        self.app.include_router(plans_router, prefix="/plans")
        self.app.include_router(documents_router, prefix="/plans")
        self.app.include_router(bunrei_router, prefix="/plans")

        @self.app.get("/healthz")
        def healthz():
            return {"status": "ok"}

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.app.dependency_overrides[plan_docs_auth.get_session] = override_get_session
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def staff_cookies(self):
        return {
            MOCK_ROLE_COOKIE: "admin",
            MOCK_STAFF_NAME_COOKIE: "Test%20Staff",
            MOCK_CALENDAR_USER_COOKIE: str(uuid4()),
        }

    def test_plan_docs_home_uses_main_mock_staff_session(self):
        self.client.cookies.update(self.staff_cookies())
        response = self.client.get("/plans/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("文書作成", response.text)
        self.assertIn("Test Staff", response.text)
        self.assertIn("/plans/annual-plans/new", response.text)
        self.assertNotIn("/staff/session", response.text)

    def test_htmx_post_without_staff_redirects_to_staff_login(self):
        response = self.client.post(
            "/plans/annual-plans",
            data={"school_year": "2026", "classroom_ref": "ひよこ組"},
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertIn("HX-Redirect", response.headers)
        self.assertTrue(response.headers["HX-Redirect"].startswith("/staff/login?redirect="))

    def test_healthz(self):
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
