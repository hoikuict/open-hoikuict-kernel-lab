import unittest
from datetime import date, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from auth import Role, StaffUser
from models import (
    Child,
    ChildStatus,
    Classroom,
    DailyContactEntry,
    DailyContactReply,
    DailyContactReplyStatus,
    Family,
    Notice,
    NoticePriority,
    NoticeRead,
    NoticeStatus,
    NoticeTarget,
    NoticeTargetType,
    ParentAccount,
    ParentAccountStatus,
    ParentContactType,
    ProfileChangeNotification,
)
from time_utils import utc_now
import routers.daily_contacts as daily_contacts_module
import routers.notices as notices_module
import routers.parent_accounts as parent_accounts_module
import routers.parent_portal as parent_portal_module
from testing_helpers import configure_test_environment


class ParentPortalTests(unittest.TestCase):
    def setUp(self):
        configure_test_environment()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        self.app = FastAPI()
        self.app.include_router(parent_portal_module.router)
        self.app.include_router(parent_portal_module.mock_login_router)
        self.app.include_router(parent_accounts_module.router)
        self.app.include_router(notices_module.router)
        self.app.include_router(daily_contacts_module.router)

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.app.dependency_overrides[parent_portal_module.get_session] = override_get_session
        self.app.dependency_overrides[parent_accounts_module.get_session] = override_get_session
        self.app.dependency_overrides[notices_module.get_session] = override_get_session
        self.app.dependency_overrides[daily_contacts_module.get_session] = override_get_session
        self.app.dependency_overrides[parent_accounts_module.get_current_staff_user] = (
            lambda: StaffUser(
                role=Role.CAN_EDIT,
                name="台帳担当",
                can_manage_child_records=True,
            )
        )

        self.client = TestClient(self.app)

        with Session(self.engine) as session:
            classroom_a = Classroom(name="ひよこ組", display_order=1)
            classroom_b = Classroom(name="うさぎ組", display_order=2)
            session.add(classroom_a)
            session.add(classroom_b)
            session.flush()

            family_main = Family(family_name="田中家", home_address="東京都港区1-1-1", home_phone="03-1111-1111")
            family_single = Family(family_name="佐藤家", home_address="東京都新宿区2-2-2", home_phone="03-2222-2222")
            session.add(family_main)
            session.add(family_single)
            session.flush()

            child_main_1 = Child(
                last_name="田中",
                first_name="さくら",
                last_name_kana="タナカ",
                first_name_kana="サクラ",
                birth_date=date(2021, 5, 5),
                enrollment_date=date(2024, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=classroom_a.id,
                family_id=family_main.id,
            )
            child_main_2 = Child(
                last_name="田中",
                first_name="はると",
                last_name_kana="タナカ",
                first_name_kana="ハルト",
                birth_date=date(2020, 6, 6),
                enrollment_date=date(2023, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=classroom_b.id,
                family_id=family_main.id,
            )
            child_single = Child(
                last_name="佐藤",
                first_name="みお",
                last_name_kana="サトウ",
                first_name_kana="ミオ",
                birth_date=date(2021, 7, 7),
                enrollment_date=date(2024, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=classroom_b.id,
                family_id=family_single.id,
            )
            session.add(child_main_1)
            session.add(child_main_2)
            session.add(child_single)
            session.flush()
            self.child_id = child_main_1.id
            self.second_child_id = child_main_2.id
            self.other_child_id = child_single.id
            self.main_family_id = family_main.id

            parent_main = ParentAccount(
                display_name="田中 花",
                email="tanaka@example.com",
                phone="090-0000-0001",
                home_address="東京都港区1-1-1",
                workplace="サンプル会社",
                workplace_address="東京都港区3-3-3",
                status=ParentAccountStatus.active,
                family_id=family_main.id,
                invited_at=utc_now(),
            )
            parent_single = ParentAccount(
                display_name="佐藤 美穂",
                email="sato@example.com",
                phone="090-0000-0002",
                home_address="東京都新宿区2-2-2",
                workplace="グリーン商事",
                workplace_address="東京都新宿区4-4-4",
                status=ParentAccountStatus.active,
                family_id=family_single.id,
                invited_at=utc_now(),
            )
            session.add(parent_main)
            session.add(parent_single)
            session.flush()
            self.parent_account_id = parent_main.id
            self.single_parent_account_id = parent_single.id

            public_notice = Notice(
                title="遠足のお知らせ",
                body="全家庭向けのお知らせです。",
                priority=NoticePriority.normal,
                status=NoticeStatus.published,
                publish_start_at=utc_now() - timedelta(hours=1),
            )
            hidden_notice = Notice(
                title="限定連絡",
                body="対象児童向けのみです。",
                priority=NoticePriority.high,
                status=NoticeStatus.published,
                publish_start_at=utc_now() - timedelta(hours=1),
            )
            session.add(public_notice)
            session.add(hidden_notice)
            session.flush()
            self.public_notice_id = public_notice.id

            session.add(NoticeTarget(notice_id=public_notice.id, target_type=NoticeTargetType.all))
            session.add(
                NoticeTarget(
                    notice_id=hidden_notice.id,
                    target_type=NoticeTargetType.child,
                    target_value=str(child_single.id),
                )
            )
            session.commit()

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def _login_parent(self, account_id: int):
        response = self.client.post(
            "/parent-portal/login",
            data={"parent_account_id": account_id},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def test_parent_can_log_in_and_see_linked_family_children(self):
        self._login_parent(self.parent_account_id)

        response = self.client.get("/parent-portal/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("田中 さくら", response.text)
        self.assertIn("田中 はると", response.text)
        self.assertIn("遠足のお知らせ", response.text)
        self.assertNotIn("限定連絡", response.text)

    def test_parent_can_submit_present_contact_and_staff_can_review_it(self):
        self._login_parent(self.parent_account_id)
        today = date.today().isoformat()

        response = self.client.post(
            f"/parent-portal/children/{self.child_id}/contact",
            data={
                "date": today,
                "contact_type": ParentContactType.present.value,
                "temperature": "36.8",
                "sleep_notes": "21:00-6:15",
                "breakfast_status": "完食",
                "bowel_movement_status": "あり",
                "mood": "良好",
                "cough": "なし",
                "runny_nose": "なし",
                "medication": "なし",
                "condition_note": "少し眠そうです。",
                "contact_note": "本日は16:30に迎えます。",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            f"/parent-portal/?date={today}&notice=saved",
        )

        with Session(self.engine) as session:
            entry = session.exec(select(DailyContactEntry).where(DailyContactEntry.child_id == self.child_id)).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.contact_type, ParentContactType.present)
        self.assertEqual(entry.contact_note, "本日は16:30に迎えます。")

        home_response = self.client.get(response.headers["location"])
        self.assertEqual(home_response.status_code, 200)
        self.assertIn("日次連絡を保存しました。", home_response.text)

        history_response = self.client.get("/parent-portal/history")
        self.assertEqual(history_response.status_code, 200)
        self.assertIn("出席", history_response.text)
        self.assertIn("本日は16:30に迎えます。", history_response.text)

        staff_list_response = self.client.get(f"/daily-contacts/?date={today}")
        self.assertEqual(staff_list_response.status_code, 200)
        self.assertIn("提出済み", staff_list_response.text)
        self.assertIn("出席", staff_list_response.text)
        self.assertIn("田中 花", staff_list_response.text)

        detail_response = self.client.get(f"/daily-contacts/{self.child_id}?date={today}")
        self.assertEqual(detail_response.status_code, 200)
        self.assertIn("本日は16:30に迎えます。", detail_response.text)

    def test_staff_can_publish_daily_contact_reply_to_parent(self):
        self._login_parent(self.parent_account_id)
        target = date(2026, 7, 5)

        contact_response = self.client.post(
            f"/parent-portal/children/{self.child_id}/contact",
            data={
                "date": target.isoformat(),
                "attendance_mode": "present",
                "contact_type": ParentContactType.present.value,
                "temperature": "36.8",
                "mood": "良好",
                "breakfast_status": "完食",
            },
            follow_redirects=False,
        )
        self.assertEqual(contact_response.status_code, 303)

        draft_response = self.client.post(
            f"/daily-contacts/{self.child_id}/reply",
            data={
                "date": target.isoformat(),
                "reply_nap_time": "12:30-14:10",
                "action": "draft",
            },
            follow_redirects=False,
        )
        self.assertEqual(draft_response.status_code, 303)
        staff_list_with_draft = self.client.get(f"/daily-contacts/?date={target.isoformat()}")
        self.assertEqual(staff_list_with_draft.status_code, 200)
        self.assertIn("下書き", staff_list_with_draft.text)
        self.assertIn("返信者: 台帳担当", staff_list_with_draft.text)
        self.assertNotIn("返信済み", staff_list_with_draft.text)

        parent_home_before_publish = self.client.get(f"/parent-portal/?date={target.isoformat()}")
        self.assertEqual(parent_home_before_publish.status_code, 200)
        self.assertNotIn("園からの返信", parent_home_before_publish.text)
        self.assertNotIn("12:30-14:10", parent_home_before_publish.text)

        publish_response = self.client.post(
            f"/daily-contacts/{self.child_id}/reply",
            data={
                "date": target.isoformat(),
                "reply_nap_time": "12:30-14:10",
                "reply_temperature": "36.9",
                "reply_bowel_movement": "あり",
                "reply_appetite": "完食",
                "reply_message": "今日も元気に過ごしました。",
                "action": "publish",
            },
            follow_redirects=False,
        )
        self.assertEqual(publish_response.status_code, 303)

        staff_list_after_publish = self.client.get(f"/daily-contacts/?date={target.isoformat()}")
        self.assertEqual(staff_list_after_publish.status_code, 200)
        self.assertIn("返信済み", staff_list_after_publish.text)
        self.assertIn("返信者: 台帳担当", staff_list_after_publish.text)

        staff_detail = self.client.get(f"/daily-contacts/{self.child_id}?date={target.isoformat()}")
        self.assertEqual(staff_detail.status_code, 200)
        self.assertIn("公開済み", staff_detail.text)
        self.assertIn("更新者: 台帳担当", staff_detail.text)
        self.assertIn("今日も元気に過ごしました。", staff_detail.text)

        parent_home = self.client.get(f"/parent-portal/?date={target.isoformat()}")
        self.assertEqual(parent_home.status_code, 200)
        self.assertIn("園からの返信", parent_home.text)
        self.assertIn("返信者: 台帳担当", parent_home.text)
        self.assertIn("お昼寝時間: 12:30-14:10", parent_home.text)
        self.assertIn("体温: 36.9", parent_home.text)
        self.assertIn("今日も元気に過ごしました。", parent_home.text)

        parent_contact_form = self.client.get(
            f"/parent-portal/children/{self.child_id}/contact?date={target.isoformat()}"
        )
        self.assertEqual(parent_contact_form.status_code, 200)
        self.assertIn("返信者: 台帳担当", parent_contact_form.text)
        self.assertIn("食欲: 完食", parent_contact_form.text)

        history_response = self.client.get("/parent-portal/history")
        self.assertEqual(history_response.status_code, 200)
        self.assertIn("返信者: 台帳担当", history_response.text)
        self.assertIn("排便: あり", history_response.text)

        with Session(self.engine) as session:
            reply = session.exec(
                select(DailyContactReply).where(DailyContactReply.child_id == self.child_id)
            ).first()
        self.assertIsNotNone(reply)
        self.assertEqual(reply.status, DailyContactReplyStatus.published)
        self.assertEqual(reply.staff_name, "台帳担当")

    def test_daily_contact_list_can_sort_by_submission_status(self):
        target = date(2026, 7, 5)
        with Session(self.engine) as session:
            session.add(
                DailyContactEntry(
                    child_id=self.second_child_id,
                    parent_account_id=self.parent_account_id,
                    target_date=target,
                    contact_type=ParentContactType.present,
                )
            )
            session.commit()

        submitted_first_response = self.client.get(
            f"/daily-contacts/?date={target.isoformat()}&classroom_id=&sort=submitted_first"
        )
        self.assertEqual(submitted_first_response.status_code, 200)
        submitted_first_html = submitted_first_response.text
        self.assertIn('value="submitted_first" selected', submitted_first_html)
        self.assertIn(
            f'/daily-contacts/{self.second_child_id}?date={target.isoformat()}&amp;sort=submitted_first',
            submitted_first_html,
        )
        self.assertLess(
            submitted_first_html.index("田中 はると"),
            submitted_first_html.index("田中 さくら"),
        )

        detail_response = self.client.get(
            f"/daily-contacts/{self.second_child_id}?date={target.isoformat()}&sort=submitted_first"
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertIn(
            f'href="/daily-contacts/?date={target.isoformat()}&amp;sort=submitted_first"',
            detail_response.text,
        )

        unsubmitted_first_response = self.client.get(
            f"/daily-contacts/?date={target.isoformat()}&sort=unsubmitted_first"
        )
        self.assertEqual(unsubmitted_first_response.status_code, 200)
        unsubmitted_first_html = unsubmitted_first_response.text
        self.assertIn('value="unsubmitted_first" selected', unsubmitted_first_html)
        self.assertLess(
            unsubmitted_first_html.index("田中 さくら"),
            unsubmitted_first_html.index("田中 はると"),
        )

    def test_parent_can_submit_sick_absence_and_staff_can_review_it(self):
        self._login_parent(self.parent_account_id)
        today = date.today().isoformat()

        response = self.client.post(
            f"/parent-portal/children/{self.child_id}/contact",
            data={
                "date": today,
                "contact_type": ParentContactType.absent_sick.value,
                "absence_temperature": "38.1",
                "absence_symptoms": "発熱と咳",
                "absence_diagnosis": "かぜ",
                "absence_note": "午前中に受診予定です。",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        with Session(self.engine) as session:
            entry = session.exec(select(DailyContactEntry).where(DailyContactEntry.child_id == self.child_id)).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.contact_type, ParentContactType.absent_sick)
        self.assertEqual(entry.absence_temperature, "38.1")
        self.assertEqual(entry.absence_symptoms, "発熱と咳")
        self.assertEqual(entry.absence_diagnosis, "かぜ")
        self.assertIsNone(entry.temperature)

        history_response = self.client.get("/parent-portal/history")
        self.assertEqual(history_response.status_code, 200)
        self.assertIn("欠席(病欠)", history_response.text)
        self.assertIn("かぜ", history_response.text)

        detail_response = self.client.get(f"/daily-contacts/{self.child_id}?date={today}")
        self.assertEqual(detail_response.status_code, 200)
        self.assertIn("病欠", detail_response.text)
        self.assertIn("発熱と咳", detail_response.text)

    def test_sick_absence_requires_temperature_and_symptoms(self):
        self._login_parent(self.parent_account_id)
        today = date.today().isoformat()

        response = self.client.post(
            f"/parent-portal/children/{self.child_id}/contact",
            data={
                "date": today,
                "contact_type": ParentContactType.absent_sick.value,
                "absence_symptoms": "発熱",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("現在の体温", response.text)

    def test_absence_requires_private_or_sick_reason(self):
        self._login_parent(self.parent_account_id)
        today = date.today().isoformat()

        response = self.client.post(
            f"/parent-portal/children/{self.child_id}/contact",
            data={
                "date": today,
                "attendance_mode": "absent",
                "contact_type": ParentContactType.present.value,
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("欠席の場合は、私用または病欠を選択してください。", response.text)
        with Session(self.engine) as session:
            entry = session.exec(select(DailyContactEntry).where(DailyContactEntry.child_id == self.child_id)).first()
        self.assertIsNone(entry)

    def test_parent_only_sees_accessible_notices_and_read_is_recorded(self):
        self._login_parent(self.parent_account_id)

        list_response = self.client.get("/parent-portal/notices")
        self.assertEqual(list_response.status_code, 200)
        self.assertIn("遠足のお知らせ", list_response.text)
        self.assertNotIn("限定連絡", list_response.text)

        detail_response = self.client.get(f"/parent-portal/notices/{self.public_notice_id}")
        self.assertEqual(detail_response.status_code, 200)
        self.assertIn("全家庭向けのお知らせです。", detail_response.text)

        with Session(self.engine) as session:
            read = session.exec(
                select(NoticeRead).where(
                    NoticeRead.notice_id == self.public_notice_id,
                    NoticeRead.parent_account_id == self.parent_account_id,
                )
            ).first()
        self.assertIsNotNone(read)

    def test_staff_can_create_parent_account_for_family(self):
        response = self.client.post(
            "/parent-accounts/",
            data={
                "display_name": "田中 美香",
                "email": "new-parent@example.com",
                "phone": "090-9999-9999",
                "status": "active",
                "family_id": str(self.main_family_id),
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        with Session(self.engine) as session:
            account = session.exec(select(ParentAccount).where(ParentAccount.email == "new-parent@example.com")).first()

        self.assertIsNotNone(account)
        self.assertEqual(account.family_id, self.main_family_id)

    def test_profile_update_creates_staff_notification(self):
        self._login_parent(self.parent_account_id)

        response = self.client.post(
            "/parent-portal/profile",
            data={
                "email": "updated-parent@example.com",
                "phone": "090-1234-5678",
                "home_address": "東京都港区3-3-3",
                "workplace": "新しい会社",
                "workplace_address": "東京都港区4-4-4",
                "workplace_phone": "03-3333-3333",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        with Session(self.engine) as session:
            account = session.get(ParentAccount, self.parent_account_id)
            notification = session.exec(select(ProfileChangeNotification)).first()

        self.assertEqual(account.email, "updated-parent@example.com")
        self.assertEqual(account.home_address, "東京都港区3-3-3")
        self.assertIsNotNone(notification)
        self.assertIn("プロフィール", notification.change_summary)

        staff_response = self.client.get("/parent-accounts/")
        self.assertEqual(staff_response.status_code, 200)
        self.assertIn("未確認のプロフィール変更", staff_response.text)
        self.assertIn("東京都港区3-3-3", staff_response.text)

    def test_child_profile_selector_redirects_when_only_one_child(self):
        self._login_parent(self.single_parent_account_id)

        response = self.client.get("/parent-portal/children/profile", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/parent-portal/children/{self.other_child_id}/profile")

    def test_child_profile_selector_lists_children_when_multiple_are_linked(self):
        self._login_parent(self.parent_account_id)

        response = self.client.get("/parent-portal/children/profile")

        self.assertEqual(response.status_code, 200)
        self.assertIn(f"/parent-portal/children/{self.child_id}/profile", response.text)
        self.assertIn(f"/parent-portal/children/{self.second_child_id}/profile", response.text)


if __name__ == "__main__":
    unittest.main()
