import shutil
import unittest
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from auth import Role, StaffUser
from models import Classroom, Message, MessageAttachment
from time_utils import utc_now
import routers.staff_rooms as staff_rooms_module


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?"
    b"\x00\x05\xfe\x02\xfeA\xdd\x9d\xb1\x00\x00\x00\x00IEND\xaeB`\x82"
)


class StaffRoomRouterTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        self.app = FastAPI()
        self.app.include_router(staff_rooms_module.router)

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.current_user = StaffUser(role=Role.CAN_EDIT, name="テスト職員")

        def override_get_current_staff_user():
            return self.current_user

        self.app.dependency_overrides[staff_rooms_module.get_session] = override_get_session
        self.app.dependency_overrides[staff_rooms_module.get_current_staff_user] = override_get_current_staff_user

        self.original_upload_root = staff_rooms_module.MESSAGE_UPLOAD_ROOT
        self.upload_root = Path("test_uploads") / self._testMethodName
        shutil.rmtree(self.upload_root, ignore_errors=True)
        self.upload_root.mkdir(parents=True, exist_ok=True)
        staff_rooms_module.MESSAGE_UPLOAD_ROOT = self.upload_root

        self.client = TestClient(self.app)

        with Session(self.engine) as session:
            room_a = Classroom(name="ひよこ組", display_order=1)
            room_b = Classroom(name="りす組", display_order=2)
            session.add(room_a)
            session.add(room_b)
            session.commit()
            session.refresh(room_a)
            session.refresh(room_b)
            self.room_a_id = room_a.id
            self.room_b_id = room_b.id

    def tearDown(self):
        self.client.close()
        staff_rooms_module.MESSAGE_UPLOAD_ROOT = self.original_upload_root
        shutil.rmtree(self.upload_root, ignore_errors=True)
        self.engine.dispose()

    def _create_message(
        self,
        *,
        room_id: int,
        body: str,
        parent_message_id: int | None = None,
        created_at=None,
        deleted: bool = False,
    ) -> int:
        with Session(self.engine) as session:
            timestamp = created_at or utc_now()
            message = Message(
                room_id=room_id,
                parent_message_id=parent_message_id,
                author_name="テスト職員",
                body=body,
                created_at=timestamp,
                updated_at=timestamp,
                deleted_at=timestamp if deleted else None,
                deleted_by="テスト職員" if deleted else None,
            )
            session.add(message)
            session.commit()
            session.refresh(message)
            return message.id

    def test_timeline_shows_parent_messages_from_multiple_rooms_on_one_screen(self):
        parent_message_id = self._create_message(room_id=self.room_a_id, body="親メッセージ")
        self._create_message(room_id=self.room_a_id, body="返信は一覧に出さない", parent_message_id=parent_message_id)
        self._create_message(room_id=self.room_b_id, body="別ルームの親メッセージ")

        response = self.client.get("/staff-rooms/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("親メッセージ", response.text)
        self.assertIn("別ルームの親メッセージ", response.text)
        self.assertIn("返信 1件", response.text)
        self.assertIn('data-reply-level="low"', response.text)
        self.assertIn("from-indigo-50", response.text)
        self.assertIn("data-focus-reply-input", response.text)
        self.assertIn("thread-reply-body", response.text)
        self.assertIn("scrollIntoView", response.text)
        self.assertNotIn("返信は一覧に出さない", response.text)
        self.assertNotIn("ひよこ組", response.text)
        self.assertNotIn("りす組", response.text)
        self.assertIn("新規メッセージを書く", response.text)
        self.assertIn('id="staff-sidebar"', response.text)
        self.assertIn('data-sidebar-open', response.text)
        self.assertIn('/children', response.text)
        self.assertIn('/staff-rooms/', response.text)
        self.assertIn('/staff/login', response.text)
        self.assertIn("職員を選択する", response.text)
        self.assertNotIn('/staff/logout', response.text)
        self.assertIn('hx-get="/staff-rooms/partials/timeline"', response.text)
        self.assertIn('hx-trigger="every 5s"', response.text)

    def test_timeline_partial_reflects_new_parent_messages(self):
        first_response = self.client.get("/staff-rooms/partials/timeline")
        self.assertEqual(first_response.status_code, 200)
        self.assertIn("まだ親メッセージはありません。", first_response.text)

        self._create_message(room_id=self.room_a_id, body="あとから追加された親メッセージ")

        second_response = self.client.get("/staff-rooms/partials/timeline")
        self.assertEqual(second_response.status_code, 200)
        self.assertIn("あとから追加された親メッセージ", second_response.text)

    def test_thread_reply_post_binds_to_parent_and_saves_attachments(self):
        parent_message_id = self._create_message(room_id=self.room_a_id, body="親メッセージ")
        existing_reply_id = self._create_message(
            room_id=self.room_a_id,
            body="既存返信",
            parent_message_id=parent_message_id,
        )

        response = self.client.post(
            f"/staff-rooms/threads/{parent_message_id}/replies",
            data={
                "body": "返信への返信も親にぶら下げる",
                "reply_to_message_id": str(existing_reply_id),
            },
            files=[
                ("attachments", ("memo.txt", b"thread note", "text/plain")),
                ("attachments", ("photo.png", PNG_BYTES, "image/png")),
            ],
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("返信 2件", response.text)
        self.assertIn('data-reply-level="medium"', response.text)
        self.assertIn("from-indigo-100", response.text)
        self.assertIn('hx-swap-oob="outerHTML"', response.text)
        self.assertIn('hx-trigger="every 5s[!(document.activeElement && document.activeElement.closest(\'#thread-panel form\'))]"', response.text)
        self.assertIn("object-contain", response.text)
        self.assertIn("max-h-full max-w-full", response.text)

        with Session(self.engine) as session:
            replies = session.exec(
                select(Message)
                .where(Message.parent_message_id == parent_message_id)
                .order_by(Message.id)
            ).all()
            latest_reply = replies[-1]
            attachments = session.exec(
                select(MessageAttachment).where(MessageAttachment.message_id == latest_reply.id)
            ).all()

        self.assertEqual(latest_reply.parent_message_id, parent_message_id)
        self.assertNotEqual(latest_reply.parent_message_id, existing_reply_id)
        self.assertEqual(len(attachments), 2)
        self.assertEqual(sum(1 for attachment in attachments if attachment.is_image), 1)

        first_attachment = attachments[0]
        download_response = self.client.get(f"/staff-rooms/attachments/{first_attachment.id}")
        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(download_response.content, b"thread note")

    def test_thread_detail_orders_replies_and_shows_deleted_parent_placeholder(self):
        timestamp = utc_now()
        parent_message_id = self._create_message(
            room_id=self.room_a_id,
            body="削除済み親",
            created_at=timestamp,
            deleted=True,
        )
        self._create_message(
            room_id=self.room_a_id,
            body="最初の返信",
            parent_message_id=parent_message_id,
            created_at=timestamp - timedelta(minutes=5),
        )
        self._create_message(
            room_id=self.room_a_id,
            body="次の返信",
            parent_message_id=parent_message_id,
            created_at=timestamp + timedelta(minutes=5),
        )

        response = self.client.get(f"/staff-rooms/threads/{parent_message_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("このメッセージは削除されました。", response.text)
        self.assertLess(response.text.index("最初の返信"), response.text.index("次の返信"))

    def test_legacy_room_url_redirects_to_single_timeline_and_missing_parent_is_404(self):
        legacy_response = self.client.get(f"/staff-rooms/{self.room_a_id}", follow_redirects=False)
        missing_response = self.client.get("/staff-rooms/threads/9999")

        self.assertEqual(legacy_response.status_code, 303)
        self.assertEqual(legacy_response.headers["location"], "/staff-rooms/")
        self.assertEqual(missing_response.status_code, 404)

    def test_view_only_staff_can_view_but_cannot_post(self):
        parent_message_id = self._create_message(room_id=self.room_a_id, body="親メッセージ")
        self.current_user = StaffUser(role=Role.VIEW_ONLY, name="閲覧スタッフ")

        room_response = self.client.get("/staff-rooms/")
        create_response = self.client.post(
            "/staff-rooms/messages",
            data={"body": "投稿不可"},
            follow_redirects=False,
        )
        reply_response = self.client.post(
            f"/staff-rooms/threads/{parent_message_id}/replies",
            data={"body": "返信不可"},
        )

        self.assertEqual(room_response.status_code, 200)
        self.assertEqual(create_response.status_code, 403)
        self.assertEqual(reply_response.status_code, 403)
        self.assertIn("閲覧専用", room_response.text)


if __name__ == "__main__":
    unittest.main()
