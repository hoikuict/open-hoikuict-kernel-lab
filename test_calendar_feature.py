import unittest
import json
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

import routers.calendar as calendar_module
from testing_helpers import configure_test_environment
from models import (
    Calendar,
    CalendarActivityKind,
    CalendarActivityLog,
    CalendarMember,
    CalendarMemberRole,
    CalendarType,
    CalendarUserPreference,
    Event,
    EventKind,
    EventVisibility,
    NotificationJob,
    Reminder,
    User,
)


class CalendarFeatureTests(unittest.TestCase):
    def setUp(self):
        configure_test_environment()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        self.app = FastAPI()
        self.app.include_router(calendar_module.router)
        self.app.include_router(calendar_module.mock_login_router)

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.app.dependency_overrides[calendar_module.get_session] = override_get_session
        self.client = TestClient(self.app)

        with Session(self.engine) as session:
            self.user_a = User(
                email="calendar-a@example.com",
                display_name="田中先生",
                timezone="Asia/Tokyo",
                staff_role="admin",
                staff_sort_order=10,
                is_calendar_admin=True,
            )
            self.user_b = User(
                email="calendar-b@example.com",
                display_name="佐藤先生",
                timezone="Asia/Tokyo",
                staff_role="can_edit",
                staff_sort_order=20,
                is_calendar_admin=False,
            )
            session.add(self.user_a)
            session.add(self.user_b)
            session.flush()

            self.a_personal = Calendar(
                owner_user_id=self.user_a.id,
                name="田中先生の個人カレンダー",
                calendar_type=CalendarType.staff_personal,
                color="#2563EB",
                is_primary=True,
            )
            self.b_personal = Calendar(
                owner_user_id=self.user_b.id,
                name="佐藤先生の個人カレンダー",
                calendar_type=CalendarType.staff_personal,
                color="#DC2626",
                is_primary=True,
            )
            self.shared_calendar = Calendar(
                owner_user_id=self.user_a.id,
                name="施設共用カレンダー",
                calendar_type=CalendarType.facility_shared,
                color="#059669",
                description="全職員が使う共有カレンダー",
            )
            session.add(self.a_personal)
            session.add(self.b_personal)
            session.add(self.shared_calendar)
            session.flush()

            self.user_a.default_calendar_id = self.a_personal.id
            self.user_b.default_calendar_id = self.b_personal.id
            session.add(self.user_a)
            session.add(self.user_b)

            session.add(CalendarMember(calendar_id=self.a_personal.id, user_id=self.user_a.id, role=CalendarMemberRole.owner))
            session.add(CalendarMember(calendar_id=self.b_personal.id, user_id=self.user_b.id, role=CalendarMemberRole.owner))
            session.add(CalendarMember(calendar_id=self.shared_calendar.id, user_id=self.user_a.id, role=CalendarMemberRole.owner))
            session.add(CalendarMember(calendar_id=self.shared_calendar.id, user_id=self.user_b.id, role=CalendarMemberRole.editor))

            session.add(CalendarUserPreference(calendar_id=self.a_personal.id, user_id=self.user_a.id, is_visible=True, display_order=10))
            session.add(CalendarUserPreference(calendar_id=self.shared_calendar.id, user_id=self.user_a.id, is_visible=True, display_order=30))
            session.add(CalendarUserPreference(calendar_id=self.b_personal.id, user_id=self.user_b.id, is_visible=True, display_order=10))
            session.add(CalendarUserPreference(calendar_id=self.shared_calendar.id, user_id=self.user_b.id, is_visible=True, display_order=30))
            session.commit()
            self.user_a_id = self.user_a.id
            self.user_b_id = self.user_b.id
            self.a_personal_id = self.a_personal.id
            self.b_personal_id = self.b_personal.id
            self.shared_calendar_id = self.shared_calendar.id

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def _login(self, user_id):
        response = self.client.post(
            "/session/mock-login",
            data={"user_id": str(user_id), "redirect_to": "/calendar"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def test_mock_login_and_calendar_page_render(self):
        self._login(self.user_a_id)

        response = self.client.get("/calendar")

        self.assertEqual(response.status_code, 200)
        self.assertIn("田中先生の個人カレンダー", response.text)
        self.assertIn("施設共用カレンダー", response.text)
        self.assertNotIn("佐藤先生の個人カレンダー", response.text)
        self.assertIn("カレンダー権限: 管理者", response.text)

    def test_calendar_websocket_authenticates_and_releases_request_session(self):
        self._login(self.user_a_id)
        with self.client.websocket_connect(
            f"/ws/calendars/{self.a_personal_id}"
        ) as websocket:
            websocket.send_text("ping")

        response = self.client.get("/calendar")
        self.assertEqual(response.status_code, 200)

    def test_admin_user_can_see_create_form_with_color_palette(self):
        self._login(self.user_a_id)

        response = self.client.get("/calendar")

        self.assertEqual(response.status_code, 200)
        self.assertIn("カレンダー作成", response.text)
        self.assertIn('type="radio"', response.text)
        self.assertIn('value="#2563EB"', response.text)
        self.assertNotIn('type="text" name="color"', response.text)

    def test_non_admin_user_sees_admin_only_message_for_calendar_creation(self):
        self._login(self.user_b_id)

        response = self.client.get("/calendar")

        self.assertEqual(response.status_code, 200)
        self.assertIn("新しいカレンダーを作成できるのは管理者のみです。", response.text)
        self.assertNotIn('name="calendar_type"', response.text)

    def test_visibility_toggle_is_user_specific(self):
        self._login(self.user_a_id)
        response = self.client.post(
            f"/calendar/preferences/{self.shared_calendar_id}/visibility",
            data={"mode": "month", "anchor_date": "2026-04-12", "is_visible": "false"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        with Session(self.engine) as session:
            a_pref = session.exec(
                select(CalendarUserPreference).where(
                    CalendarUserPreference.calendar_id == self.shared_calendar_id,
                    CalendarUserPreference.user_id == self.user_a_id,
                )
            ).first()
            b_pref = session.exec(
                select(CalendarUserPreference).where(
                    CalendarUserPreference.calendar_id == self.shared_calendar_id,
                    CalendarUserPreference.user_id == self.user_b_id,
                )
            ).first()

        self.assertFalse(a_pref.is_visible)
        self.assertTrue(b_pref.is_visible)

    def test_creating_facility_shared_calendar_adds_all_staff(self):
        self._login(self.user_a_id)
        response = self.client.post(
            "/calendars",
            data={
                "name": "行事共有カレンダー",
                "calendar_type": CalendarType.facility_shared.value,
                "description": "行事共有",
                "color": "#0EA5E9",
                "mode": "month",
                "anchor_date": "2026-04-12",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        with Session(self.engine) as session:
            calendar = session.exec(select(Calendar).where(Calendar.name == "行事共有カレンダー")).first()
            members = session.exec(
                select(CalendarMember).where(CalendarMember.calendar_id == calendar.id)
            ).all()

        self.assertEqual(calendar.calendar_type, CalendarType.facility_shared)
        self.assertEqual(
            {member.user_id: member.role for member in members},
            {
                self.user_a_id: CalendarMemberRole.owner,
                self.user_b_id: CalendarMemberRole.editor,
            },
        )

    def test_htmx_calendar_creation_updates_shell_and_new_event_form(self):
        self._login(self.user_a_id)
        response = self.client.post(
            "/calendars",
            headers={"HX-Request": "true", "HX-Target": "calendar-shell"},
            data={
                "name": "面談共有カレンダー",
                "calendar_type": CalendarType.facility_shared.value,
                "description": "面談用",
                "color": "#0891B2",
                "mode": "month",
                "anchor_date": "2026-04-12",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="calendar-shell"', response.text)
        self.assertIn("面談共有カレンダー", response.text)

        modal_response = self.client.get("/events/new?mode=month&date=2026-04-12")
        self.assertEqual(modal_response.status_code, 200)
        self.assertIn("面談共有カレンダー", modal_response.text)
        self.assertIn('id="event-start-value"', modal_response.text)
        self.assertIn('id="event-end-value"', modal_response.text)
        self.assertIn("data-event-start", modal_response.text)
        self.assertIn("data-event-end", modal_response.text)

    def test_htmx_visibility_toggle_returns_shell_and_respects_state(self):
        self._login(self.user_a_id)
        response = self.client.post(
            f"/calendar/preferences/{self.shared_calendar_id}/visibility",
            headers={"HX-Request": "true", "HX-Target": "calendar-shell"},
            data={"mode": "month", "anchor_date": "2026-04-12", "is_visible": "false"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="calendar-shell"', response.text)

        with Session(self.engine) as session:
            preference = session.exec(
                select(CalendarUserPreference).where(
                    CalendarUserPreference.calendar_id == self.shared_calendar_id,
                    CalendarUserPreference.user_id == self.user_a_id,
                )
            ).first()

        self.assertFalse(preference.is_visible)

    def test_non_admin_user_cannot_create_calendar(self):
        self._login(self.user_b_id)
        response = self.client.post(
            "/calendars",
            data={
                "name": "佐藤先生の追加カレンダー",
                "calendar_type": CalendarType.staff_personal.value,
                "description": "作成不可の確認",
                "color": "#059669",
                "mode": "month",
                "anchor_date": "2026-04-12",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)
        with Session(self.engine) as session:
            created = session.exec(select(Calendar).where(Calendar.name == "佐藤先生の追加カレンダー")).first()
        self.assertIsNone(created)

    def test_event_creation_on_shared_calendar_creates_creator_only_jobs(self):
        self._login(self.user_a_id)
        tomorrow_start = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        tomorrow_end = tomorrow_start.replace(hour=10)

        response = self.client.post(
            "/events",
            data={
                "calendar_id": str(self.shared_calendar_id),
                "title": "全体会議",
                "description": "連絡事項の確認",
                "location": "会議室",
                "timezone": "Asia/Tokyo",
                "start_value": tomorrow_start.strftime("%Y-%m-%dT%H:%M"),
                "end_value": tomorrow_end.strftime("%Y-%m-%dT%H:%M"),
                "visibility": EventVisibility.normal.value,
                "reminders": "5,30",
                "mode": "day",
                "anchor_date": tomorrow_start.date().isoformat(),
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        with Session(self.engine) as session:
            event = session.exec(select(Event).where(Event.title == "全体会議")).first()
            reminders = session.exec(select(Reminder).where(Reminder.event_id == event.id)).all()
            jobs = session.exec(select(NotificationJob).where(NotificationJob.source_event_id == event.id)).all()

        self.assertEqual(len(reminders), 2)
        self.assertEqual({item.user_id for item in reminders}, {self.user_a_id})
        self.assertEqual(len(jobs), 2)
        self.assertEqual({item.user_id for item in jobs}, {self.user_a_id})

    def test_shared_event_creation_records_activity_log(self):
        self._login(self.user_a_id)
        start_value = datetime(2026, 4, 13, 11, 0).strftime("%Y-%m-%dT%H:%M")
        end_value = datetime(2026, 4, 13, 12, 0).strftime("%Y-%m-%dT%H:%M")

        response = self.client.post(
            "/events",
            data={
                "calendar_id": str(self.shared_calendar_id),
                "title": "Shared planning",
                "timezone": "Asia/Tokyo",
                "start_value": start_value,
                "end_value": end_value,
                "mode": "day",
                "anchor_date": "2026-04-13",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with Session(self.engine) as session:
            logs = session.exec(
                select(CalendarActivityLog).where(CalendarActivityLog.calendar_id == self.shared_calendar_id)
            ).all()

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].actor_user_id, self.user_a_id)
        self.assertEqual(logs[0].action, CalendarActivityKind.event_created)
        self.assertIn("Shared planning", logs[0].summary)

    def test_personal_event_creation_does_not_record_activity_log(self):
        self._login(self.user_a_id)
        start_value = datetime(2026, 4, 13, 15, 0).strftime("%Y-%m-%dT%H:%M")
        end_value = datetime(2026, 4, 13, 16, 0).strftime("%Y-%m-%dT%H:%M")

        response = self.client.post(
            "/events",
            data={
                "calendar_id": str(self.a_personal_id),
                "title": "Personal memo",
                "timezone": "Asia/Tokyo",
                "start_value": start_value,
                "end_value": end_value,
                "mode": "day",
                "anchor_date": "2026-04-13",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with Session(self.engine) as session:
            logs = session.exec(
                select(CalendarActivityLog).where(CalendarActivityLog.calendar_id == self.a_personal_id)
            ).all()

        self.assertEqual(logs, [])

    def test_admin_calendar_page_shows_shared_activity_log(self):
        self._login(self.user_a_id)
        start_value = datetime(2026, 4, 13, 17, 0).strftime("%Y-%m-%dT%H:%M")
        end_value = datetime(2026, 4, 13, 18, 0).strftime("%Y-%m-%dT%H:%M")

        create_response = self.client.post(
            "/events",
            data={
                "calendar_id": str(self.shared_calendar_id),
                "title": "LOG-ONLY-EVENT",
                "timezone": "Asia/Tokyo",
                "start_value": start_value,
                "end_value": end_value,
                "mode": "day",
                "anchor_date": "2026-04-13",
            },
            follow_redirects=False,
        )

        self.assertEqual(create_response.status_code, 303)

        admin_response = self.client.get("/calendar?mode=day&date=2026-04-12")
        self.assertEqual(admin_response.status_code, 200)
        self.assertIn("LOG-ONLY-EVENT", admin_response.text)

        self._login(self.user_b_id)
        staff_response = self.client.get("/calendar?mode=day&date=2026-04-12")
        self.assertEqual(staff_response.status_code, 200)
        self.assertNotIn("LOG-ONLY-EVENT", staff_response.text)

    def test_htmx_event_creation_returns_close_modal_trigger(self):
        self._login(self.user_a_id)
        start_value = datetime(2026, 4, 15, 9, 0).strftime("%Y-%m-%dT%H:%M")
        end_value = datetime(2026, 4, 15, 10, 0).strftime("%Y-%m-%dT%H:%M")

        response = self.client.post(
            "/events",
            headers={"HX-Request": "true"},
            data={
                "calendar_id": str(self.a_personal_id),
                "title": "保護者連絡",
                "timezone": "Asia/Tokyo",
                "start_value": start_value,
                "end_value": end_value,
                "mode": "day",
                "anchor_date": "2026-04-15",
            },
        )

        self.assertEqual(response.status_code, 200)
        trigger = json.loads(response.headers["HX-Trigger"])
        self.assertTrue(trigger["calendar-close-modal"])

    def test_htmx_validation_error_stays_in_modal(self):
        self._login(self.user_a_id)
        response = self.client.post(
            "/events",
            headers={"HX-Request": "true"},
            data={
                "calendar_id": str(self.a_personal_id),
                "title": "入力エラー確認",
                "timezone": "Asia/Tokyo",
                "start_value": "2026-04-16T10:00",
                "end_value": "2026-04-12T09:00",
                "mode": "day",
                "anchor_date": "2026-04-16",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("終了日時は開始日時より後にしてください。", response.text)
        self.assertIn('hx-target="#event-modal"', response.text)
        self.assertNotIn('id="calendar-main"', response.text)

    def test_private_event_hidden_from_other_staff_and_search(self):
        self._login(self.user_b_id)
        secret_start = datetime(2026, 4, 13, 13, 0).strftime("%Y-%m-%dT%H:%M")
        secret_end = datetime(2026, 4, 13, 14, 0).strftime("%Y-%m-%dT%H:%M")
        create_response = self.client.post(
            "/events",
            data={
                "calendar_id": str(self.shared_calendar_id),
                "title": "面談メモ",
                "description": "外部に見せない内容",
                "timezone": "Asia/Tokyo",
                "start_value": secret_start,
                "end_value": secret_end,
                "visibility": EventVisibility.private.value,
                "mode": "day",
                "anchor_date": "2026-04-13",
            },
            follow_redirects=False,
        )
        self.assertEqual(create_response.status_code, 303)

        self.client = TestClient(self.app)
        self._login(self.user_a_id)

        day_response = self.client.get("/calendar?mode=day&date=2026-04-13")
        self.assertEqual(day_response.status_code, 200)
        self.assertIn("予定あり", day_response.text)
        self.assertNotIn("外部に見せない内容", day_response.text)

        search_response = self.client.get("/search/events?q=面談メモ&date_from=2026-04-13&date_to=2026-04-13")
        self.assertEqual(search_response.status_code, 200)
        self.assertIn("該当する予定はありません", search_response.text)
        self.assertNotIn("外部に見せない内容", search_response.text)

    def test_search_results_render_in_modal(self):
        self._login(self.user_a_id)
        start_value = datetime(2026, 4, 14, 15, 0).strftime("%Y-%m-%dT%H:%M")
        end_value = datetime(2026, 4, 14, 16, 0).strftime("%Y-%m-%dT%H:%M")
        create_response = self.client.post(
            "/events",
            data={
                "calendar_id": str(self.a_personal_id),
                "title": "園内研修",
                "description": "検索モーダル確認用",
                "timezone": "Asia/Tokyo",
                "start_value": start_value,
                "end_value": end_value,
                "mode": "day",
                "anchor_date": "2026-04-14",
            },
            follow_redirects=False,
        )
        self.assertEqual(create_response.status_code, 303)

        search_response = self.client.get("/search/events?q=園内研修&date=2026-04-14&date_from=2026-04-14&date_to=2026-04-14")
        self.assertEqual(search_response.status_code, 200)
        self.assertIn("検索結果", search_response.text)
        self.assertIn("閉じる", search_response.text)
        self.assertIn('hx-target="#event-modal"', search_response.text)
        self.assertIn("園内研修", search_response.text)

    def test_recurring_update_requires_original_start_at_for_scope_one(self):
        self._login(self.user_a_id)
        start_value = datetime(2026, 4, 14, 9, 0).strftime("%Y-%m-%dT%H:%M")
        end_value = datetime(2026, 4, 14, 10, 0).strftime("%Y-%m-%dT%H:%M")
        create_response = self.client.post(
            "/events",
            data={
                "calendar_id": str(self.a_personal_id),
                "title": "朝会",
                "timezone": "Asia/Tokyo",
                "start_value": start_value,
                "end_value": end_value,
                "recurrence_mode": "daily",
                "recurrence_interval": "1",
                "mode": "day",
                "anchor_date": "2026-04-14",
            },
            follow_redirects=False,
        )
        self.assertEqual(create_response.status_code, 303)

        with Session(self.engine) as session:
            event = session.exec(select(Event).where(Event.title == "朝会")).first()
        self.assertEqual(event.kind, EventKind.series_master)

        update_response = self.client.post(
            f"/events/{event.id}",
            data={
                "calendar_id": str(self.a_personal_id),
                "title": "朝会",
                "timezone": "Asia/Tokyo",
                "start_value": start_value,
                "end_value": end_value,
                "scope": "one",
                "mode": "day",
                "anchor_date": "2026-04-14",
            },
            follow_redirects=False,
        )
        self.assertEqual(update_response.status_code, 400)

    def test_following_delete_from_first_occurrence_deletes_series_master(self):
        self._login(self.user_a_id)
        start_value = datetime(2026, 4, 14, 9, 0).strftime("%Y-%m-%dT%H:%M")
        end_value = datetime(2026, 4, 14, 10, 0).strftime("%Y-%m-%dT%H:%M")
        create_response = self.client.post(
            "/events",
            data={
                "calendar_id": str(self.a_personal_id),
                "title": "毎朝の確認",
                "timezone": "Asia/Tokyo",
                "start_value": start_value,
                "end_value": end_value,
                "recurrence_mode": "daily",
                "recurrence_interval": "1",
                "mode": "day",
                "anchor_date": "2026-04-14",
            },
            follow_redirects=False,
        )
        self.assertEqual(create_response.status_code, 303)

        with Session(self.engine) as session:
            event = session.exec(select(Event).where(Event.title == "毎朝の確認")).first()
            original_start_at = event.start_at.replace(tzinfo=timezone.utc).isoformat()

        delete_response = self.client.post(
            f"/events/{event.id}/delete",
            data={
                "scope": "following",
                "original_start_at": original_start_at,
                "mode": "day",
                "anchor_date": "2026-04-14",
            },
            follow_redirects=False,
        )
        self.assertEqual(delete_response.status_code, 303)

        with Session(self.engine) as session:
            event = session.get(Event, event.id)

        self.assertTrue(event.is_deleted)

    def test_admin_can_archive_and_restore_shared_calendar(self):
        self._login(self.user_a_id)

        archive_response = self.client.post(
            f"/calendars/{self.shared_calendar_id}/archive",
            data={"mode": "month", "anchor_date": "2026-04-12"},
            follow_redirects=False,
        )

        self.assertEqual(archive_response.status_code, 303)
        with Session(self.engine) as session:
            shared_calendar = session.get(Calendar, self.shared_calendar_id)
            logs = session.exec(
                select(CalendarActivityLog).where(CalendarActivityLog.calendar_id == self.shared_calendar_id)
            ).all()

        self.assertTrue(shared_calendar.is_archived)
        self.assertTrue(any("アーカイブ" in log.summary for log in logs))

        archived_page = self.client.get("/calendar")
        self.assertEqual(archived_page.status_code, 200)
        self.assertIn(f"/calendars/{self.shared_calendar_id}/restore", archived_page.text)

        restore_response = self.client.post(
            f"/calendars/{self.shared_calendar_id}/restore",
            data={"mode": "month", "anchor_date": "2026-04-12"},
            follow_redirects=False,
        )

        self.assertEqual(restore_response.status_code, 303)
        with Session(self.engine) as session:
            restored_calendar = session.get(Calendar, self.shared_calendar_id)
            logs = session.exec(
                select(CalendarActivityLog).where(CalendarActivityLog.calendar_id == self.shared_calendar_id)
            ).all()

        self.assertFalse(restored_calendar.is_archived)
        self.assertTrue(any("復元" in log.summary for log in logs))

    def test_non_admin_user_cannot_archive_shared_calendar(self):
        self._login(self.user_b_id)
        response = self.client.post(
            f"/calendars/{self.shared_calendar_id}/archive",
            data={"mode": "month", "anchor_date": "2026-04-12"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)
        with Session(self.engine) as session:
            shared_calendar = session.get(Calendar, self.shared_calendar_id)

        self.assertFalse(shared_calendar.is_archived)

    def test_personal_calendar_owner_can_archive_and_restore_own_calendar(self):
        self._login(self.user_b_id)

        archive_response = self.client.post(
            f"/calendars/{self.b_personal_id}/archive",
            data={"mode": "month", "anchor_date": "2026-04-12"},
            follow_redirects=False,
        )

        self.assertEqual(archive_response.status_code, 303)
        with Session(self.engine) as session:
            personal_calendar = session.get(Calendar, self.b_personal_id)
            user_b = session.get(User, self.user_b_id)
            logs = session.exec(
                select(CalendarActivityLog).where(CalendarActivityLog.calendar_id == self.b_personal_id)
            ).all()

        self.assertTrue(personal_calendar.is_archived)
        self.assertEqual(logs, [])
        self.assertEqual(user_b.default_calendar_id, self.shared_calendar_id)

        archived_page = self.client.get("/calendar")
        self.assertEqual(archived_page.status_code, 200)
        self.assertIn(f"/calendars/{self.b_personal_id}/restore", archived_page.text)

        restore_response = self.client.post(
            f"/calendars/{self.b_personal_id}/restore",
            data={"mode": "month", "anchor_date": "2026-04-12"},
            follow_redirects=False,
        )

        self.assertEqual(restore_response.status_code, 303)
        with Session(self.engine) as session:
            restored_calendar = session.get(Calendar, self.b_personal_id)

        self.assertFalse(restored_calendar.is_archived)

    def test_management_section_lists_active_calendars(self):
        self._login(self.user_a_id)

        response = self.client.get("/calendar")

        self.assertEqual(response.status_code, 200)
        self.assertIn("管理できるカレンダー", response.text)
        self.assertIn("運用中のカレンダー", response.text)
        self.assertIn("アーカイブ済み", response.text)
        self.assertIn(f"/calendars/{self.a_personal_id}/archive", response.text)
        self.assertIn(f"/calendars/{self.shared_calendar_id}/archive", response.text)

    def test_archived_calendar_is_hidden_from_visible_list_and_shown_in_archived_management(self):
        self._login(self.user_a_id)
        archive_response = self.client.post(
            f"/calendars/{self.shared_calendar_id}/archive",
            data={"mode": "month", "anchor_date": "2026-04-12"},
            follow_redirects=False,
        )

        self.assertEqual(archive_response.status_code, 303)
        response = self.client.get("/calendar")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(f"/calendar/preferences/{self.shared_calendar_id}/visibility", response.text)
        self.assertIn(f"/calendars/{self.shared_calendar_id}/restore", response.text)
        self.assertIn(f"/calendars/{self.shared_calendar_id}/delete", response.text)

    def test_admin_can_delete_shared_calendar(self):
        self._login(self.user_a_id)
        start_value = datetime(2026, 4, 13, 9, 0).strftime("%Y-%m-%dT%H:%M")
        end_value = datetime(2026, 4, 13, 10, 0).strftime("%Y-%m-%dT%H:%M")
        self.client.post(
            "/events",
            data={
                "calendar_id": str(self.shared_calendar_id),
                "title": "Delete shared",
                "timezone": "Asia/Tokyo",
                "start_value": start_value,
                "end_value": end_value,
                "mode": "day",
                "anchor_date": "2026-04-13",
            },
            follow_redirects=False,
        )

        response = self.client.post(
            f"/calendars/{self.shared_calendar_id}/delete",
            data={"mode": "month", "anchor_date": "2026-04-12"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with Session(self.engine) as session:
            calendar = session.get(Calendar, self.shared_calendar_id)
            members = session.exec(
                select(CalendarMember).where(CalendarMember.calendar_id == self.shared_calendar_id)
            ).all()
            preferences = session.exec(
                select(CalendarUserPreference).where(CalendarUserPreference.calendar_id == self.shared_calendar_id)
            ).all()
            logs = session.exec(
                select(CalendarActivityLog).where(CalendarActivityLog.calendar_id == self.shared_calendar_id)
            ).all()

        self.assertIsNone(calendar)
        self.assertEqual(members, [])
        self.assertEqual(preferences, [])
        self.assertEqual(logs, [])

    def test_non_admin_user_cannot_delete_shared_calendar(self):
        self._login(self.user_b_id)
        response = self.client.post(
            f"/calendars/{self.shared_calendar_id}/delete",
            data={"mode": "month", "anchor_date": "2026-04-12"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)
        with Session(self.engine) as session:
            calendar = session.get(Calendar, self.shared_calendar_id)

        self.assertIsNotNone(calendar)

    def test_personal_calendar_owner_can_delete_own_calendar(self):
        self._login(self.user_b_id)
        response = self.client.post(
            f"/calendars/{self.b_personal_id}/delete",
            data={"mode": "month", "anchor_date": "2026-04-12"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with Session(self.engine) as session:
            calendar = session.get(Calendar, self.b_personal_id)
            user_b = session.get(User, self.user_b_id)

        self.assertIsNone(calendar)
        self.assertIsNotNone(user_b.default_calendar_id)
        self.assertEqual(user_b.default_calendar_id, self.shared_calendar_id)

    def test_other_staff_cannot_delete_personal_calendar(self):
        self._login(self.user_a_id)
        response = self.client.post(
            f"/calendars/{self.b_personal_id}/delete",
            data={"mode": "month", "anchor_date": "2026-04-12"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)
        with Session(self.engine) as session:
            calendar = session.get(Calendar, self.b_personal_id)

        self.assertIsNotNone(calendar)


if __name__ == "__main__":
    unittest.main()
