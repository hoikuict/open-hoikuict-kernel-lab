import unittest
from datetime import date, datetime
from io import BytesIO
from zipfile import ZipFile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from auth import Role, StaffUser
from models import AttendanceRecord, Child, ChildStatus, Classroom
import routers.attendance as attendance_module


class AttendanceReportTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        self.app = FastAPI()
        self.app.include_router(attendance_module.router)

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.current_user = StaffUser(role=Role.ADMIN, name="園長")

        def override_get_current_staff_user():
            return self.current_user

        self.app.dependency_overrides[attendance_module.get_session] = override_get_session
        self.app.dependency_overrides[attendance_module.get_current_staff_user] = override_get_current_staff_user
        self.client = TestClient(self.app)

        with Session(self.engine) as session:
            hiyoko = Classroom(name="ひよこ組", display_order=1)
            usagi = Classroom(name="うさぎ組", display_order=2)
            session.add(hiyoko)
            session.add(usagi)
            session.flush()

            self.hiyoko_id = hiyoko.id
            self.usagi_id = usagi.id

            taro = Child(
                last_name="田中",
                first_name="太郎",
                last_name_kana="タナカ",
                first_name_kana="タロウ",
                birth_date=date(2020, 1, 1),
                enrollment_date=date(2023, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=usagi.id,
            )
            hanako = Child(
                last_name="佐藤",
                first_name="花子",
                last_name_kana="サトウ",
                first_name_kana="ハナコ",
                birth_date=date(2020, 2, 2),
                enrollment_date=date(2023, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=hiyoko.id,
            )
            retired = Child(
                last_name="山田",
                first_name="次郎",
                last_name_kana="ヤマダ",
                first_name_kana="ジロウ",
                birth_date=date(2019, 3, 3),
                enrollment_date=date(2022, 4, 1),
                withdrawal_date=date(2026, 3, 31),
                status=ChildStatus.graduated,
            )
            session.add(taro)
            session.add(hanako)
            session.add(retired)
            session.flush()

            session.add(
                AttendanceRecord(
                    child_id=taro.id,
                    attendance_date=date(2026, 2, 15),
                    check_in_at=datetime(2026, 2, 15, 10, 30),
                    check_out_at=datetime(2026, 2, 15, 16, 0),
                    planned_pickup_time="16:00",
                    pickup_person="母",
                )
            )
            session.add(
                AttendanceRecord(
                    child_id=taro.id,
                    attendance_date=date(2026, 3, 2),
                    check_in_at=datetime(2026, 3, 2, 11, 30),
                    check_out_at=datetime(2026, 3, 2, 17, 5),
                    planned_pickup_time="17:00",
                    pickup_person="父",
                )
            )
            session.add(
                AttendanceRecord(
                    child_id=hanako.id,
                    attendance_date=date(2026, 2, 20),
                    check_in_at=datetime(2026, 2, 20, 11, 0),
                )
            )
            session.add(
                AttendanceRecord(
                    child_id=retired.id,
                    attendance_date=date(2026, 2, 10),
                    check_in_at=datetime(2026, 2, 10, 9, 15),
                    check_out_at=datetime(2026, 2, 10, 14, 45),
                )
            )
            session.commit()

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    @staticmethod
    def _age_on_today(birth_date: date) -> str:
        today = date.today()
        age = today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))
        return f"{age}歳"

    def test_single_day_view_keeps_absent_enrolled_children_and_shows_classrooms(self):
        response = self.client.get("/attendance?date=2026-02-15")

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("検索詳細", html)
        self.assertIn("田中 太郎", html)
        self.assertIn("佐藤 花子", html)
        self.assertIn("うさぎ組", html)
        self.assertIn("ひよこ組", html)
        self.assertIn("未登園 1 名", html)
        self.assertNotIn("山田 次郎", html)

    def test_can_filter_and_sort_by_classroom(self):
        sorted_response = self.client.get("/attendance?date=2026-02-15&sort_by=classroom&sort_order=asc")
        self.assertEqual(sorted_response.status_code, 200)
        sorted_html = sorted_response.text
        self.assertLess(sorted_html.index("佐藤 花子"), sorted_html.index("田中 太郎"))

        filtered_response = self.client.get(
            f"/attendance?start_date=2026-02-01&end_date=2026-03-31&classroom_id={self.usagi_id}"
        )
        self.assertEqual(filtered_response.status_code, 200)
        filtered_html = filtered_response.text
        self.assertIn("田中 太郎", filtered_html)
        self.assertNotIn("佐藤 花子", filtered_html)
        self.assertNotIn("山田 次郎", filtered_html)

    def test_range_filters_and_time_sorting(self):
        response = self.client.get(
            "/attendance?start_date=2026-02-01&end_date=2026-03-31&child_name=太郎"
            "&time_field=check_in&time_from=10:00&time_to=12:00"
            "&sort_by=check_in_at&sort_order=desc"
        )

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("2026-03-02", html)
        self.assertIn("2026-02-15", html)
        self.assertNotIn("佐藤 花子", html)
        self.assertLess(html.index("2026-03-02"), html.index("2026-02-15"))

    def test_csv_export_respects_filters(self):
        response = self.client.get(
            "/attendance/export.csv?start_date=2026-02-01&end_date=2026-03-31&child_name=太郎"
            "&classroom_id={}&time_field=check_in&time_from=10:00&time_to=12:00"
            "&sort_by=check_in_at&sort_order=desc".format(self.usagi_id)
        )

        self.assertEqual(response.status_code, 200)
        csv_text = response.content.decode("utf-8-sig")
        lines = [line for line in csv_text.splitlines() if line]
        self.assertEqual(len(lines), 3)
        expected_age = self._age_on_today(date(2020, 1, 1))
        self.assertIn("日付,園児名,園児名（カナ）,クラス,年齢", lines[0])
        self.assertIn(f"2026-03-02,田中 太郎,タナカ タロウ,うさぎ組,{expected_age},11:30,17:05,降園済み,17:00,父,", lines[1])
        self.assertIn(f"2026-02-15,田中 太郎,タナカ タロウ,うさぎ組,{expected_age},10:30,16:00,降園済み,16:00,母,", lines[2])

    def test_excel_export_returns_valid_xlsx(self):
        response = self.client.get(
            "/attendance/export.xlsx?start_date=2026-02-01&end_date=2026-03-31&child_name=太郎"
            "&classroom_id={}&time_field=check_in&time_from=10:00&time_to=12:00"
            "&sort_by=check_in_at&sort_order=desc".format(self.usagi_id)
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            response.headers["content-type"],
        )

        with ZipFile(BytesIO(response.content)) as archive:
            sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")

        self.assertIn("田中 太郎", sheet_xml)
        self.assertIn("うさぎ組", sheet_xml)
        self.assertIn("2026-03-02", sheet_xml)
        self.assertIn("2026-02-15", sheet_xml)

    def test_non_admin_cannot_export_attendance(self):
        self.current_user = StaffUser(role=Role.CAN_EDIT, name="一般職員")

        response = self.client.get("/attendance/export.csv?date=2026-02-15", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertIn("/attendance?", response.headers["location"])
        self.assertIn("date=2026-02-15", response.headers["location"])
        self.assertIn("notice=export_admin_required", response.headers["location"])

        notice_response = self.client.get(response.headers["location"])
        self.assertEqual(notice_response.status_code, 200)
        self.assertIn("CSV/Excel出力は管理者のみ利用できます。", notice_response.text)

    def test_non_admin_attendance_list_hides_export_links(self):
        self.current_user = StaffUser(role=Role.CAN_EDIT, name="一般職員")

        response = self.client.get("/attendance?date=2026-02-15")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("/attendance/export.csv", response.text)
        self.assertNotIn("/attendance/export.xlsx", response.text)

    def test_invalid_date_is_rejected(self):
        response = self.client.get("/attendance?date=not-a-date")

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
