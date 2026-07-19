import unittest
from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from child_health_service import build_health_check_chart_records
from models import Child, ChildAllergy, ChildHealthProfile, ChildStatus, Classroom, Family, HealthCheckRecord, HealthCheckType
import routers.child_health as child_health_module
from testing_helpers import authenticate_mock_staff


class ChildHealthRouterTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        self.app = FastAPI()
        self.app.include_router(child_health_module.router)

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.app.dependency_overrides[child_health_module.get_session] = override_get_session
        self.client = TestClient(self.app)
        authenticate_mock_staff(self.client)

        with Session(self.engine) as session:
            classroom = Classroom(name="ひよこ組", display_order=1)
            family = Family(family_name="田中家")
            session.add(classroom)
            session.add(family)
            session.flush()

            child = Child(
                last_name="田中",
                first_name="さくら",
                last_name_kana="タナカ",
                first_name_kana="サクラ",
                birth_date=date(2021, 5, 5),
                enrollment_date=date(2024, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=classroom.id,
                family_id=family.id,
                extra_data={"allergy": ["卵"], "medical_notes": "エピペン携帯"},
            )
            session.add(child)
            session.commit()
            self.child_id = child.id

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_health_summary_bootstraps_legacy_health_data(self):
        response = self.client.get(f"/children/{self.child_id}/health")

        self.assertEqual(response.status_code, 200)
        self.assertIn("卵", response.text)
        self.assertIn("エピペン携帯", response.text)

        with Session(self.engine) as session:
            profile = session.exec(select(ChildHealthProfile).where(ChildHealthProfile.child_id == self.child_id)).first()
            allergies = session.exec(select(ChildAllergy).where(ChildAllergy.child_id == self.child_id)).all()

        self.assertIsNotNone(profile)
        self.assertEqual(profile.medical_history, "エピペン携帯")
        self.assertEqual(len(allergies), 1)
        self.assertEqual(allergies[0].allergen_name, "卵")

    def test_health_overview_lists_children_with_attention_flags(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertIn("健康管理一覧", response.text)
        self.assertIn("田中 さくら", response.text)
        self.assertIn("健診要確認", response.text)
        self.assertIn(f"/children/{self.child_id}/health", response.text)

    def test_can_add_health_check_record_and_render_chart(self):
        response = self.client.post(
            f"/children/{self.child_id}/health/check-records",
            data={
                "check_type": "periodic",
                "checked_at": "2026-04-01",
                "height_cm": "98.4",
                "weight_kg": "14.8",
                "temperature": "36.7",
                "general_condition": "元気",
                "overall_result": "良好",
                "doctor_name": "園医",
                "range_key": "all",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("notice=created", response.headers["location"])

        page = self.client.get(f"/children/{self.child_id}/health/check-records?range=all")
        self.assertEqual(page.status_code, 200)
        self.assertIn("height-chart", page.text)
        self.assertIn("2026-04-01", page.text)
        self.assertIn("98.4", page.text)
        self.assertIn("14.8", page.text)

        with Session(self.engine) as session:
            record = session.exec(select(HealthCheckRecord).where(HealthCheckRecord.child_id == self.child_id)).first()

        self.assertIsNotNone(record)
        self.assertEqual(record.doctor_name, "園医")

    def test_chart_records_keep_same_day_different_check_types(self):
        records = [
            HealthCheckRecord(
                child_id=self.child_id,
                check_type=HealthCheckType.entrance,
                checked_at=date(2026, 4, 1),
                height_cm=97.1,
            ),
            HealthCheckRecord(
                child_id=self.child_id,
                check_type=HealthCheckType.periodic,
                checked_at=date(2026, 4, 1),
                height_cm=98.4,
            ),
        ]

        chart_records = build_health_check_chart_records(records, range_key="all")

        self.assertEqual(len(chart_records), 2)
        self.assertEqual({record.check_type for record in chart_records}, {HealthCheckType.entrance, HealthCheckType.periodic})

    def test_view_only_cannot_post_health_profile(self):
        response = self.client.post(
            f"/children/{self.child_id}/health/profile?as=view_only",
            data={"medical_history": "更新"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)

    def test_health_profile_uses_nursery_priority_management_items(self):
        page = self.client.get(f"/children/{self.child_id}/health/profile")

        self.assertEqual(page.status_code, 200)
        for label in (
            "アレルギーあり",
            "エピペンあり",
            "アナフィラキシーあり",
            "熱性けいれんあり",
            "肘内障あり",
            "与薬あり",
            "その他の管理事項",
        ):
            self.assertIn(label, page.text)
        self.assertNotIn("医療的ケアが必要", page.text)
        self.assertNotIn("SIDS高リスク対象", page.text)

    def test_can_save_priority_management_items(self):
        response = self.client.post(
            f"/children/{self.child_id}/health/profile",
            data={
                "has_allergy": "on",
                "has_epipen": "on",
                "has_anaphylaxis": "on",
                "has_febrile_seizure": "on",
                "has_nursemaids_elbow": "on",
                "has_medication": "on",
                "other_management_items": "食後の運動に注意",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with Session(self.engine) as session:
            profile = session.exec(
                select(ChildHealthProfile).where(ChildHealthProfile.child_id == self.child_id)
            ).first()

        self.assertTrue(profile.has_allergy)
        self.assertTrue(profile.has_epipen)
        self.assertTrue(profile.has_anaphylaxis)
        self.assertTrue(profile.has_febrile_seizure)
        self.assertTrue(profile.has_nursemaids_elbow)
        self.assertTrue(profile.has_medication)
        self.assertEqual(profile.other_management_items, "食後の運動に注意")

    def test_can_edit_existing_allergy(self):
        with Session(self.engine) as session:
            session.add(
                ChildAllergy(
                    child_id=self.child_id,
                    allergen_name="牛乳",
                    severity="mild",
                    is_active=True,
                )
            )
            session.commit()
            allergy = session.exec(
                select(ChildAllergy)
                .where(ChildAllergy.child_id == self.child_id, ChildAllergy.allergen_name == "牛乳")
            ).first()

        edit_page = self.client.get(f"/children/{self.child_id}/health/allergies?edit={allergy.id}")
        self.assertEqual(edit_page.status_code, 200)
        self.assertIn("アレルギーを編集", edit_page.text)
        self.assertIn('value="牛乳"', edit_page.text)

        update_response = self.client.post(
            f"/children/{self.child_id}/health/allergies",
            data={
                "allergy_id": str(allergy.id),
                "allergen_category": "other_food",
                "allergen_name": "牛乳",
                "severity": "severe",
                "symptoms": "発疹",
                "diagnosis_confirmed": "on",
                "removal_required": "on",
                "action_plan": "救急対応",
            },
            follow_redirects=False,
        )
        self.assertEqual(update_response.status_code, 303)
        self.assertIn("notice=updated", update_response.headers["location"])

        with Session(self.engine) as session:
            updated = session.get(ChildAllergy, allergy.id)

        self.assertEqual(updated.severity.value, "severe")
        self.assertEqual(updated.symptoms, "発疹")
        self.assertEqual(updated.action_plan, "救急対応")

    def test_allergy_deactivate_updates_legacy_extra_data(self):
        create_response = self.client.post(
            f"/children/{self.child_id}/health/allergies",
            data={
                "allergen_category": "other_food",
                "allergen_name": "牛乳",
                "severity": "moderate",
                "diagnosis_confirmed": "on",
                "removal_required": "on",
            },
            follow_redirects=False,
        )
        self.assertEqual(create_response.status_code, 303)

        with Session(self.engine) as session:
            child = session.get(Child, self.child_id)
            allergy = session.exec(
                select(ChildAllergy)
                .where(ChildAllergy.child_id == self.child_id, ChildAllergy.allergen_name == "牛乳")
            ).first()

        self.assertIn("牛乳", child.extra_data["allergy"])
        self.assertIsNotNone(allergy)

        deactivate_response = self.client.post(
            f"/children/{self.child_id}/health/allergies/{allergy.id}/deactivate",
            follow_redirects=False,
        )
        self.assertEqual(deactivate_response.status_code, 303)
        self.assertIn("notice=deactivated", deactivate_response.headers["location"])

        with Session(self.engine) as session:
            child = session.get(Child, self.child_id)
            updated = session.get(ChildAllergy, allergy.id)

        self.assertFalse(updated.is_active)
        self.assertNotIn("牛乳", child.extra_data["allergy"])


if __name__ == "__main__":
    unittest.main()
