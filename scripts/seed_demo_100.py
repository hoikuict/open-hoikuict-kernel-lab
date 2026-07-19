from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, text
from sqlmodel import Session, select

from database import create_db_and_tables, engine
from extended_care_fee_service import recalculate_period
from models import (
    AttendanceAlarmHistory, AttendanceAlarmState, AttendanceRecord,
    AttendanceVerification, AttendanceVerificationHistory, Calendar,
    CalendarMember, CalendarUserPreference, Child, ChildAllergy, DailyContactEntry,
    ChildHealthProfile, ChildProfileChangeRequest, Classroom, Event,
    ExtendedCareCharge, ExtendedCareChargeStatus, ExtendedCareFeeRule,
    Family, Guardian, HealthCheckRecord, Message, Notice, NoticeRead,
    NoticeTarget, ParentAccount, ParentChildLink, ProfileChangeNotification,
    Survey, SurveyAnswer, SurveyQuestion, SurveyQuestionOption,
    SurveyResponse, SurveyTarget, User,
    USER_SOURCE_WEB_DEMO,
)

BASE_DIR = Path(__file__).resolve().parents[1]
CSV_DIR = BASE_DIR / "demo_data" / "full"

MODEL_ORDER = [
    ("classrooms", Classroom),
    ("families", Family),
    ("children", Child),
    ("guardians", Guardian),
    ("parent_accounts", ParentAccount),
    ("parent_child_links", ParentChildLink),
    ("child_health_profiles", ChildHealthProfile),
    ("child_allergies", ChildAllergy),
    ("health_check_records", HealthCheckRecord),
    ("users", User),
    ("calendars", Calendar),
    ("calendar_members", CalendarMember),
    ("calendar_user_preferences", CalendarUserPreference),
    ("events", Event),
    ("daily_contact_entries", DailyContactEntry),
    ("attendance_records", AttendanceRecord),
    ("attendance_verifications", AttendanceVerification),
    ("attendance_verification_histories", AttendanceVerificationHistory),
    ("attendance_alarm_states", AttendanceAlarmState),
    ("attendance_alarm_histories", AttendanceAlarmHistory),
    ("notices", Notice),
    ("notice_targets", NoticeTarget),
    ("notice_reads", NoticeRead),
    ("messages", Message),
    ("surveys", Survey),
    ("survey_targets", SurveyTarget),
    ("survey_questions", SurveyQuestion),
    ("survey_question_options", SurveyQuestionOption),
    ("survey_answers", SurveyAnswer),
    ("survey_responses", SurveyResponse),
    ("profile_change_notifications", ProfileChangeNotification),
    ("child_profile_change_requests", ChildProfileChangeRequest),
]

WIPE_ORDER = [
    ExtendedCareCharge,
    ExtendedCareFeeRule,
    *list(reversed([model for _, model in MODEL_ORDER])),
]

DATE_FIELDS = {
    "birth_date", "enrollment_date", "withdrawal_date", "target_date", "attendance_date",
    "diagnosis_date", "source_document_date", "valid_until", "checked_at", "value_date",
    "effective_from", "effective_to",
}
DATETIME_FIELDS = {
    "created_at", "updated_at", "invited_at", "last_login_at", "submitted_at", "reviewed_at",
    "read_at", "publish_start_at", "publish_end_at", "check_in_at", "check_out_at", "evaluated_at",
    "opens_at", "closes_at", "start_at", "end_at", "submitted_at", "split_from_original_start_at",
    "charge_start_at", "actual_check_out_at", "confirmed_at",
}
UUID_FIELDS = {
    "id", "default_calendar_id", "owner_user_id", "calendar_id", "user_id", "actor_user_id",
    "created_by_user_id", "recurrence_rule_id", "split_from_event_id", "staff_user_id",
    "created_by_staff_user_id", "submitted_by_staff_user_id",
}
JSON_FIELDS = {
    "shared_profile", "extra_data", "reasons", "change_details", "request_data", "value_option_ids",
}
BOOL_FIELDS = {
    "is_primary_contact", "diagnosis_confirmed", "removal_required", "is_active", "requires_medical_care",
    "epipen_required", "sids_risk_flag", "has_allergy", "has_epipen", "has_anaphylaxis",
    "has_febrile_seizure", "has_nursemaids_elbow", "has_medication", "breastfed",
    "requires_followup", "is_calendar_admin",
    "is_primary", "is_archived", "is_visible", "is_all_day", "is_deleted", "is_read",
    "is_required", "value_bool",
}
INT_FIELDS = {
    "id", "display_order", "child_id", "classroom_id", "family_id", "older_sibling_id", "order",
    "parent_account_id", "parent_child_link_id", "notice_id", "survey_id", "question_id", "answer_id",
    "created_by_parent_account_id", "submitted_by_parent_account_id", "staff_sort_order", "heart_rate",
    "respiratory_rate", "created_count", "updated_count", "skipped_count", "error_count", "room_id",
    "parent_message_id", "value_scale", "display_order", "attendance_record_id", "rule_id",
    "grace_minutes", "rounding_minutes", "unit_price", "daily_cap_amount", "extended_minutes",
    "billable_units", "auto_amount", "adjustment_amount", "final_amount",
}
FLOAT_FIELDS = {"height_cm", "weight_kg", "head_circumference_cm", "chest_circumference_cm"}

# Fields named id in UUID models must parse as UUID, not int.
UUID_MODEL_TABLES = {
    "users", "calendars", "calendar_members", "calendar_user_preferences", "events",
}

def parse_value(table: str, key: str, value: str) -> Any:
    if value == "":
        return None
    if key in JSON_FIELDS:
        return json.loads(value)
    if key in DATE_FIELDS:
        return date.fromisoformat(value)
    if key in DATETIME_FIELDS:
        return datetime.fromisoformat(value)
    if key in BOOL_FIELDS:
        return value.lower() in {"true", "1", "yes", "y", "はい", "有", "あり"}
    if key in UUID_FIELDS and (key != "id" or table in UUID_MODEL_TABLES):
        return UUID(value)
    if key in INT_FIELDS:
        try:
            return int(value)
        except ValueError:
            return value
    if key == "temperature" and table == "health_check_records":
        return float(value)
    if key in FLOAT_FIELDS:
        return float(value)
    return value

def load_rows(table: str) -> list[dict[str, Any]]:
    path = CSV_DIR / f"{table}.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [
            {key: parse_value(table, key, value) for key, value in row.items() if value != ""}
            for row in reader
        ]

def wipe_all(session: Session) -> None:
    session.exec(text("PRAGMA foreign_keys=OFF"))
    for model in WIPE_ORDER:
        session.exec(delete(model))
    session.commit()
    session.exec(text("PRAGMA foreign_keys=ON"))


def seed_extended_care_demo_data(session: Session) -> dict[str, int]:
    rule = ExtendedCareFeeRule(
        id=1,
        name="標準延長保育料（デモ）",
        effective_from=date(2026, 4, 1),
        start_time="18:00",
        grace_minutes=5,
        rounding_minutes=15,
        unit_price=100,
        daily_cap_amount=None,
        is_active=True,
        created_at=datetime(2026, 4, 1, 9, 0),
        updated_at=datetime(2026, 4, 1, 9, 0),
    )
    session.add(rule)
    session.flush()

    recalculate_period(
        session,
        date(2026, 4, 13),
        date(2026, 5, 15),
        include_locked=True,
    )
    session.flush()

    charges = session.exec(select(ExtendedCareCharge)).all()
    for charge in charges:
        if charge.auto_amount <= 0:
            continue

        confirmed_at = (charge.actual_check_out_at or charge.charge_start_at) + timedelta(minutes=8)
        if charge.attendance_record_id % 29 == 0:
            charge.status = ExtendedCareChargeStatus.excluded
            charge.adjustment_amount = -charge.auto_amount
            charge.final_amount = 0
            charge.adjustment_reason = "デモ対象外: 園判断"
            charge.confirmed_by = "園長"
            charge.confirmed_at = confirmed_at + timedelta(minutes=4)
        elif charge.attendance_record_id % 17 == 0:
            charge.status = ExtendedCareChargeStatus.manual_adjusted
            charge.adjustment_amount = 50
            charge.final_amount = charge.auto_amount + charge.adjustment_amount
            charge.adjustment_reason = "デモ調整: 連絡確認済み"
            charge.confirmed_by = "事務"
            charge.confirmed_at = confirmed_at + timedelta(minutes=2)
        elif charge.attendance_record_id % 5 == 0:
            charge.status = ExtendedCareChargeStatus.confirmed
            charge.confirmed_by = "事務"
            charge.confirmed_at = confirmed_at

        if charge.confirmed_at is not None:
            charge.updated_at = charge.confirmed_at
        session.add(charge)

    return {
        "extended_care_fee_rules": 1,
        "extended_care_charges": len(charges),
    }


def seed(wipe: bool = False) -> dict[str, int]:
    create_db_and_tables()
    counts: dict[str, int] = {}
    with Session(engine) as session:
        if wipe:
            wipe_all(session)
        else:
            existing = session.get(Classroom, 1)
            if existing:
                raise RuntimeError(
                    "既存データがあるようです。デモDBを作り直す場合のみ --wipe-all を付けて実行してください。"
                )
        # The demo set contains a small circular reference between users.default_calendar_id
        # and calendars.owner_user_id. Keep this limited to local/demo seeding only.
        session.exec(text("PRAGMA foreign_keys=OFF"))
        for table, model in MODEL_ORDER:
            rows = load_rows(table)
            if table == "users":
                for row in rows:
                    row.setdefault("provisioning_source", USER_SOURCE_WEB_DEMO)
            for row in rows:
                session.add(model(**row))
            counts[table] = len(rows)
            session.flush()
            if table == "attendance_records":
                counts.update(seed_extended_care_demo_data(session))
        session.commit()
        session.exec(text("PRAGMA foreign_keys=ON"))
    return counts

def main() -> int:
    parser = argparse.ArgumentParser(description="Seed open-hoikuict with a 100-child realistic demo dataset.")
    parser.add_argument("--wipe-all", action="store_true", help="Delete all existing rows in supported demo tables before seeding. Use only for local/demo DBs.")
    args = parser.parse_args()
    try:
        counts = seed(wipe=args.wipe_all)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Demo data seeded successfully.")
    for table, count in counts.items():
        print(f"- {table}: {count}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
