import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from auth import (
    MOCK_CALENDAR_USER_COOKIE,
    MOCK_CHILD_RECORDS_PERMISSION_COOKIE,
    MOCK_ROLE_COOKIE,
    MOCK_STAFF_NAME_COOKIE,
)
import database
from models import USER_SOURCE_LOCAL_SAMPLE, USER_SOURCE_MANUAL, USER_SOURCE_WEB_DEMO, User
import routers.staff_auth as staff_auth_module
from testing_helpers import configure_test_environment


class StaffAuthRouterTests(unittest.TestCase):
    def setUp(self):
        configure_test_environment()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        self.app = FastAPI()
        self.app.include_router(staff_auth_module.router)
        self.app.include_router(staff_auth_module.mock_login_router)

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.app.dependency_overrides[staff_auth_module.get_session] = override_get_session
        self.client = TestClient(self.app)

        with Session(self.engine) as session:
            self.principal = User(
                email="principal@example.com",
                display_name="園長",
                staff_role="admin",
                staff_sort_order=10,
                is_calendar_admin=True,
            )
            self.part_timer = User(
                email="part@example.com",
                display_name="早番パート",
                staff_role="view_only",
                staff_sort_order=150,
                is_calendar_admin=False,
            )
            self.external_user = User(
                email="external@example.com",
                display_name="外部確認用",
                staff_role="view_only",
                staff_sort_order=220,
                is_calendar_admin=False,
            )
            session.add(self.principal)
            session.add(self.part_timer)
            session.add(self.external_user)
            session.commit()
            self.principal_id = self.principal.id

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def _login_admin(self):
        response = self.client.post(
            "/staff/login",
            data={"user_id": str(self.principal_id), "redirect_to": "/staff/users"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def test_login_page_renders_staff_cards(self):
        response = self.client.get("/staff/login?redirect=/staff-rooms/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/staff/login"', response.text)
        self.assertIn('name="redirect_to" value="/children"', response.text)
        self.assertEqual(response.text.count('name="user_id"'), 2)
        self.assertIn("職員ログイン", response.text)
        self.assertIn("未ログイン", response.text)
        self.assertIn("職員を選択する", response.text)
        self.assertLess(response.text.index("職員を選択する"), response.text.index("基本業務"))
        self.assertIn("園長", response.text)
        self.assertIn("早番パート", response.text)
        self.assertNotIn("外部確認用", response.text)
        self.assertIn("管理者", response.text)
        self.assertIn("閲覧のみ", response.text)
        self.assertIn("園児台帳管理", response.text)

    def test_login_sets_staff_and_calendar_cookies_and_redirects(self):
        response = self.client.post(
            "/staff/login",
            data={"user_id": str(self.principal_id), "redirect_to": "/staff-rooms/"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/children")
        set_cookie = response.headers.get("set-cookie", "")
        self.assertIn(f"{MOCK_ROLE_COOKIE}=admin", set_cookie)
        self.assertIn(f"{MOCK_STAFF_NAME_COOKIE}=", set_cookie)
        self.assertIn(f"{MOCK_CALENDAR_USER_COOKIE}=", set_cookie)
        self.assertIn(f"{MOCK_CHILD_RECORDS_PERMISSION_COOKIE}=1", set_cookie)

    def test_logout_clears_staff_and_calendar_cookies(self):
        self.client.post(
            "/staff/login",
            data={"user_id": str(self.principal_id), "redirect_to": "/staff-rooms/"},
            follow_redirects=False,
        )

        response = self.client.post(
            "/staff/logout",
            data={"redirect_to": "/staff/login"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/staff/login")
        set_cookie = response.headers.get("set-cookie", "")
        self.assertIn(f"{MOCK_ROLE_COOKIE}=", set_cookie)
        self.assertIn(f"{MOCK_STAFF_NAME_COOKIE}=", set_cookie)
        self.assertIn(f"{MOCK_CALENDAR_USER_COOKIE}=", set_cookie)
        self.assertIn(f"{MOCK_CHILD_RECORDS_PERMISSION_COOKIE}=", set_cookie)

        login_page = self.client.get("/staff/login")
        self.assertEqual(login_page.status_code, 200)
        self.assertIn("未ログイン", login_page.text)
        self.assertNotIn("ログイン中</p>\n          <p class=\"mt-2 text-base font-semibold\">園長", login_page.text)

    def test_seed_calendar_data_restores_full_staff_users(self):
        original_engine = database.engine
        database.engine = self.engine
        try:
            database.seed_calendar_data()
        finally:
            database.engine = original_engine

        with Session(self.engine) as session:
            staff_users = session.exec(
                select(User)
                .where(User.is_active.is_(True), User.staff_sort_order < 200)
                .order_by(User.staff_sort_order, User.display_name)
            ).all()

        self.assertEqual(len(staff_users), 19)
        names = [user.display_name for user in staff_users]
        self.assertIn("看護師", names)
        self.assertIn("ぞう組担任B", names)
        self.assertIn("早番パート", names)
        self.assertIn("遅番パート", names)
        office = next(user for user in staff_users if user.display_name == "事務")
        self.assertTrue(office.can_manage_child_records)
        self.assertTrue(all(user.provisioning_source == USER_SOURCE_LOCAL_SAMPLE for user in staff_users))

    def test_seed_calendar_data_skips_local_staff_when_web_demo_users_exist(self):
        with Session(self.engine) as session:
            session.add_all(
                [
                    User(
                        email="principal@demo.open-hoikuict.example",
                        display_name="園長",
                        staff_role="admin",
                        staff_sort_order=10,
                        provisioning_source=USER_SOURCE_WEB_DEMO,
                        is_calendar_admin=True,
                    ),
                    User(
                        email="chief@demo.open-hoikuict.example",
                        display_name="主任",
                        staff_role="admin",
                        staff_sort_order=20,
                        provisioning_source=USER_SOURCE_WEB_DEMO,
                        is_calendar_admin=True,
                    ),
                    User(
                        email="chief@example.com",
                        display_name="主任",
                        staff_role="admin",
                        staff_sort_order=20,
                        provisioning_source=USER_SOURCE_LOCAL_SAMPLE,
                        is_calendar_admin=True,
                    ),
                ]
            )
            session.commit()

        original_engine = database.engine
        database.engine = self.engine
        try:
            database.seed_calendar_data()
        finally:
            database.engine = original_engine

        with Session(self.engine) as session:
            local_principal = session.exec(
                select(User).where(User.email == "principal@example.com")
            ).first()
            local_chief = session.exec(
                select(User).where(User.email == "chief@example.com")
            ).first()
            demo_principal = session.exec(
                select(User).where(User.email == "principal@demo.open-hoikuict.example")
            ).first()

        self.assertEqual(local_principal.provisioning_source, USER_SOURCE_MANUAL)
        self.assertIsNotNone(local_chief)
        self.assertFalse(local_chief.is_active)
        self.assertIsNotNone(demo_principal)
        self.assertEqual(demo_principal.provisioning_source, USER_SOURCE_WEB_DEMO)

    def test_admin_can_create_staff_user_with_child_record_permission(self):
        self._login_admin()

        response = self.client.post(
            "/staff/users",
            data={
                "display_name": "台帳担当",
                "email": "records@example.com",
                "staff_role": "can_edit",
                "can_manage_child_records": "1",
                "staff_sort_order": "45",
                "is_active": "1",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/staff/users")
        with Session(self.engine) as session:
            user = session.exec(select(User).where(User.email == "records@example.com")).first()
        self.assertIsNotNone(user)
        self.assertEqual(user.staff_role, "can_edit")
        self.assertTrue(user.can_manage_child_records)
        self.assertEqual(user.provisioning_source, USER_SOURCE_MANUAL)

    def test_admin_can_filter_staff_users_by_source(self):
        with Session(self.engine) as session:
            session.add(
                User(
                    email="principal@demo.open-hoikuict.example",
                    display_name="デモ園長",
                    staff_role="admin",
                    staff_sort_order=10,
                    provisioning_source=USER_SOURCE_WEB_DEMO,
                    is_calendar_admin=True,
                )
            )
            session.commit()

        self._login_admin()

        response = self.client.get("/staff/users")
        self.assertEqual(response.status_code, 200)
        self.assertIn("手動追加", response.text)
        self.assertIn("WEB公開デモ", response.text)
        self.assertIn("デモ園長", response.text)
        self.assertNotIn("早番パート", response.text)

        all_response = self.client.get("/staff/users?source=all")
        self.assertEqual(all_response.status_code, 200)
        self.assertIn("デモ園長", all_response.text)
        self.assertIn("早番パート", all_response.text)

        filtered_response = self.client.get("/staff/users?source=web_demo")
        self.assertEqual(filtered_response.status_code, 200)
        self.assertIn("デモ園長", filtered_response.text)
        self.assertNotIn("早番パート", filtered_response.text)

    def test_admin_cannot_remove_own_last_admin_permission(self):
        self._login_admin()

        response = self.client.post(
            f"/staff/users/{self.principal_id}/edit",
            data={
                "display_name": "園長",
                "email": "principal@example.com",
                "staff_role": "can_edit",
                "staff_sort_order": "10",
                "is_active": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("自分自身の管理者権限はこの画面では外せません。", response.text)


if __name__ == "__main__":
    unittest.main()
