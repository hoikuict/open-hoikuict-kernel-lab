import unittest
from datetime import date, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from models import (
    Child,
    ChildStatus,
    Classroom,
    Notice,
    NoticePriority,
    NoticeStatus,
    NoticeTarget,
    NoticeTargetType,
)
import routers.notices as notices_module
from testing_helpers import authenticate_mock_staff
from time_utils import utc_now


class NoticeRouterTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        self.app = FastAPI()
        self.app.include_router(notices_module.router)

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.app.dependency_overrides[notices_module.get_session] = override_get_session
        self.client = TestClient(self.app)
        authenticate_mock_staff(self.client)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_list_tolerates_invalid_target_value(self):
        with Session(self.engine) as session:
            classroom = Classroom(name="ひよこ組", display_order=1)
            child = Child(
                last_name="田中",
                first_name="さくら",
                last_name_kana="タナカ",
                first_name_kana="サクラ",
                birth_date=date(2021, 5, 5),
                enrollment_date=date(2024, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=None,
            )
            notice = Notice(title="確認", body="本文")
            session.add(classroom)
            session.add(child)
            session.add(notice)
            session.flush()
            session.add(
                NoticeTarget(
                    notice_id=notice.id,
                    target_type=NoticeTargetType.classroom,
                    target_value="not-a-number",
                )
            )
            session.commit()

        response = self.client.get("/notices/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("クラス指定", response.text)

    def test_publish_end_must_not_be_before_start(self):
        response = self.client.post(
            "/notices/",
            data={
                "title": "期間エラー",
                "body": "本文",
                "publish_start_at": "2026-04-02T10:00",
                "publish_end_at": "2026-04-01T10:00",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 400)

    def test_list_can_search_filter_and_sort_notices(self):
        now = utc_now()
        with Session(self.engine) as session:
            important_published = Notice(
                title="避難訓練のお知らせ",
                body="防災頭巾を持参してください。",
                status=NoticeStatus.published,
                priority=NoticePriority.high,
                created_by="園長",
                updated_at=now - timedelta(days=2),
            )
            normal_draft = Notice(
                title="来月の予定",
                body="行事予定を確認中です。",
                status=NoticeStatus.draft,
                priority=NoticePriority.normal,
                created_by="主任",
                updated_at=now,
            )
            important_draft = Notice(
                title="緊急連絡網",
                body="連絡先を再確認します。",
                status=NoticeStatus.draft,
                priority=NoticePriority.high,
                created_by="事務",
                updated_at=now - timedelta(days=1),
            )
            session.add(important_published)
            session.add(normal_draft)
            session.add(important_draft)
            session.commit()
            session.refresh(important_published)

        body_search = self.client.get("/notices/?q=防災頭巾")
        author_search = self.client.get("/notices/?q=主任")
        id_search = self.client.get(f"/notices/?q={important_published.id}")
        published_only = self.client.get("/notices/?status=published")
        high_only = self.client.get("/notices/?priority=high")
        priority_sorted = self.client.get("/notices/?sort=priority_desc")
        status_sorted = self.client.get("/notices/?sort=status_published")

        self.assertIn("避難訓練のお知らせ", body_search.text)
        self.assertNotIn("来月の予定", body_search.text)
        self.assertIn("来月の予定", author_search.text)
        self.assertIn("避難訓練のお知らせ", id_search.text)
        self.assertIn("避難訓練のお知らせ", published_only.text)
        self.assertNotIn("緊急連絡網", published_only.text)
        self.assertIn("避難訓練のお知らせ", high_only.text)
        self.assertIn("緊急連絡網", high_only.text)
        self.assertNotIn("来月の予定", high_only.text)
        self.assertLess(priority_sorted.text.index("緊急連絡網"), priority_sorted.text.index("来月の予定"))
        self.assertLess(status_sorted.text.index("避難訓練のお知らせ"), status_sorted.text.index("来月の予定"))


if __name__ == "__main__":
    unittest.main()
