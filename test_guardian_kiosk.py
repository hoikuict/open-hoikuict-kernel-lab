import unittest
from datetime import date, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from models import AttendanceRecord, Child, ChildStatus, Classroom
import routers.guardian as guardian_module
from testing_helpers import configure_test_environment


class GuardianKioskTests(unittest.TestCase):
    def setUp(self):
        configure_test_environment()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        self.app = FastAPI()
        self.app.include_router(guardian_module.router)

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.app.dependency_overrides[guardian_module.get_session] = override_get_session
        self.client = TestClient(self.app)

        with Session(self.engine) as session:
            classroom = Classroom(name="ひよこ組", display_order=1)
            session.add(classroom)
            session.flush()
            child = Child(
                last_name="田中",
                first_name="太郎",
                last_name_kana="タナカ",
                first_name_kana="タロウ",
                birth_date=date(2020, 1, 1),
                enrollment_date=date(2023, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=classroom.id,
            )
            session.add(child)
            session.flush()
            self.classroom_id = classroom.id
            self.child_id = child.id
            session.commit()

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_default_date_uses_local_today(self):
        original_local_today = guardian_module.local_today
        guardian_module.local_today = lambda: date(2026, 7, 5)
        try:
            response = self.client.get("/guardian/")
        finally:
            guardian_module.local_today = original_local_today

        self.assertEqual(response.status_code, 200)
        self.assertIn('value="2026-07-05"', response.text)

    def test_pickup_form_shows_button_choices_and_snack_checkbox(self):
        with Session(self.engine) as session:
            session.add(
                AttendanceRecord(
                    child_id=self.child_id,
                    attendance_date=date(2026, 7, 5),
                    check_in_at=datetime(2026, 7, 5, 8, 30),
                )
            )
            session.commit()

        response = self.client.get(
            f"/guardian/?date=2026-07-05&class_id={self.classroom_id}&child_id={self.child_id}"
        )

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('data-pickup-hour="07"', html)
        self.assertIn('data-pickup-hour="21"', html)
        self.assertIn('data-pickup-minute="15"', html)
        self.assertIn('data-pickup-person="母"', html)
        self.assertIn('data-pickup-person="ファミリーサポート"', html)
        self.assertIn('name="snack_required"', html)

    def test_pickup_form_is_hidden_after_pickup_plan_is_saved(self):
        with Session(self.engine) as session:
            session.add(
                AttendanceRecord(
                    child_id=self.child_id,
                    attendance_date=date(2026, 7, 5),
                    check_in_at=datetime(2026, 7, 5, 8, 30),
                    planned_pickup_time="18:15",
                    pickup_person="母",
                    snack_required=True,
                )
            )
            session.commit()

        response = self.client.get(
            f"/guardian/?date=2026-07-05&class_id={self.classroom_id}&child_id={self.child_id}"
        )

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertNotIn(f'action="/guardian/child/{self.child_id}/pickup"', html)
        self.assertNotIn('data-pickup-hour="07"', html)
        self.assertIn("降園する", html)
        self.assertIn("18:15", html)
        self.assertIn("母", html)

    def test_check_in_uses_local_naive_now(self):
        fixed_now = datetime(2026, 7, 5, 8, 45)
        original_local_naive_now = guardian_module.local_naive_now
        guardian_module.local_naive_now = lambda: fixed_now
        try:
            response = self.client.post(
                f"/guardian/child/{self.child_id}/check-in",
                data={"date": "2026-07-05", "class_id": str(self.classroom_id)},
                follow_redirects=False,
            )
        finally:
            guardian_module.local_naive_now = original_local_naive_now

        self.assertEqual(response.status_code, 303)
        with Session(self.engine) as session:
            record = session.exec(select(AttendanceRecord)).one()
            self.assertEqual(record.check_in_at, fixed_now)

    def test_pickup_commit_saves_pickup_buttons_and_snack_flag(self):
        with Session(self.engine) as session:
            session.add(
                AttendanceRecord(
                    child_id=self.child_id,
                    attendance_date=date(2026, 7, 5),
                    check_in_at=datetime(2026, 7, 5, 8, 30),
                )
            )
            session.commit()

        response = self.client.post(
            f"/guardian/child/{self.child_id}/pickup/commit",
            data={
                "date": "2026-07-05",
                "class_id": str(self.classroom_id),
                "planned_pickup_time": "18:15",
                "pickup_person": "ファミリーサポート",
                "snack_required": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        with Session(self.engine) as session:
            record = session.exec(select(AttendanceRecord)).one()
            self.assertEqual(record.planned_pickup_time, "18:15")
            self.assertEqual(record.pickup_person, "ファミリーサポート")
            self.assertTrue(record.snack_required)


if __name__ == "__main__":
    unittest.main()
