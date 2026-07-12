from __future__ import annotations

import logging
import os
from datetime import date, timedelta

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import make_url
from sqlmodel import SQLModel, Session, create_engine, select

from family_support import bootstrap_family_data, sync_parent_child_links, sync_family_to_children
from time_utils import local_today, utc_now

DATABASE_URL = os.getenv("HOIKUICT_DATABASE_URL", "sqlite:///./hoikuict.db")
_database_url = make_url(DATABASE_URL)
_is_sqlite_url = _database_url.get_backend_name() == "sqlite"
engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"timeout": 15} if _is_sqlite_url else {},
)
logger = logging.getLogger(__name__)


if _is_sqlite_url:
    @event.listens_for(engine, "connect")
    def _set_sqlite_connection_pragmas(dbapi_connection, connection_record) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

DEFAULT_CLASSROOMS = [
    ("ひよこ組", 1),
    ("うさぎ組", 2),
    ("きりん組", 3),
]


def get_session():
    with Session(engine) as session:
        yield session


def create_db_and_tables() -> None:
    import models  # noqa: F401
    import plan_docs.db_models  # noqa: F401

    if engine.dialect.name != "sqlite":
        if os.getenv("HOIKUICT_ALLOW_UNMANAGED_SCHEMA") != "1":
            raise RuntimeError(
                "組み込みマイグレーションはSQLite専用です。"
                "他DBではHOIKUICT_ALLOW_UNMANAGED_SCHEMA=1と外部管理済みスキーマが必要です。"
            )
        _validate_unmanaged_schema()
        return

    _enable_sqlite_wal()
    SQLModel.metadata.create_all(engine)
    _migrate_add_child_columns()
    _migrate_add_attendance_columns()
    _migrate_add_daily_contact_columns()
    _migrate_add_parent_account_columns()
    _migrate_add_family_columns()
    _migrate_add_message_columns()
    _migrate_add_calendar_columns()
    _migrate_survey_tables()
    _migrate_billing_fee_labels()
    _migrate_zengin_workflow()
    _validate_sqlite_foreign_keys()


def _enable_sqlite_wal() -> None:
    database_name = engine.url.database
    if not database_name or database_name == ":memory:":
        return
    with engine.connect() as conn:
        mode = str(conn.execute(text("PRAGMA journal_mode=WAL")).scalar_one()).lower()
    if mode != "wal":
        raise RuntimeError(f"SQLite WALを有効化できませんでした: journal_mode={mode}")


def _validate_sqlite_foreign_keys() -> None:
    with engine.connect() as conn:
        violations = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
    if violations:
        sample = ", ".join(str(tuple(row)) for row in violations[:5])
        raise RuntimeError(f"SQLite外部キー違反があります: {sample}")


def _validate_unmanaged_schema() -> None:
    db_inspector = inspect(engine)
    existing_tables = set(db_inspector.get_table_names())
    missing_tables = sorted(set(SQLModel.metadata.tables) - existing_tables)
    missing_columns: list[str] = []
    for table_name, table in SQLModel.metadata.tables.items():
        if table_name not in existing_tables:
            continue
        existing_columns = {item["name"] for item in db_inspector.get_columns(table_name)}
        for column_name in table.columns.keys():
            if column_name not in existing_columns:
                missing_columns.append(f"{table_name}.{column_name}")
    if missing_tables or missing_columns:
        raise RuntimeError(
            "外部管理スキーマが不足しています: "
            f"tables={missing_tables[:10]}, columns={missing_columns[:20]}"
        )


def _table_columns(table_name: str) -> list[str]:
    with engine.connect() as conn:
        result = conn.execute(text(f"PRAGMA table_info({table_name})"))
        return [row[1] for row in result]


def _log_migration_skip(migration_name: str, exc: Exception) -> None:
    raise RuntimeError(f"{migration_name} migration failed") from exc


def _migrate_add_child_columns() -> None:
    try:
        with engine.connect() as conn:
            cols = _table_columns("children")
            if not cols:
                return
            if "home_address" not in cols:
                conn.execute(text("ALTER TABLE children ADD COLUMN home_address VARCHAR"))
            if "home_phone" not in cols:
                conn.execute(text("ALTER TABLE children ADD COLUMN home_phone VARCHAR"))
            if "older_sibling_id" not in cols:
                conn.execute(text("ALTER TABLE children ADD COLUMN older_sibling_id INTEGER REFERENCES children(id)"))
            if "classroom_id" not in cols:
                conn.execute(text("ALTER TABLE children ADD COLUMN classroom_id INTEGER REFERENCES classrooms(id)"))
            conn.commit()
    except Exception as exc:
        _log_migration_skip("children column", exc)


def _migrate_add_attendance_columns() -> None:
    try:
        with engine.connect() as conn:
            cols = _table_columns("attendance_records")
            if not cols:
                return
            if "planned_pickup_time" not in cols:
                conn.execute(text("ALTER TABLE attendance_records ADD COLUMN planned_pickup_time VARCHAR"))
            if "pickup_person" not in cols:
                conn.execute(text("ALTER TABLE attendance_records ADD COLUMN pickup_person VARCHAR"))
            if "snack_required" not in cols:
                conn.execute(text("ALTER TABLE attendance_records ADD COLUMN snack_required BOOLEAN DEFAULT 0 NOT NULL"))
            conn.commit()
    except Exception as exc:
        _log_migration_skip("attendance column", exc)


def _migrate_add_daily_contact_columns() -> None:
    try:
        with engine.connect() as conn:
            cols = _table_columns("daily_contact_entries")
            if not cols:
                return
            if "contact_type" not in cols:
                conn.execute(text("ALTER TABLE daily_contact_entries ADD COLUMN contact_type VARCHAR DEFAULT 'present'"))
            if "absence_temperature" not in cols:
                conn.execute(text("ALTER TABLE daily_contact_entries ADD COLUMN absence_temperature VARCHAR"))
            if "absence_symptoms" not in cols:
                conn.execute(text("ALTER TABLE daily_contact_entries ADD COLUMN absence_symptoms VARCHAR"))
            if "absence_diagnosis" not in cols:
                conn.execute(text("ALTER TABLE daily_contact_entries ADD COLUMN absence_diagnosis VARCHAR"))
            if "absence_note" not in cols:
                conn.execute(text("ALTER TABLE daily_contact_entries ADD COLUMN absence_note VARCHAR"))
            conn.commit()
    except Exception as exc:
        _log_migration_skip("daily contact column", exc)


def _migrate_add_parent_account_columns() -> None:
    try:
        with engine.connect() as conn:
            cols = _table_columns("parent_accounts")
            if not cols:
                return
            if "home_address" not in cols:
                conn.execute(text("ALTER TABLE parent_accounts ADD COLUMN home_address VARCHAR"))
            if "workplace" not in cols:
                conn.execute(text("ALTER TABLE parent_accounts ADD COLUMN workplace VARCHAR"))
            if "workplace_address" not in cols:
                conn.execute(text("ALTER TABLE parent_accounts ADD COLUMN workplace_address VARCHAR"))
            if "workplace_phone" not in cols:
                conn.execute(text("ALTER TABLE parent_accounts ADD COLUMN workplace_phone VARCHAR"))
            conn.commit()
    except Exception as exc:
        _log_migration_skip("parent account column", exc)


def _migrate_add_family_columns() -> None:
    try:
        with engine.connect() as conn:
            child_cols = _table_columns("children")
            if child_cols and "family_id" not in child_cols:
                conn.execute(text("ALTER TABLE children ADD COLUMN family_id INTEGER REFERENCES families(id)"))

            parent_cols = _table_columns("parent_accounts")
            if parent_cols and "family_id" not in parent_cols:
                conn.execute(text("ALTER TABLE parent_accounts ADD COLUMN family_id INTEGER REFERENCES families(id)"))
            conn.commit()
    except Exception as exc:
        _log_migration_skip("family column", exc)


def _migrate_add_message_columns() -> None:
    try:
        with engine.connect() as conn:
            message_cols = _table_columns("messages")
            if message_cols:
                if "parent_message_id" not in message_cols:
                    conn.execute(text("ALTER TABLE messages ADD COLUMN parent_message_id INTEGER REFERENCES messages(id)"))
                if "deleted_at" not in message_cols:
                    conn.execute(text("ALTER TABLE messages ADD COLUMN deleted_at DATETIME"))
                if "deleted_by" not in message_cols:
                    conn.execute(text("ALTER TABLE messages ADD COLUMN deleted_by VARCHAR"))
            conn.commit()
    except Exception as exc:
        _log_migration_skip("message column", exc)


def _migrate_add_calendar_columns() -> None:
    try:
        with engine.connect() as conn:
            calendar_cols = _table_columns("calendars")
            if calendar_cols and "calendar_type" not in calendar_cols:
                conn.execute(text("ALTER TABLE calendars ADD COLUMN calendar_type VARCHAR DEFAULT 'staff_personal'"))
            user_cols = _table_columns("users")
            if user_cols and "is_calendar_admin" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN is_calendar_admin BOOLEAN DEFAULT 0"))
            if user_cols and "staff_role" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN staff_role VARCHAR DEFAULT 'can_edit'"))
            if user_cols and "staff_sort_order" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN staff_sort_order INTEGER DEFAULT 100"))
            if user_cols and "can_manage_child_records" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN can_manage_child_records BOOLEAN DEFAULT 0"))
            if user_cols and "provisioning_source" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN provisioning_source VARCHAR DEFAULT 'manual'"))
                user_cols.append("provisioning_source")
            if user_cols and "provisioning_source" in user_cols:
                conn.execute(
                    text(
                        """
                        UPDATE users
                        SET provisioning_source = 'local_sample'
                        WHERE email IN (
                            'principal@example.com',
                            'chief@example.com',
                            'nurse@example.com',
                            'nutritionist@example.com',
                            'office@example.com',
                            'hiyoko@example.com',
                            'hiyoko-b@example.com',
                            'takenoko@example.com',
                            'risu-b@example.com',
                            'kinoko@example.com',
                            'usagi-b@example.com',
                            'panda-a@example.com',
                            'panda-b@example.com',
                            'kirin-a@example.com',
                            'kirin-b@example.com',
                            'zou-a@example.com',
                            'zou-b@example.com',
                            'part@example.com',
                            'arbeit@example.com'
                        )
                          AND (
                            provisioning_source IS NULL
                            OR provisioning_source = ''
                            OR provisioning_source = 'manual'
                          )
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        UPDATE users
                        SET provisioning_source = 'web_demo'
                        WHERE email LIKE '%@demo.open-hoikuict.example'
                          AND (
                            provisioning_source IS NULL
                            OR provisioning_source = ''
                            OR provisioning_source = 'manual'
                          )
                        """
                    )
                )
            conn.commit()
    except Exception as exc:
        _log_migration_skip("calendar column", exc)


def _migrate_survey_tables() -> None:
    # New survey tables are created by SQLModel.metadata.create_all().
    # Keep this hook explicit for future additive indexes or backfills.
    return


def _migrate_billing_fee_labels() -> None:
    try:
        with engine.connect() as conn:
            fee_cols = _table_columns("fee_items")
            line_cols = _table_columns("billing_charge_lines")
            if not fee_cols:
                return

            conn.execute(
                text(
                    """
                    UPDATE fee_items
                    SET name = '延長保育料（月額）'
                    WHERE code = 'monthly_childcare'
                      AND name IN ('保育料', '保育料（月額）')
                    """
                )
            )
            if line_cols:
                conn.execute(
                    text(
                        """
                        UPDATE billing_charge_lines
                        SET description = '延長保育料（月額）'
                        WHERE fee_item_id IN (
                            SELECT id FROM fee_items WHERE code = 'monthly_childcare'
                        )
                          AND description IN ('保育料', '保育料（月額）')
                        """
                    )
                )
            conn.commit()
    except Exception as exc:
        _log_migration_skip("billing fee label", exc)


def _migrate_zengin_workflow() -> None:
    with engine.begin() as conn:
        profile_cols = _table_columns("family_billing_profiles")
        export_cols = _table_columns("zengin_exports")
        if profile_cols and "new_code_consumed_by_export_id" not in profile_cols:
            conn.execute(
                text(
                    "ALTER TABLE family_billing_profiles "
                    "ADD COLUMN new_code_consumed_by_export_id INTEGER "
                    "REFERENCES zengin_exports(id)"
                )
            )
        if profile_cols:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_family_billing_profiles_new_code_consumed_by_export_id "
                    "ON family_billing_profiles (new_code_consumed_by_export_id)"
                )
            )
        if export_cols and "submitted_at" not in export_cols:
            conn.execute(text("ALTER TABLE zengin_exports ADD COLUMN submitted_at DATETIME"))
            if "downloaded_at" in export_cols:
                conn.execute(
                    text(
                        "UPDATE zengin_exports SET submitted_at = downloaded_at "
                        "WHERE submitted_at IS NULL"
                    )
                )
        if export_cols:
            conn.execute(
                text(
                    "UPDATE zengin_exports SET status = 'submitted' "
                    "WHERE status = 'downloaded'"
                )
            )
            conn.execute(
                text(
                    "UPDATE zengin_exports SET status = 'superseded' "
                    "WHERE status = 'reissued'"
                )
            )


def seed_classroom_data() -> None:
    from models import Classroom

    with Session(engine) as session:
        classrooms = session.exec(select(Classroom).order_by(Classroom.display_order, Classroom.id)).all()
        if classrooms:
            return

        for name, order in DEFAULT_CLASSROOMS:
            session.add(Classroom(name=name, display_order=order))

        session.commit()


def seed_extended_care_fee_rules() -> None:
    from models import ExtendedCareFeeRule

    with Session(engine) as session:
        if session.exec(select(ExtendedCareFeeRule)).first():
            return

        session.add(
            ExtendedCareFeeRule(
                name="標準延長保育料",
                effective_from=date(2020, 1, 1),
                start_time="18:00",
                grace_minutes=5,
                rounding_minutes=15,
                unit_price=100,
                daily_cap_amount=None,
                is_active=True,
            )
        )
        session.commit()


def seed_sample_data() -> None:
    from models import Child, ChildStatus, Classroom, Family, Guardian

    with Session(engine) as session:
        if session.exec(select(Child)).first():
            return

        classrooms = session.exec(select(Classroom).order_by(Classroom.display_order, Classroom.id)).all()
        classroom_ids = [classroom.id for classroom in classrooms if classroom.id is not None]

        def classroom_id_at(index: int) -> int | None:
            if not classroom_ids:
                return None
            return classroom_ids[min(index, len(classroom_ids) - 1)]

        tanaka_family = Family(
            family_name="田中家",
            home_address="東京都渋谷区1-2-3",
            home_phone="03-1234-5678",
            shared_profile={
                "guardians": [
                    {
                        "order": 1,
                        "last_name": "田中",
                        "first_name": "真由美",
                        "last_name_kana": "タナカ",
                        "first_name_kana": "マユミ",
                        "relationship": "母",
                        "phone": "090-1111-2222",
                        "workplace": "サンプル商事",
                        "workplace_address": "東京都港区1-1-1",
                        "workplace_phone": "03-1111-2222",
                    },
                    {
                        "order": 2,
                        "last_name": "田中",
                        "first_name": "健一",
                        "last_name_kana": "タナカ",
                        "first_name_kana": "ケンイチ",
                        "relationship": "父",
                        "phone": "090-3333-4444",
                        "workplace": "サンプル工業",
                        "workplace_address": "東京都品川区2-2-2",
                        "workplace_phone": "03-3333-4444",
                    },
                ]
            },
        )
        sato_family = Family(
            family_name="佐藤家",
            home_address="東京都新宿区4-5-6",
            home_phone="03-2345-6789",
            shared_profile={
                "guardians": [
                    {
                        "order": 1,
                        "last_name": "佐藤",
                        "first_name": "真由美",
                        "last_name_kana": "サトウ",
                        "first_name_kana": "マユミ",
                        "relationship": "母",
                        "phone": "090-5555-6666",
                        "workplace": "グリーン企画",
                        "workplace_address": "東京都新宿区7-8-9",
                        "workplace_phone": "03-5555-6666",
                    }
                ]
            },
        )
        ito_family = Family(
            family_name="伊藤家",
            home_address="東京都目黒区9-8-7",
            home_phone="03-3456-7890",
            shared_profile={
                "guardians": [
                    {
                        "order": 1,
                        "last_name": "伊藤",
                        "first_name": "恵",
                        "last_name_kana": "イトウ",
                        "first_name_kana": "メグミ",
                        "relationship": "母",
                        "phone": "090-7777-8888",
                        "workplace": "ブルークリニック",
                        "workplace_address": "東京都目黒区3-3-3",
                        "workplace_phone": "03-7777-8888",
                    }
                ]
            },
        )
        session.add(tanaka_family)
        session.add(sato_family)
        session.add(ito_family)
        session.flush()

        children = [
            Child(
                last_name="田中",
                first_name="さくら",
                last_name_kana="タナカ",
                first_name_kana="サクラ",
                birth_date=date(2020, 4, 5),
                enrollment_date=date(2023, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=classroom_id_at(1),
                family_id=tanaka_family.id,
                extra_data={"allergy": ["卵"], "medical_notes": "特記事項なし"},
            ),
            Child(
                last_name="田中",
                first_name="はると",
                last_name_kana="タナカ",
                first_name_kana="ハルト",
                birth_date=date(2021, 8, 12),
                enrollment_date=date(2024, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=classroom_id_at(0),
                family_id=tanaka_family.id,
                extra_data={"allergy": [], "medical_notes": ""},
            ),
            Child(
                last_name="佐藤",
                first_name="真由美",
                last_name_kana="サトウ",
                first_name_kana="マユミ",
                birth_date=date(2019, 6, 15),
                enrollment_date=date(2022, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=classroom_id_at(2),
                family_id=sato_family.id,
                extra_data={"allergy": ["乳"], "medical_notes": "エピペン持参"},
            ),
            Child(
                last_name="伊藤",
                first_name="ネオ",
                last_name_kana="イトウ",
                first_name_kana="ネオ",
                birth_date=date(2021, 4, 12),
                enrollment_date=date(2024, 4, 1),
                status=ChildStatus.enrolled,
                classroom_id=classroom_id_at(0),
                family_id=ito_family.id,
                extra_data={"allergy": [], "medical_notes": ""},
            ),
            Child(
                last_name="山田",
                first_name="こうた",
                last_name_kana="ヤマダ",
                first_name_kana="コウタ",
                birth_date=date(2018, 11, 3),
                enrollment_date=date(2021, 4, 1),
                withdrawal_date=date(2025, 3, 31),
                status=ChildStatus.graduated,
                extra_data={"allergy": [], "medical_notes": ""},
            ),
        ]
        for child in children:
            session.add(child)
        session.flush()

        children[1].older_sibling_id = children[0].id
        session.add(children[1])

        for family in (tanaka_family, sato_family, ito_family):
            sync_family_to_children(session, family, updated_at=utc_now())

        session.add(
            Guardian(
                child_id=children[4].id,
                last_name="山田",
                first_name="太郎",
                relationship="父",
                phone="090-9999-0000",
                workplace="レッド建設",
                workplace_address="東京都世田谷区5-5-5",
                workplace_phone="03-9999-0000",
                order=1,
            )
        )

        session.commit()


def seed_parent_portal_data() -> None:
    from models import (
        Child,
        ChildStatus,
        DailyContactEntry,
        Family,
        Notice,
        NoticePriority,
        NoticeRead,
        NoticeStatus,
        NoticeTarget,
        NoticeTargetType,
        ParentAccount,
        ParentAccountStatus,
    )

    with Session(engine) as session:
        if session.exec(select(ParentAccount)).first():
            return

        families = session.exec(
            select(Family).order_by(Family.id)
        ).all()
        if not families:
            bootstrap_family_data(session)
            session.flush()
            families = session.exec(select(Family).order_by(Family.id)).all()
        if not families:
            return

        tanaka_family = families[0]
        sato_family = families[1] if len(families) > 1 else families[0]

        accounts = [
            ParentAccount(
                display_name="田中 健一",
                email="tanaka.parent@example.com",
                phone="090-1111-0001",
                home_address=tanaka_family.home_address,
                workplace="サンプル商事",
                workplace_address="東京都港区1-1-1",
                workplace_phone="03-1111-1111",
                family_id=tanaka_family.id,
                status=ParentAccountStatus.active,
                invited_at=utc_now(),
            ),
            ParentAccount(
                display_name="佐藤 真由美",
                email="sato.parent@example.com",
                phone="090-2222-0002",
                home_address=sato_family.home_address,
                workplace="グリーン企画",
                workplace_address="東京都新宿区7-8-9",
                workplace_phone="03-2222-2222",
                family_id=sato_family.id,
                status=ParentAccountStatus.active,
                invited_at=utc_now(),
            ),
        ]
        for account in accounts:
            session.add(account)
        session.flush()

        families = session.exec(
            select(Family)
            .where(Family.id.in_([tanaka_family.id, sato_family.id]))
        ).all()
        for family in families:
            session.refresh(family)
            sync_parent_child_links(session, family)

        today = local_today()
        enrolled_children = session.exec(
            select(Child)
            .where(Child.status == ChildStatus.enrolled)
            .order_by(Child.last_name_kana, Child.first_name_kana)
        ).all()
        tanaka_child = next((child for child in enrolled_children if child.family_id == tanaka_family.id), None)
        sato_child = next((child for child in enrolled_children if child.family_id == sato_family.id), None)

        if tanaka_child:
            session.add(
                DailyContactEntry(
                    child_id=tanaka_child.id,
                    parent_account_id=accounts[0].id,
                    target_date=today,
                    temperature="36.7",
                    sleep_notes="21:00-6:30",
                    breakfast_status="完食",
                    bowel_movement_status="あり",
                    mood="元気",
                    cough="なし",
                    runny_nose="なし",
                    medication="なし",
                    condition_note="朝から元気です。",
                    contact_note="本日は16:30ごろお迎え予定です。",
                    submitted_at=utc_now(),
                )
            )
        if sato_child:
            session.add(
                DailyContactEntry(
                    child_id=sato_child.id,
                    parent_account_id=accounts[1].id,
                    target_date=today,
                    temperature="37.0",
                    sleep_notes="20:30-6:00",
                    breakfast_status="少なめ",
                    bowel_movement_status="なし",
                    mood="少し眠そう",
                    cough="少し",
                    runny_nose="なし",
                    medication="なし",
                    condition_note="少し鼻水があります。",
                    contact_note="様子を見てください。",
                    submitted_at=utc_now(),
                )
            )

        all_notice = Notice(
            title="今週の持ち物について",
            body="来週は避難訓練があります。カラー帽子と上履きを忘れずにお持ちください。",
            priority=NoticePriority.normal,
            status=NoticeStatus.published,
            publish_start_at=utc_now() - timedelta(hours=2),
            created_by="管理者サンプル",
        )
        session.add(all_notice)
        session.flush()
        session.add(NoticeTarget(notice_id=all_notice.id, target_type=NoticeTargetType.all))

        if tanaka_child and tanaka_child.classroom_id:
            classroom_notice = Notice(
                title="クラス懇談会のお知らせ",
                body="今週金曜日の16:00よりクラス懇談会を行います。ご都合をお知らせください。",
                priority=NoticePriority.high,
                status=NoticeStatus.published,
                publish_start_at=utc_now() - timedelta(hours=2),
                created_by="管理者サンプル",
            )
            session.add(classroom_notice)
            session.flush()
            session.add(
                NoticeTarget(
                    notice_id=classroom_notice.id,
                    target_type=NoticeTargetType.classroom,
                    target_value=str(tanaka_child.classroom_id),
                )
            )

        if sato_child:
            child_notice = Notice(
                title="個別連絡",
                body=f"{sato_child.full_name} さんの体調確認をお願いします。",
                priority=NoticePriority.high,
                status=NoticeStatus.published,
                publish_start_at=utc_now() - timedelta(hours=2),
                created_by="管理者サンプル",
            )
            session.add(child_notice)
            session.flush()
            session.add(
                NoticeTarget(
                    notice_id=child_notice.id,
                    target_type=NoticeTargetType.child,
                    target_value=str(sato_child.id),
                )
            )

        session.flush()
        session.add(
            NoticeRead(
                notice_id=all_notice.id,
                parent_account_id=accounts[0].id,
                read_at=utc_now(),
            )
        )
        session.commit()


def bootstrap_family_records() -> None:
    with Session(engine) as session:
        bootstrap_family_data(session)
        session.commit()


def bootstrap_health_records() -> None:
    from child_health_service import sync_health_records_from_legacy_extra_data
    from models import Child

    with Session(engine) as session:
        children = session.exec(select(Child)).all()
        changed = False
        for child in children:
            changed = sync_health_records_from_legacy_extra_data(session, child) or changed
        if changed:
            session.commit()


def seed_calendar_data() -> None:
    from models import (
        Calendar,
        CalendarMember,
        CalendarMemberRole,
        CalendarType,
        CalendarUserPreference,
        USER_SOURCE_LOCAL_SAMPLE,
        USER_SOURCE_WEB_DEMO,
        User,
    )

    with Session(engine) as session:
        web_demo_users = session.exec(
            select(User).where(
                User.provisioning_source == USER_SOURCE_WEB_DEMO,
                User.is_active.is_(True),
            )
        ).all()
        if web_demo_users:
            web_demo_identities = {
                (user.display_name.strip(), user.staff_role)
                for user in web_demo_users
            }
            local_duplicates = session.exec(
                select(User).where(
                    User.provisioning_source == USER_SOURCE_LOCAL_SAMPLE,
                    User.is_active.is_(True),
                )
            ).all()
            changed = False
            for user in local_duplicates:
                if (user.display_name.strip(), user.staff_role) not in web_demo_identities:
                    continue
                user.is_active = False
                user.updated_at = utc_now()
                session.add(user)
                changed = True
            if changed:
                session.commit()
            return

        staff_specs = [
            {"email": "principal@example.com", "display_name": "園長", "staff_role": "admin", "staff_sort_order": 10, "color": "#2563EB", "can_manage_child_records": True},
            {"email": "chief@example.com", "display_name": "主任", "staff_role": "admin", "staff_sort_order": 20, "color": "#7C3AED", "can_manage_child_records": True},
            {"email": "nurse@example.com", "display_name": "看護師", "staff_role": "can_edit", "staff_sort_order": 30, "color": "#0891B2", "can_manage_child_records": False},
            {"email": "nutritionist@example.com", "display_name": "栄養士", "staff_role": "can_edit", "staff_sort_order": 35, "color": "#65A30D", "can_manage_child_records": False},
            {"email": "office@example.com", "display_name": "事務", "staff_role": "can_edit", "staff_sort_order": 40, "color": "#9333EA", "can_manage_child_records": True},
            {"email": "hiyoko@example.com", "display_name": "ひよこ組担任A", "staff_role": "can_edit", "staff_sort_order": 60, "color": "#F59E0B", "can_manage_child_records": False},
            {"email": "hiyoko-b@example.com", "display_name": "ひよこ組担任B", "staff_role": "can_edit", "staff_sort_order": 61, "color": "#F97316", "can_manage_child_records": False},
            {"email": "takenoko@example.com", "display_name": "りす組担任A", "staff_role": "can_edit", "staff_sort_order": 70, "color": "#10B981", "can_manage_child_records": False},
            {"email": "risu-b@example.com", "display_name": "りす組担任B", "staff_role": "can_edit", "staff_sort_order": 71, "color": "#14B8A6", "can_manage_child_records": False},
            {"email": "kinoko@example.com", "display_name": "うさぎ組担任A", "staff_role": "can_edit", "staff_sort_order": 80, "color": "#EC4899", "can_manage_child_records": False},
            {"email": "usagi-b@example.com", "display_name": "うさぎ組担任B", "staff_role": "can_edit", "staff_sort_order": 81, "color": "#F43F5E", "can_manage_child_records": False},
            {"email": "panda-a@example.com", "display_name": "ぱんだ組担任A", "staff_role": "can_edit", "staff_sort_order": 90, "color": "#8B5CF6", "can_manage_child_records": False},
            {"email": "panda-b@example.com", "display_name": "ぱんだ組担任B", "staff_role": "can_edit", "staff_sort_order": 91, "color": "#A855F7", "can_manage_child_records": False},
            {"email": "kirin-a@example.com", "display_name": "きりん組担任A", "staff_role": "can_edit", "staff_sort_order": 100, "color": "#0EA5E9", "can_manage_child_records": False},
            {"email": "kirin-b@example.com", "display_name": "きりん組担任B", "staff_role": "can_edit", "staff_sort_order": 101, "color": "#38BDF8", "can_manage_child_records": False},
            {"email": "zou-a@example.com", "display_name": "ぞう組担任A", "staff_role": "can_edit", "staff_sort_order": 110, "color": "#2563EB", "can_manage_child_records": False},
            {"email": "zou-b@example.com", "display_name": "ぞう組担任B", "staff_role": "can_edit", "staff_sort_order": 111, "color": "#1D4ED8", "can_manage_child_records": False},
            {"email": "part@example.com", "display_name": "早番パート", "staff_role": "view_only", "staff_sort_order": 150, "color": "#64748B", "can_manage_child_records": False},
            {"email": "arbeit@example.com", "display_name": "遅番パート", "staff_role": "view_only", "staff_sort_order": 151, "color": "#475569", "can_manage_child_records": False},
        ]

        def ensure_user(
            *,
            email: str,
            display_name: str,
            staff_role: str,
            staff_sort_order: int,
            can_manage_child_records: bool,
        ) -> User:
            is_calendar_admin = staff_role == "admin"
            user = session.exec(select(User).where(User.email == email)).first()
            if user is None:
                user = User(
                    email=email,
                    display_name=display_name,
                    timezone="Asia/Tokyo",
                    locale="ja-JP",
                    staff_role=staff_role,
                    staff_sort_order=staff_sort_order,
                    is_calendar_admin=is_calendar_admin,
                    can_manage_child_records=can_manage_child_records,
                    provisioning_source=USER_SOURCE_LOCAL_SAMPLE,
                )
            else:
                user.display_name = display_name
                user.timezone = user.timezone or "Asia/Tokyo"
                user.locale = user.locale or "ja-JP"
                user.staff_role = staff_role
                user.staff_sort_order = staff_sort_order
                user.is_calendar_admin = is_calendar_admin
                user.can_manage_child_records = can_manage_child_records
                user.provisioning_source = USER_SOURCE_LOCAL_SAMPLE
                user.is_active = True
                user.updated_at = utc_now()
            session.add(user)
            session.flush()
            return user

        def ensure_member(calendar: Calendar, user: User, role: CalendarMemberRole) -> None:
            member = session.exec(
                select(CalendarMember).where(
                    CalendarMember.calendar_id == calendar.id,
                    CalendarMember.user_id == user.id,
                )
            ).first()
            if member is None:
                member = CalendarMember(calendar_id=calendar.id, user_id=user.id, role=role)
            else:
                member.role = role
                member.updated_at = utc_now()
            session.add(member)
            session.flush()

        def ensure_preference(calendar: Calendar, user: User, *, display_order: int) -> None:
            preference = session.exec(
                select(CalendarUserPreference).where(
                    CalendarUserPreference.calendar_id == calendar.id,
                    CalendarUserPreference.user_id == user.id,
                )
            ).first()
            if preference is None:
                preference = CalendarUserPreference(
                    calendar_id=calendar.id,
                    user_id=user.id,
                    is_visible=True,
                    display_order=display_order,
                )
            else:
                preference.is_visible = True
                preference.display_order = display_order
                preference.updated_at = utc_now()
            session.add(preference)
            session.flush()

        def membership_count(calendar_id) -> int:
            return len(
                session.exec(select(CalendarMember).where(CalendarMember.calendar_id == calendar_id)).all()
            )

        def shared_role_for_user(calendar: Calendar, user: User) -> CalendarMemberRole:
            if user.id == calendar.owner_user_id:
                return CalendarMemberRole.owner
            return CalendarMemberRole.editor if user.can_edit_calendar else CalendarMemberRole.viewer

        active_users = [
            ensure_user(
                email=spec["email"],
                display_name=spec["display_name"],
                staff_role=spec["staff_role"],
                staff_sort_order=spec["staff_sort_order"],
                can_manage_child_records=spec["can_manage_child_records"],
            )
            for spec in staff_specs
        ]
        lead_user = active_users[0]

        for spec, user in zip(staff_specs, active_users):
            personal_calendar = session.exec(
                select(Calendar).where(
                    Calendar.owner_user_id == user.id,
                    Calendar.is_primary.is_(True),
                )
            ).first()
            if personal_calendar is None:
                personal_calendar = session.exec(
                    select(Calendar).where(
                        Calendar.owner_user_id == user.id,
                        Calendar.calendar_type == CalendarType.staff_personal,
                    )
                ).first()
            if personal_calendar is None:
                personal_calendar = Calendar(owner_user_id=user.id)
            personal_calendar.name = f"{user.display_name}の個人カレンダー"
            personal_calendar.calendar_type = CalendarType.staff_personal
            personal_calendar.color = spec["color"]
            personal_calendar.description = "職員ごとの個人用カレンダー"
            personal_calendar.is_primary = True
            personal_calendar.is_archived = False
            personal_calendar.updated_at = utc_now()
            session.add(personal_calendar)
            session.flush()

            ensure_member(personal_calendar, user, CalendarMemberRole.owner)
            ensure_preference(personal_calendar, user, display_order=10)

            user.default_calendar_id = personal_calendar.id
            user.updated_at = utc_now()
            session.add(user)

        rename_map = {
            "A Shared Team": "施設共用カレンダー",
            "B Shared Review": "行事共有カレンダー",
        }
        facility_calendars = []
        for calendar in session.exec(select(Calendar)).all():
            if calendar.name in rename_map:
                calendar.name = rename_map[calendar.name]
            if calendar.is_primary:
                calendar.calendar_type = CalendarType.staff_personal
            elif membership_count(calendar.id) > 1:
                calendar.calendar_type = CalendarType.facility_shared
            if calendar.calendar_type == CalendarType.facility_shared and not calendar.is_archived:
                facility_calendars.append(calendar)
            session.add(calendar)

        if not facility_calendars:
            shared_calendar = Calendar(
                owner_user_id=lead_user.id,
                name="施設共用カレンダー",
                calendar_type=CalendarType.facility_shared,
                color="#059669",
                description="施設全体で共有するカレンダー",
                is_primary=False,
                is_archived=False,
            )
            session.add(shared_calendar)
            session.flush()
            facility_calendars.append(shared_calendar)

        for index, calendar in enumerate(facility_calendars, start=1):
            calendar.calendar_type = CalendarType.facility_shared
            calendar.is_primary = False
            if not calendar.description:
                calendar.description = "施設全体で共有するカレンダー"
            calendar.updated_at = utc_now()
            session.add(calendar)
            owner_user = next((item for item in active_users if item.id == calendar.owner_user_id), lead_user)
            if calendar.owner_user_id != owner_user.id:
                calendar.owner_user_id = owner_user.id
            ensure_member(calendar, owner_user, CalendarMemberRole.owner)
            for user in active_users:
                ensure_member(calendar, user, shared_role_for_user(calendar, user))
                ensure_preference(calendar, user, display_order=20 + index * 10)

        session.commit()
