import unittest
from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from auth import Role, StaffUser
from models import (
    AttendanceAlarmHistory,
    AttendanceAlarmState,
    AttendanceVerification,
    AttendanceVerificationHistory,
    Child,
    ChildStatus,
    Classroom,
    DailyContactEntry,
    ParentAccount,
    ParentContactType,
)
import routers.attendance_checks as attendance_checks_module


class AttendanceChecksTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        self.app = FastAPI()
        self.app.include_router(attendance_checks_module.router)

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.current_user = StaffUser(role=Role.CAN_EDIT, name="確認担当")

        def override_get_current_staff_user():
            return self.current_user

        self.app.dependency_overrides[attendance_checks_module.get_session] = override_get_session
        self.app.dependency_overrides[attendance_checks_module.get_current_staff_user] = override_get_current_staff_user

        self.client = TestClient(self.app)
        self.day = date(2026, 3, 22)

        with Session(self.engine) as session:
            classroom = Classroom(name="ひまわり組", display_order=1)
            session.add(classroom)
            session.flush()

            child = Child(
                last_name="田中",
                first_name="太郎",
                last_name_kana="タナカ",
                first_name_kana="タロウ",
                birth_date=date(2021, 4, 1),
                enrollment_date=date(2024, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=classroom.id,
            )
            parent = ParentAccount(
                display_name="田中 保護者",
                email="tanaka-parent@example.com",
            )
            session.add(child)
            session.add(parent)
            session.commit()

            self.child_id = child.id
            self.parent_id = parent.id

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_editor_can_update_attendance_check(self):
        response = self.client.post(
            f"/attendance-checks/{self.child_id}/verification",
            data={
                "date": self.day.isoformat(),
                "status": "present",
                "layout": "flat",
                "filter": "all",
                "classroom_id": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        with Session(self.engine) as session:
            verification = session.exec(select(AttendanceVerification)).first()
            history = session.exec(select(AttendanceVerificationHistory)).all()

        self.assertIsNotNone(verification)
        self.assertEqual(verification.status.value, "present")
        self.assertEqual(verification.updated_by_name, "確認担当")
        self.assertEqual(len(history), 1)

    def test_htmx_update_returns_partial_and_keeps_operator_history(self):
        first_response = self.client.post(
            f"/attendance-checks/{self.child_id}/verification",
            headers={"HX-Request": "true"},
            data={
                "date": self.day.isoformat(),
                "status": "present",
                "layout": "flat",
                "filter": "all",
                "classroom_id": "",
            },
        )
        second_response = self.client.post(
            f"/attendance-checks/{self.child_id}/verification",
            headers={"HX-Request": "true"},
            data={
                "date": self.day.isoformat(),
                "status": "present",
                "layout": "flat",
                "filter": "all",
                "classroom_id": "",
            },
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertIn('id="attendance-checks-board"', second_response.text)
        self.assertIn("確認担当", second_response.text)
        self.assertIn("data-history-status=", second_response.text)

        with Session(self.engine) as session:
            verification = session.exec(select(AttendanceVerification)).first()
            histories = session.exec(
                select(AttendanceVerificationHistory).order_by(AttendanceVerificationHistory.id)
            ).all()

        self.assertIsNotNone(verification)
        self.assertEqual(verification.updated_by_name, "確認担当")
        self.assertEqual(len(histories), 2)
        self.assertTrue(all(history.updated_by_name == "確認担当" for history in histories))

    def test_list_shows_compact_summary_row_and_detail_toggle(self):
        with Session(self.engine) as session:
            session.add(
                DailyContactEntry(
                    child_id=self.child_id,
                    parent_account_id=self.parent_id,
                    target_date=self.day,
                    contact_type=ParentContactType.absent_sick,
                    absence_temperature="38.2",
                    absence_symptoms="発熱",
                    absence_note="受診予定",
                )
            )
            session.commit()

        response = self.client.get(f"/attendance-checks/?date={self.day.isoformat()}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("詳細表示", response.text)
        self.assertIn(f'aria-controls="attendance-check-detail-{self.child_id}"', response.text)
        self.assertIn('data-status-key="present"', response.text)
        self.assertIn('data-status-key="private_absent"', response.text)
        self.assertIn('data-status-key="sick_absent"', response.text)
        self.assertIn('data-status-key="unknown"', response.text)
        self.assertIn("病欠", response.text)

    def test_view_only_staff_cannot_update_attendance_check(self):
        self.current_user = StaffUser(role=Role.VIEW_ONLY, name="閲覧担当")

        response = self.client.post(
            f"/attendance-checks/{self.child_id}/verification",
            data={
                "date": self.day.isoformat(),
                "status": "present",
                "layout": "flat",
                "filter": "all",
                "classroom_id": "",
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_alarm_is_not_recalculated_by_list_get(self):
        response = self.client.post(
            f"/attendance-checks/{self.child_id}/verification",
            data={
                "date": self.day.isoformat(),
                "status": "private_absent",
                "layout": "flat",
                "filter": "all",
                "classroom_id": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        with Session(self.engine) as session:
            alarm_state = session.exec(select(AttendanceAlarmState)).first()
            self.assertIsNotNone(alarm_state)
            self.assertTrue(alarm_state.is_active)
            self.assertEqual(alarm_state.reasons, ["no_contact_and_not_present"])

            session.add(
                DailyContactEntry(
                    child_id=self.child_id,
                    parent_account_id=self.parent_id,
                    target_date=self.day,
                    contact_type=ParentContactType.absent_private,
                    absence_note="私用のため欠席",
                )
            )
            session.commit()

        refresh_response = self.client.get(f"/attendance-checks/?date={self.day.isoformat()}")
        self.assertEqual(refresh_response.status_code, 200)

        with Session(self.engine) as session:
            alarm_state = session.exec(select(AttendanceAlarmState)).first()
            alarm_history = session.exec(select(AttendanceAlarmHistory)).all()

        self.assertTrue(alarm_state.is_active)
        self.assertEqual(len(alarm_history), 1)

    def test_invalid_date_is_rejected_without_creating_verification(self):
        response = self.client.post(
            f"/attendance-checks/{self.child_id}/verification",
            data={
                "date": "not-a-date",
                "status": "present",
                "layout": "flat",
                "filter": "all",
                "classroom_id": "",
            },
        )

        self.assertEqual(response.status_code, 400)
        with Session(self.engine) as session:
            verification = session.exec(select(AttendanceVerification)).first()
        self.assertIsNone(verification)


if __name__ == "__main__":
    unittest.main()
