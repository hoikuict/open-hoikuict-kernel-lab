from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, text
from sqlmodel import Session

from database import create_db_and_tables, engine
from models import (
    AttendanceAlarmHistory, AttendanceAlarmState, AttendanceRecord,
    AttendanceVerification, AttendanceVerificationHistory, Calendar,
    CalendarMember, CalendarUserPreference, Child, ChildAllergy, DailyContactEntry,
    ChildHealthProfile, ChildProfileChangeRequest, Classroom, Event,
    Family, Guardian, HealthCheckRecord, Message, Notice, NoticeRead,
    NoticeTarget, ParentAccount, ParentChildLink, ProfileChangeNotification,
    Survey, SurveyAnswer, SurveyQuestion, SurveyQuestionOption,
    SurveyResponse, SurveyTarget, User,
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

WIPE_ORDER = list(reversed([model for _, model in MODEL_ORDER]))

DATE_FIELDS = {
    "birth_date", "enrollment_date", "withdrawal_date", "target_date", "attendance_date",
    "diagnosis_date", "source_document_date", "valid_until", "checked_at", "value_date",
}
DATETIME_FIELDS = {
    "created_at", "updated_at", "invited_at", "last_login_at", "submitted_at", "reviewed_at",
    "read_at", "publish_start_at", "publish_end_at", "check_in_at", "check_out_at", "evaluated_at",
    "opens_at", "closes_at", "start_at", "end_at", "submitted_at", "split_from_original_start_at",
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
    "epipen_required", "sids_risk_flag", "breastfed", "requires_followup", "is_calendar_admin",
    "is_primary", "is_archived", "is_visible", "is_all_day", "is_deleted", "is_read",
}
INT_FIELDS = {
    "id", "display_order", "child_id", "classroom_id", "family_id", "older_sibling_id", "order",
    "parent_account_id", "parent_child_link_id", "notice_id", "survey_id", "question_id", "answer_id",
    "created_by_parent_account_id", "submitted_by_parent_account_id", "staff_sort_order", "heart_rate",
    "respiratory_rate", "created_count", "updated_count", "skipped_count", "error_count", "room_id",
    "parent_message_id", "value_scale", "display_order",
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
            for row in rows:
                session.add(model(**row))
            counts[table] = len(rows)
            session.flush()
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
