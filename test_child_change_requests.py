import unittest
from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from models import (
    Child,
    ChildProfileChangeRequest,
    ChildProfileChangeRequestStatus,
    ChildStatus,
    Classroom,
    Family,
    Guardian,
    ParentAccount,
    ParentAccountStatus,
)
import routers.child_change_requests as child_change_requests_module
from testing_helpers import authenticate_mock_staff
import routers.parent_portal as parent_portal_module


class ChildChangeRequestTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        self.app = FastAPI()
        self.app.include_router(parent_portal_module.router)
        self.app.include_router(parent_portal_module.mock_login_router)
        self.app.include_router(child_change_requests_module.router)

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.app.dependency_overrides[parent_portal_module.get_session] = override_get_session
        self.app.dependency_overrides[child_change_requests_module.get_session] = override_get_session

        self.client = TestClient(self.app)
        authenticate_mock_staff(self.client)

        with Session(self.engine) as session:
            classroom = Classroom(name="Class A", display_order=1)
            session.add(classroom)
            session.flush()

            family = Family(
                family_name="伊藤家",
                home_address="Old Home",
                home_phone="03-1111-1111",
                shared_profile={
                    "guardians": [
                        {
                            "order": 1,
                            "last_name": "Ito",
                            "first_name": "Parent1",
                            "relationship": "母",
                            "phone": "090-0000-0001",
                            "workplace": "Old Office",
                            "workplace_address": "Office Address",
                            "workplace_phone": "03-2222-2222",
                        }
                    ]
                },
            )
            session.add(family)
            session.flush()

            child = Child(
                last_name="Ito",
                first_name="Neo",
                last_name_kana="ITO",
                first_name_kana="NEO",
                birth_date=date(2021, 4, 12),
                enrollment_date=date(2024, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=classroom.id,
                family_id=family.id,
                home_address="Old Home",
                home_phone="03-1111-1111",
                extra_data={"allergy": ["Egg"], "medical_notes": "None"},
            )
            sibling = Child(
                last_name="Ito",
                first_name="Luna",
                last_name_kana="ITO",
                first_name_kana="LUNA",
                birth_date=date(2020, 2, 1),
                enrollment_date=date(2023, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=classroom.id,
                family_id=family.id,
                home_address="Old Home",
                home_phone="03-1111-1111",
                extra_data={"allergy": [], "medical_notes": ""},
            )
            session.add(child)
            session.add(sibling)
            session.flush()
            self.child_id = child.id
            self.sibling_id = sibling.id

            session.add(
                Guardian(
                    child_id=child.id,
                    last_name="Ito",
                    first_name="Parent1",
                    relationship="母",
                    phone="090-0000-0001",
                    workplace="Old Office",
                    workplace_address="Office Address",
                    workplace_phone="03-2222-2222",
                    order=1,
                )
            )
            session.add(
                Guardian(
                    child_id=sibling.id,
                    last_name="Ito",
                    first_name="Parent1",
                    relationship="母",
                    phone="090-0000-0001",
                    workplace="Old Office",
                    workplace_address="Office Address",
                    workplace_phone="03-2222-2222",
                    order=1,
                )
            )

            parent = ParentAccount(
                display_name="Ito Parent",
                email="parent@example.com",
                status=ParentAccountStatus.active,
                family_id=family.id,
            )
            session.add(parent)
            session.flush()
            self.parent_account_id = parent.id
            session.commit()

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def _login_parent(self):
        response = self.client.post(
            "/parent-portal/login",
            data={"parent_account_id": self.parent_account_id},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def test_parent_request_is_applied_only_after_admin_approval_and_updates_family(self):
        self._login_parent()

        form_response = self.client.get(f"/parent-portal/children/{self.child_id}/profile")
        self.assertEqual(form_response.status_code, 200)
        self.assertIn("情報変更申請", form_response.text)

        submit_response = self.client.post(
            f"/parent-portal/children/{self.child_id}/profile",
            data={
                "last_name": "Ito",
                "first_name": "Neo",
                "last_name_kana": "ITO",
                "first_name_kana": "NEO",
                "birth_date": "2021-04-12",
                "enrollment_date": "2024-04-01",
                "withdrawal_date": "",
                "status": "enrolled",
                "home_address": "New Home",
                "home_phone": "03-9999-9999",
                "allergy": "Egg,Milk",
                "medical_notes": "Updated",
                "g1_last_name": "Ito",
                "g1_first_name": "Parent1",
                "g1_last_name_kana": "",
                "g1_first_name_kana": "",
                "g1_relationship": "母",
                "g1_phone": "090-1234-5678",
                "g1_workplace": "New Office",
                "g1_workplace_address": "New Office Address",
                "g1_workplace_phone": "03-7777-7777",
                "g2_last_name": "",
                "g2_first_name": "",
                "g2_last_name_kana": "",
                "g2_first_name_kana": "",
                "g2_relationship": "父",
                "g2_phone": "",
                "g2_workplace": "",
                "g2_workplace_address": "",
                "g2_workplace_phone": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(submit_response.status_code, 303)

        with Session(self.engine) as session:
            child = session.exec(
                select(Child).options(selectinload(Child.guardians)).where(Child.id == self.child_id)
            ).first()
            sibling = session.exec(
                select(Child).options(selectinload(Child.guardians)).where(Child.id == self.sibling_id)
            ).first()
            change_request = session.exec(select(ChildProfileChangeRequest)).first()

        self.assertEqual(child.home_address, "Old Home")
        self.assertEqual(sibling.home_address, "Old Home")
        self.assertIsNotNone(change_request)
        self.assertEqual(change_request.status, ChildProfileChangeRequestStatus.pending)
        self.assertEqual(change_request.change_details["home_address"]["new"], "New Home")

        approve_response = self.client.post(
            f"/child-change-requests/{change_request.id}/approve?as=admin",
            data={"review_note": "Looks good"},
            follow_redirects=False,
        )
        self.assertEqual(approve_response.status_code, 303)

        with Session(self.engine) as session:
            children = session.exec(
                select(Child)
                .options(selectinload(Child.guardians))
                .where(Child.id.in_([self.child_id, self.sibling_id]))
                .order_by(Child.id)
            ).all()
            family = session.get(Family, children[0].family_id)
            change_request = session.get(ChildProfileChangeRequest, change_request.id)

        self.assertEqual(children[0].home_address, "New Home")
        self.assertEqual(children[1].home_address, "New Home")
        self.assertEqual(children[0].home_phone, "03-9999-9999")
        self.assertEqual(children[1].guardians[0].workplace, "New Office")
        self.assertEqual(children[0].extra_data["allergy"], ["Egg", "Milk"])
        self.assertEqual(family.home_address, "New Home")
        self.assertEqual(change_request.status, ChildProfileChangeRequestStatus.approved)
        self.assertEqual(change_request.review_note, "Looks good")


if __name__ == "__main__":
    unittest.main()
