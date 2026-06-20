import unittest
from datetime import date, datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from auth import Role, StaffUser
from extended_care_fee_service import (
    adjust_charge,
    calculate_charge,
    recalculate_attendance_charge,
    recalculate_period,
)
from models import (
    AttendanceRecord,
    Child,
    ChildStatus,
    Classroom,
    ExtendedCareCharge,
    ExtendedCareChargeStatus,
    ExtendedCareFeeRule,
)
import routers.attendance as attendance_module
import routers.extended_care_fees as extended_care_fees_module


class ExtendedCareFeeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        self.app = FastAPI()
        self.app.include_router(attendance_module.router)
        self.app.include_router(extended_care_fees_module.router)

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.current_user = StaffUser(role=Role.CAN_EDIT, name="料金担当")

        def override_get_current_staff_user():
            return self.current_user

        self.app.dependency_overrides[attendance_module.get_session] = override_get_session
        self.app.dependency_overrides[extended_care_fees_module.get_session] = override_get_session
        self.app.dependency_overrides[attendance_module.get_current_staff_user] = override_get_current_staff_user
        self.app.dependency_overrides[
            extended_care_fees_module.get_current_staff_user
        ] = override_get_current_staff_user
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
                birth_date=date(2021, 4, 1),
                enrollment_date=date(2024, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=classroom.id,
            )
            rule = ExtendedCareFeeRule(
                name="標準延長保育料",
                effective_from=date(2026, 1, 1),
                start_time="18:00",
                grace_minutes=5,
                rounding_minutes=15,
                unit_price=100,
                is_active=True,
            )
            session.add(child)
            session.add(rule)
            session.commit()
            self.child_id = child.id
            self.rule_id = rule.id

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def _rule(self):
        with Session(self.engine) as session:
            return session.get(ExtendedCareFeeRule, self.rule_id)

    def test_calculates_by_grace_and_rounding_unit(self):
        rule = self._rule()
        examples = [
            (datetime(2026, 3, 2, 18, 4), 0, 0),
            (datetime(2026, 3, 2, 18, 6), 15, 100),
            (datetime(2026, 3, 2, 18, 20), 15, 100),
            (datetime(2026, 3, 2, 18, 21), 30, 200),
        ]

        for checkout_at, expected_minutes, expected_amount in examples:
            record = AttendanceRecord(
                child_id=self.child_id,
                attendance_date=date(2026, 3, 2),
                check_out_at=checkout_at,
            )
            computed = calculate_charge(record, rule)
            self.assertEqual(computed.extended_minutes, expected_minutes)
            self.assertEqual(computed.auto_amount, expected_amount)

    def test_checkout_creates_extended_care_charge(self):
        with Session(self.engine) as session:
            session.add(
                AttendanceRecord(
                    child_id=self.child_id,
                    attendance_date=date(2026, 3, 2),
                    check_in_at=datetime(2026, 3, 2, 9, 0),
                )
            )
            session.commit()

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 3, 2, 18, 6)

        with patch.object(attendance_module, "datetime", FixedDateTime):
            response = self.client.post(
                f"/attendance/{self.child_id}/check-out",
                data={"date": "2026-03-02"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        with Session(self.engine) as session:
            charge = session.exec(select(ExtendedCareCharge)).one()
            self.assertEqual(charge.final_amount, 100)
            self.assertEqual(charge.extended_minutes, 15)
            self.assertEqual(charge.status, ExtendedCareChargeStatus.draft)

    def test_manual_adjusted_charge_is_not_overwritten_by_normal_recalculation(self):
        with Session(self.engine) as session:
            record = AttendanceRecord(
                child_id=self.child_id,
                attendance_date=date(2026, 3, 2),
                check_in_at=datetime(2026, 3, 2, 9, 0),
                check_out_at=datetime(2026, 3, 2, 18, 21),
            )
            session.add(record)
            session.flush()
            charge = recalculate_attendance_charge(session, record)
            adjust_charge(charge, 50, "園判断の加算", "料金担当")
            session.add(charge)
            session.commit()
            charge_id = charge.id

        with Session(self.engine) as session:
            record = session.exec(select(AttendanceRecord)).one()
            record.check_out_at = datetime(2026, 3, 2, 18, 45)
            session.add(record)
            recalculate_attendance_charge(session, record)
            session.commit()

            charge = session.get(ExtendedCareCharge, charge_id)
            self.assertEqual(charge.status, ExtendedCareChargeStatus.manual_adjusted)
            self.assertEqual(charge.auto_amount, 200)
            self.assertEqual(charge.final_amount, 250)

    def test_monthly_screen_and_csv_show_summary(self):
        with Session(self.engine) as session:
            record = AttendanceRecord(
                child_id=self.child_id,
                attendance_date=date(2026, 3, 2),
                check_in_at=datetime(2026, 3, 2, 9, 0),
                check_out_at=datetime(2026, 3, 2, 18, 6),
            )
            session.add(record)
            session.flush()
            recalculate_attendance_charge(session, record)
            session.commit()

        response = self.client.get("/extended-care-fees/?month=2026-03")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("田中 太郎", html)
        self.assertIn("100 円", html)
        self.assertIn("要確認", html)

        csv_response = self.client.get("/extended-care-fees/export.csv?month=2026-03")
        self.assertEqual(csv_response.status_code, 200)
        csv_text = csv_response.content.decode("utf-8-sig")
        self.assertIn("対象月,園児ID,園児名,園児名カナ,クラス", csv_text)
        self.assertIn("2026-03", csv_text)
        self.assertIn("田中 太郎", csv_text)
        self.assertIn(",100,0,100,1", csv_text)

    def test_settings_can_create_rule_and_reject_active_overlap(self):
        overlap_response = self.client.post(
            "/extended-care-fees/settings",
            data={
                "name": "重複ルール",
                "effective_from": "2026-02-01",
                "effective_to": "",
                "start_time": "18:30",
                "grace_minutes": "0",
                "rounding_minutes": "30",
                "unit_price": "200",
                "daily_cap_amount": "",
                "is_active": "1",
            },
        )
        self.assertEqual(overlap_response.status_code, 400)
        self.assertIn("適用期間が重複", overlap_response.text)

        create_response = self.client.post(
            "/extended-care-fees/settings",
            data={
                "name": "検証用無効ルール",
                "effective_from": "2026-02-01",
                "effective_to": "",
                "start_time": "18:30",
                "grace_minutes": "0",
                "rounding_minutes": "30",
                "unit_price": "200",
                "daily_cap_amount": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(create_response.status_code, 303)
        with Session(self.engine) as session:
            rules = session.exec(select(ExtendedCareFeeRule)).all()
            self.assertEqual(len(rules), 2)
            created = [rule for rule in rules if rule.name == "検証用無効ルール"][0]
            self.assertFalse(created.is_active)

    def test_view_only_staff_cannot_export_or_recalculate(self):
        self.current_user = StaffUser(role=Role.VIEW_ONLY, name="閲覧担当")

        export_response = self.client.get("/extended-care-fees/export.csv?month=2026-03")
        self.assertEqual(export_response.status_code, 403)

        recalc_response = self.client.post(
            "/extended-care-fees/recalculate",
            data={"month": "2026-03"},
            follow_redirects=False,
        )
        self.assertEqual(recalc_response.status_code, 403)

    def test_recalculate_period_counts_only_changed_charges(self):
        with Session(self.engine) as session:
            record = AttendanceRecord(
                child_id=self.child_id,
                attendance_date=date(2026, 3, 2),
                check_in_at=datetime(2026, 3, 2, 9, 0),
                check_out_at=datetime(2026, 3, 2, 18, 6),
            )
            session.add(record)
            session.flush()
            recalculate_attendance_charge(session, record)
            session.commit()

            first_count = recalculate_period(session, date(2026, 3, 1), date(2026, 3, 31))
            record.check_out_at = datetime(2026, 3, 2, 18, 21)
            session.add(record)
            changed_count = recalculate_period(session, date(2026, 3, 1), date(2026, 3, 31))

        self.assertEqual(first_count, 0)
        self.assertEqual(changed_count, 1)


if __name__ == "__main__":
    unittest.main()
