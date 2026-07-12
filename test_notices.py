import unittest
from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from models import Child, ChildStatus, Classroom, Notice, NoticeTarget, NoticeTargetType
import routers.notices as notices_module
from testing_helpers import authenticate_mock_staff


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


if __name__ == "__main__":
    unittest.main()
