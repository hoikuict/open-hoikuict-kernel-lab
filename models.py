from datetime import date, datetime
from enum import Enum
import uuid
from typing import Any, List, Optional

from sqlalchemy import JSON, CheckConstraint, UniqueConstraint
from sqlmodel import Column, Field, Relationship, SQLModel

from time_utils import local_today, utc_now


class ChildStatus(str, Enum):
    enrolled = "enrolled"
    graduated = "graduated"
    withdrawn = "withdrawn"

    @property
    def label(self) -> str:
        return {
            self.enrolled: "在園",
            self.graduated: "卒園",
            self.withdrawn: "退園",
        }[self]


class ParentAccountStatus(str, Enum):
    active = "active"
    inactive = "inactive"

    @property
    def label(self) -> str:
        return {
            self.active: "有効",
            self.inactive: "停止中",
        }[self]


class DailyContactEntryStatus(str, Enum):
    submitted = "submitted"

    @property
    def label(self) -> str:
        return {self.submitted: "提出済み"}[self]


class DailyContactReplyStatus(str, Enum):
    draft = "draft"
    published = "published"

    @property
    def label(self) -> str:
        return {
            self.draft: "下書き",
            self.published: "公開済み",
        }[self]


class ParentContactType(str, Enum):
    present = "present"
    absent_private = "absent_private"
    absent_sick = "absent_sick"

    @property
    def label(self) -> str:
        return {
            self.present: "出席",
            self.absent_private: "欠席(私用)",
            self.absent_sick: "欠席(病欠)",
        }[self]

    @property
    def short_label(self) -> str:
        return {
            self.present: "出席",
            self.absent_private: "私用",
            self.absent_sick: "病欠",
        }[self]


class AttendanceVerificationStatus(str, Enum):
    present = "present"
    private_absent = "private_absent"
    sick_absent = "sick_absent"
    unknown = "unknown"

    @property
    def label(self) -> str:
        return {
            self.present: "出席",
            self.private_absent: "私用休み",
            self.sick_absent: "病気休み",
            self.unknown: "不明",
        }[self]

    @property
    def is_present(self) -> bool:
        return self == self.present

    @property
    def is_absent(self) -> bool:
        return self in {self.private_absent, self.sick_absent}

    @property
    def is_unknown(self) -> bool:
        return self == self.unknown


class ExtendedCareChargeStatus(str, Enum):
    draft = "draft"
    confirmed = "confirmed"
    manual_adjusted = "manual_adjusted"
    excluded = "excluded"

    @property
    def label(self) -> str:
        return {
            self.draft: "要確認",
            self.confirmed: "確認済み",
            self.manual_adjusted: "調整済み",
            self.excluded: "対象外",
        }[self]


class ChildProfileChangeRequestStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"

    @property
    def label(self) -> str:
        return {
            self.pending: "承認待ち",
            self.approved: "承認済み",
            self.rejected: "差し戻し",
        }[self]


class NoticeStatus(str, Enum):
    draft = "draft"
    published = "published"

    @property
    def label(self) -> str:
        return {
            self.draft: "下書き",
            self.published: "公開中",
        }[self]


class NoticePriority(str, Enum):
    normal = "normal"
    high = "high"

    @property
    def label(self) -> str:
        return {
            self.normal: "通常",
            self.high: "重要",
        }[self]


class NoticeTargetType(str, Enum):
    all = "all"
    classroom = "classroom"
    child = "child"

    @property
    def label(self) -> str:
        return {
            self.all: "全保護者",
            self.classroom: "クラス",
            self.child: "園児",
        }[self]


class SurveyStatus(str, Enum):
    draft = "draft"
    published = "published"
    closed = "closed"

    @property
    def label(self) -> str:
        return {
            self.draft: "下書き",
            self.published: "公開中",
            self.closed: "締切済み",
        }[self]


class SurveyAudienceType(str, Enum):
    parent = "parent"
    staff = "staff"

    @property
    def label(self) -> str:
        return {
            self.parent: "保護者向け",
            self.staff: "職員向け",
        }[self]


class SurveyTargetType(str, Enum):
    all = "all"
    classroom = "classroom"
    child = "child"
    all_staff = "all_staff"
    staff_role = "staff_role"
    staff_user = "staff_user"

    @property
    def label(self) -> str:
        return {
            self.all: "全保護者",
            self.classroom: "クラス",
            self.child: "園児",
            self.all_staff: "全職員",
            self.staff_role: "職員ロール",
            self.staff_user: "職員",
        }[self]


class SurveyAnswerUnit(str, Enum):
    family = "family"
    child = "child"
    staff_user = "staff_user"

    @property
    def label(self) -> str:
        return {
            self.family: "世帯単位",
            self.child: "子ども単位",
            self.staff_user: "職員単位",
        }[self]


class QuestionType(str, Enum):
    text_short = "text_short"
    text_long = "text_long"
    single_choice = "single_choice"
    multiple_choice = "multiple_choice"
    scale = "scale"
    yes_no = "yes_no"
    date = "date"

    @property
    def label(self) -> str:
        return {
            self.text_short: "短文テキスト",
            self.text_long: "長文テキスト",
            self.single_choice: "単一選択",
            self.multiple_choice: "複数選択",
            self.scale: "評価スケール",
            self.yes_no: "はい/いいえ",
            self.date: "日付",
        }[self]


CHILD_FIELDS = [
    {"key": "family_name", "label": "家族", "default": True},
    {"key": "classroom", "label": "クラス", "default": True},
    {"key": "last_name", "label": "姓", "default": True},
    {"key": "first_name", "label": "名", "default": True},
    {"key": "last_name_kana", "label": "姓（カナ）", "default": True},
    {"key": "first_name_kana", "label": "名（カナ）", "default": True},
    {"key": "birth_date", "label": "生年月日", "default": True},
    {"key": "age", "label": "年齢", "default": True},
    {"key": "enrollment_date", "label": "入園日", "default": False},
    {"key": "withdrawal_date", "label": "退園日", "default": False},
    {"key": "status", "label": "在籍状況", "default": True},
    {"key": "home_address", "label": "自宅住所", "default": False},
    {"key": "home_phone", "label": "自宅電話番号", "default": False},
    {"key": "guardians", "label": "保護者", "default": True},
    {"key": "siblings", "label": "兄弟姉妹", "default": False},
    {"key": "allergy", "label": "アレルギー", "default": False},
    {"key": "medical_notes", "label": "医療メモ", "default": False},
]


class Family(SQLModel, table=True):
    __tablename__ = "families"

    id: Optional[int] = Field(default=None, primary_key=True)
    family_name: str = Field(index=True)
    home_address: Optional[str] = None
    home_phone: Optional[str] = None
    shared_profile: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    children: List["Child"] = Relationship(back_populates="family")
    parent_accounts: List["ParentAccount"] = Relationship(back_populates="family")

    def guardian_profiles(self) -> list[dict[str, Any]]:
        profile = self.shared_profile if isinstance(self.shared_profile, dict) else {}
        guardians = profile.get("guardians", [])
        if not isinstance(guardians, list):
            return []
        return sorted(
            [item for item in guardians if isinstance(item, dict)],
            key=lambda item: int(item.get("order", 99)),
        )

    @property
    def display_code(self) -> str:
        return f"F-{self.id:05d}" if self.id is not None else "F-未保存"

    @property
    def identity_label(self) -> str:
        return f"{self.family_name}（{self.display_code}）"

    @property
    def selection_label(self) -> str:
        return self.identity_label


class Classroom(SQLModel, table=True):
    __tablename__ = "classrooms"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    display_order: int = Field(default=1, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    children: List["Child"] = Relationship(back_populates="classroom")
    messages: List["Message"] = Relationship(back_populates="room")


class Child(SQLModel, table=True):
    __tablename__ = "children"

    id: Optional[int] = Field(default=None, primary_key=True)
    last_name: str
    first_name: str
    last_name_kana: str
    first_name_kana: str
    birth_date: date
    enrollment_date: date
    withdrawal_date: Optional[date] = None
    status: ChildStatus = Field(default=ChildStatus.enrolled)
    classroom_id: Optional[int] = Field(default=None, foreign_key="classrooms.id")
    family_id: Optional[int] = Field(default=None, foreign_key="families.id", index=True)
    home_address: Optional[str] = None
    home_phone: Optional[str] = None
    older_sibling_id: Optional[int] = Field(default=None, foreign_key="children.id")
    extra_data: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    older_sibling: Optional["Child"] = Relationship(
        back_populates="younger_siblings",
        sa_relationship_kwargs={"foreign_keys": "[Child.older_sibling_id]", "remote_side": "[Child.id]"},
    )
    younger_siblings: List["Child"] = Relationship(
        back_populates="older_sibling",
        sa_relationship_kwargs={"foreign_keys": "[Child.older_sibling_id]"},
    )
    classroom: Optional[Classroom] = Relationship(back_populates="children")
    family: Optional[Family] = Relationship(back_populates="children")
    guardians: List["Guardian"] = Relationship(back_populates="child")
    attendance_records: List["AttendanceRecord"] = Relationship(back_populates="child")
    parent_links: List["ParentChildLink"] = Relationship(back_populates="child")
    daily_contact_entries: List["DailyContactEntry"] = Relationship(back_populates="child")
    daily_contact_replies: List["DailyContactReply"] = Relationship(back_populates="child")
    profile_change_requests: List["ChildProfileChangeRequest"] = Relationship(back_populates="child")

    @property
    def full_name(self) -> str:
        return f"{self.last_name} {self.first_name}"

    @property
    def full_name_kana(self) -> str:
        return f"{self.last_name_kana} {self.first_name_kana}"

    @property
    def age(self) -> int:
        today = local_today()
        return today.year - self.birth_date.year - (
            (today.month, today.day) < (self.birth_date.month, self.birth_date.day)
        )

    @property
    def family_display_name(self) -> str:
        return self.family.identity_label if self.family else ""

    @property
    def shared_home_address(self) -> str:
        if self.family and self.family.home_address:
            return self.family.home_address
        return self.home_address or ""

    @property
    def shared_home_phone(self) -> str:
        if self.family and self.family.home_phone:
            return self.family.home_phone
        return self.home_phone or ""

    def _guardian_labels(self) -> list[str]:
        family_labels: list[str] = []
        if self.family:
            for guardian_profile in self.family.guardian_profiles():
                last_name = str(guardian_profile.get("last_name", "")).strip()
                first_name = str(guardian_profile.get("first_name", "")).strip()
                if not last_name and not first_name:
                    continue
                label = f"{last_name} {first_name}".strip()
                relationship = str(guardian_profile.get("relationship", "")).strip()
                if relationship:
                    label = f"{label}（{relationship}）"
                family_labels.append(label)
        if family_labels:
            return family_labels

        guardian_labels: list[str] = []
        for guardian in sorted(self.guardians, key=lambda item: item.order):
            label = guardian.full_name
            if guardian.relationship:
                label = f"{label}（{guardian.relationship}）"
            guardian_labels.append(label)
        if guardian_labels:
            return guardian_labels

        account_labels: list[str] = []
        for account in sorted(self.parent_links, key=lambda item: (not item.is_primary_contact, item.id or 0)):
            if not account.parent_account:
                continue
            label = account.parent_account.display_name
            if account.relationship_label:
                label = f"{label}（{account.relationship_label}）"
            account_labels.append(label)
        if account_labels:
            return account_labels

        if self.family:
            return [
                account.display_name.strip()
                for account in sorted(self.family.parent_accounts, key=lambda item: item.id or 0)
                if account.display_name.strip()
            ]

        return []

    def get_field(self, key: str) -> str:
        if key == "family_name":
            return self.family_display_name
        if key == "classroom":
            return self.classroom.name if self.classroom else ""
        if key == "age":
            return f"{self.age}歳"
        if key == "status":
            return self.status.label
        if key == "home_address":
            return self.shared_home_address
        if key == "home_phone":
            return self.shared_home_phone
        if key == "allergy":
            if isinstance(self.extra_data, dict):
                return ", ".join(self.extra_data.get("allergy", []))
            return ""
        if key == "medical_notes":
            if isinstance(self.extra_data, dict):
                return str(self.extra_data.get("medical_notes", "") or "")
            return ""
        if key == "guardians":
            return " / ".join(self._guardian_labels())
        if key == "siblings":
            if self.older_sibling:
                return f"兄姉: {self.older_sibling.full_name}"
            younger_names = [sibling.full_name for sibling in self.younger_siblings]
            return " / ".join(younger_names)
        value = getattr(self, key, None)
        return str(value) if value is not None else ""


class AllergenCategory(str, Enum):
    food_mandatory = "food_mandatory"
    food_advisory = "food_advisory"
    other_food = "other_food"
    latex = "latex"
    animal_dander = "animal_dander"
    pollen = "pollen"
    dust_mite = "dust_mite"
    contact = "contact"
    insect = "insect"
    other = "other"

    @property
    def label(self) -> str:
        return {
            self.food_mandatory: "特定原材料",
            self.food_advisory: "準特定原材料",
            self.other_food: "その他食物",
            self.latex: "ラテックス",
            self.animal_dander: "動物",
            self.pollen: "花粉",
            self.dust_mite: "ダニ",
            self.contact: "接触",
            self.insect: "昆虫",
            self.other: "その他",
        }[self]


class AllergySeverity(str, Enum):
    mild = "mild"
    moderate = "moderate"
    severe = "severe"

    @property
    def label(self) -> str:
        return {
            self.mild: "軽度",
            self.moderate: "中等度",
            self.severe: "重度",
        }[self]


class HealthCheckType(str, Enum):
    entrance = "entrance"
    periodic = "periodic"
    daily = "daily"
    post_illness = "post_illness"

    @property
    def label(self) -> str:
        return {
            self.entrance: "入所時",
            self.periodic: "定期健診",
            self.daily: "日常観察",
            self.post_illness: "病後登園",
        }[self]


class ChildHealthProfile(SQLModel, table=True):
    __tablename__ = "child_health_profiles"

    id: Optional[int] = Field(default=None, primary_key=True)
    child_id: int = Field(foreign_key="children.id", unique=True, index=True)
    blood_type: Optional[str] = None
    primary_doctor_name: Optional[str] = None
    primary_doctor_phone: Optional[str] = None
    primary_doctor_address: Optional[str] = None
    hospital_name: Optional[str] = None
    hospital_phone: Optional[str] = None
    requires_medical_care: bool = Field(default=False)
    medical_care_details: Optional[str] = None
    epipen_required: bool = Field(default=False)
    epipen_storage_location: Optional[str] = None
    medical_history: Optional[str] = None
    disability_info: Optional[str] = None
    current_medications: Optional[str] = None
    sids_risk_flag: bool = Field(default=False)
    sids_notes: Optional[str] = None
    breastfed: Optional[bool] = None
    formula_type: Optional[str] = None
    food_texture_level: Optional[str] = None
    religious_dietary: Optional[str] = None
    other_dietary_restrictions: Optional[str] = None
    developmental_notes: Optional[str] = None
    psychological_notes: Optional[str] = None
    family_health_notes: Optional[str] = None
    other_notes: Optional[str] = None
    extra_data: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChildAllergy(SQLModel, table=True):
    __tablename__ = "child_allergies"

    id: Optional[int] = Field(default=None, primary_key=True)
    child_id: int = Field(foreign_key="children.id", index=True)
    allergen_category: AllergenCategory = Field(default=AllergenCategory.other_food)
    allergen_name: str
    severity: AllergySeverity = Field(default=AllergySeverity.mild)
    symptoms: Optional[str] = None
    diagnosis_confirmed: bool = Field(default=False)
    diagnosis_date: Optional[date] = None
    treating_doctor: Optional[str] = None
    removal_required: bool = Field(default=True)
    substitute_food: Optional[str] = None
    action_plan: Optional[str] = None
    source_document: Optional[str] = None
    source_document_date: Optional[date] = None
    valid_until: Optional[date] = None
    is_active: bool = Field(default=True, index=True)
    notes: Optional[str] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HealthCheckRecord(SQLModel, table=True):
    __tablename__ = "health_check_records"

    id: Optional[int] = Field(default=None, primary_key=True)
    child_id: int = Field(foreign_key="children.id", index=True)
    check_type: HealthCheckType = Field(index=True)
    checked_at: date = Field(index=True)
    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    head_circumference_cm: Optional[float] = None
    chest_circumference_cm: Optional[float] = None
    temperature: Optional[float] = None
    heart_rate: Optional[int] = None
    respiratory_rate: Optional[int] = None
    vision_right: Optional[str] = None
    vision_left: Optional[str] = None
    hearing_result: Optional[str] = None
    dental_result: Optional[str] = None
    overall_result: Optional[str] = None
    doctor_name: Optional[str] = None
    requires_followup: bool = Field(default=False)
    followup_notes: Optional[str] = None
    general_condition: Optional[str] = None
    observer_name: Optional[str] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Guardian(SQLModel, table=True):
    __tablename__ = "guardians"

    id: Optional[int] = Field(default=None, primary_key=True)
    child_id: int = Field(foreign_key="children.id")
    last_name: str
    first_name: str
    last_name_kana: Optional[str] = None
    first_name_kana: Optional[str] = None
    relationship: str = "母"
    phone: Optional[str] = None
    workplace: Optional[str] = None
    workplace_address: Optional[str] = None
    workplace_phone: Optional[str] = None
    order: int = 1

    child: Optional[Child] = Relationship(back_populates="guardians")

    @property
    def full_name(self) -> str:
        return f"{self.last_name} {self.first_name}"


class AttendanceRecord(SQLModel, table=True):
    __tablename__ = "attendance_records"
    __table_args__ = (UniqueConstraint("child_id", "attendance_date", name="uq_attendance_child_date"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    child_id: int = Field(foreign_key="children.id", index=True)
    attendance_date: date = Field(index=True)
    check_in_at: Optional[datetime] = None
    check_out_at: Optional[datetime] = None
    planned_pickup_time: Optional[str] = None
    pickup_person: Optional[str] = None
    snack_required: bool = Field(default=False)
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    child: Optional[Child] = Relationship(back_populates="attendance_records")
    extended_care_charge: Optional["ExtendedCareCharge"] = Relationship(
        back_populates="attendance_record",
        sa_relationship_kwargs={"uselist": False},
    )


class ExtendedCareFeeRule(SQLModel, table=True):
    __tablename__ = "extended_care_fee_rules"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    effective_from: date = Field(index=True)
    effective_to: Optional[date] = Field(default=None, index=True)
    start_time: str = Field(default="18:00")
    grace_minutes: int = Field(default=5)
    rounding_minutes: int = Field(default=15)
    unit_price: int = Field(default=100)
    daily_cap_amount: Optional[int] = None
    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    charges: List["ExtendedCareCharge"] = Relationship(back_populates="rule")


class ExtendedCareCharge(SQLModel, table=True):
    __tablename__ = "extended_care_charges"
    __table_args__ = (
        UniqueConstraint("attendance_record_id", name="uq_extended_care_charge_attendance_record"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    attendance_record_id: int = Field(foreign_key="attendance_records.id", index=True)
    child_id: int = Field(foreign_key="children.id", index=True)
    target_date: date = Field(index=True)
    rule_id: int = Field(foreign_key="extended_care_fee_rules.id", index=True)
    charge_start_at: datetime
    actual_check_out_at: Optional[datetime] = None
    extended_minutes: int = Field(default=0)
    billable_units: int = Field(default=0)
    auto_amount: int = Field(default=0)
    adjustment_amount: int = Field(default=0)
    final_amount: int = Field(default=0)
    status: ExtendedCareChargeStatus = Field(default=ExtendedCareChargeStatus.draft, index=True)
    adjustment_reason: Optional[str] = None
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    attendance_record: Optional[AttendanceRecord] = Relationship(back_populates="extended_care_charge")
    child: Optional[Child] = Relationship()
    rule: Optional[ExtendedCareFeeRule] = Relationship(back_populates="charges")


class ParentAccount(SQLModel, table=True):
    __tablename__ = "parent_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    display_name: str
    email: str = Field(index=True, unique=True)
    phone: Optional[str] = None
    home_address: Optional[str] = None
    workplace: Optional[str] = None
    workplace_address: Optional[str] = None
    workplace_phone: Optional[str] = None
    family_id: Optional[int] = Field(default=None, foreign_key="families.id", index=True)
    status: ParentAccountStatus = Field(default=ParentAccountStatus.active)
    password_hash: Optional[str] = None
    invited_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    family: Optional[Family] = Relationship(back_populates="parent_accounts")
    child_links: List["ParentChildLink"] = Relationship(back_populates="parent_account")
    daily_contact_entries: List["DailyContactEntry"] = Relationship(back_populates="parent_account")
    notice_reads: List["NoticeRead"] = Relationship(back_populates="parent_account")
    profile_change_notifications: List["ProfileChangeNotification"] = Relationship(back_populates="parent_account")
    child_profile_change_requests: List["ChildProfileChangeRequest"] = Relationship(back_populates="parent_account")

    @property
    def family_display_name(self) -> str:
        return self.family.family_name if self.family else ""


class ParentChildLink(SQLModel, table=True):
    __tablename__ = "parent_child_links"
    __table_args__ = (UniqueConstraint("parent_account_id", "child_id", name="uq_parent_child_link"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    parent_account_id: int = Field(foreign_key="parent_accounts.id", index=True)
    child_id: int = Field(foreign_key="children.id", index=True)
    relationship_label: str = Field(default="保護者")
    is_primary_contact: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utc_now)

    parent_account: Optional[ParentAccount] = Relationship(back_populates="child_links")
    child: Optional[Child] = Relationship(back_populates="parent_links")


class DailyContactEntry(SQLModel, table=True):
    __tablename__ = "daily_contact_entries"
    __table_args__ = (UniqueConstraint("child_id", "target_date", name="uq_daily_contact_child_date"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    child_id: int = Field(foreign_key="children.id", index=True)
    parent_account_id: int = Field(foreign_key="parent_accounts.id", index=True)
    target_date: date = Field(index=True)
    temperature: Optional[str] = None
    sleep_notes: Optional[str] = None
    breakfast_status: Optional[str] = None
    bowel_movement_status: Optional[str] = None
    mood: Optional[str] = None
    cough: Optional[str] = None
    runny_nose: Optional[str] = None
    medication: Optional[str] = None
    condition_note: Optional[str] = None
    contact_note: Optional[str] = None
    contact_type: ParentContactType = Field(default=ParentContactType.present)
    absence_temperature: Optional[str] = None
    absence_symptoms: Optional[str] = None
    absence_diagnosis: Optional[str] = None
    absence_note: Optional[str] = None
    status: DailyContactEntryStatus = Field(default=DailyContactEntryStatus.submitted)
    extra_data: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    submitted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    child: Optional[Child] = Relationship(back_populates="daily_contact_entries")
    parent_account: Optional[ParentAccount] = Relationship(back_populates="daily_contact_entries")


    @property
    def is_present_contact(self) -> bool:
        return self.contact_type == ParentContactType.present

    @property
    def is_absent_contact(self) -> bool:
        return self.contact_type in {ParentContactType.absent_private, ParentContactType.absent_sick}

    @property
    def absence_reason_label(self) -> str:
        if self.contact_type == ParentContactType.absent_private:
            return "私用"
        if self.contact_type == ParentContactType.absent_sick:
            return "病欠"
        return ""


class DailyContactReply(SQLModel, table=True):
    __tablename__ = "daily_contact_replies"
    __table_args__ = (UniqueConstraint("child_id", "target_date", name="uq_daily_contact_reply_child_date"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    child_id: int = Field(foreign_key="children.id", index=True)
    daily_contact_entry_id: Optional[int] = Field(default=None, foreign_key="daily_contact_entries.id", index=True)
    target_date: date = Field(index=True)
    status: DailyContactReplyStatus = Field(default=DailyContactReplyStatus.draft, index=True)
    field_values: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    message: Optional[str] = None
    staff_user_id: Optional[uuid.UUID] = Field(default=None, foreign_key="users.id", index=True)
    staff_name: Optional[str] = None
    published_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    child: Optional[Child] = Relationship(back_populates="daily_contact_replies")


class AttendanceVerification(SQLModel, table=True):
    __tablename__ = "attendance_verifications"
    __table_args__ = (UniqueConstraint("child_id", "target_date", name="uq_attendance_verification_child_date"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    child_id: int = Field(foreign_key="children.id", index=True)
    target_date: date = Field(index=True)
    status: AttendanceVerificationStatus = Field(default=AttendanceVerificationStatus.unknown)
    updated_by_name: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AttendanceVerificationHistory(SQLModel, table=True):
    __tablename__ = "attendance_verification_histories"

    id: Optional[int] = Field(default=None, primary_key=True)
    child_id: int = Field(foreign_key="children.id", index=True)
    target_date: date = Field(index=True)
    status: AttendanceVerificationStatus = Field(default=AttendanceVerificationStatus.unknown)
    updated_by_name: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class AttendanceAlarmState(SQLModel, table=True):
    __tablename__ = "attendance_alarm_states"
    __table_args__ = (UniqueConstraint("child_id", "target_date", name="uq_attendance_alarm_child_date"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    child_id: int = Field(foreign_key="children.id", index=True)
    target_date: date = Field(index=True)
    is_active: bool = Field(default=False, index=True)
    reasons: Optional[list[str]] = Field(default=None, sa_column=Column(JSON))
    evaluated_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AttendanceAlarmHistory(SQLModel, table=True):
    __tablename__ = "attendance_alarm_histories"

    id: Optional[int] = Field(default=None, primary_key=True)
    child_id: int = Field(foreign_key="children.id", index=True)
    target_date: date = Field(index=True)
    is_active: bool = Field(default=False, index=True)
    reasons: Optional[list[str]] = Field(default=None, sa_column=Column(JSON))
    evaluated_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)


class BillingPaymentMethod(str, Enum):
    direct_debit = "口座振替"
    cash = "cash"
    bank_transfer = "bank_transfer"
    exempt = "exempt"


class DirectDebitStatus(str, Enum):
    not_set = "not_set"
    paper_received = "paper_received"
    active = "active"
    suspended = "suspended"


class BillingCycleStatus(str, Enum):
    draft = "draft"
    generated = "generated"
    confirmed = "confirmed"
    exported = "exported"
    result_imported = "result_imported"
    closed = "closed"


class BillingClaimStatus(str, Enum):
    draft = "draft"
    confirmed = "confirmed"
    exported = "exported"
    paid = "paid"
    failed = "failed"
    exempted = "exempted"
    canceled = "canceled"


class BillingChargeSourceType(str, Enum):
    extension_auto = "extension_auto"
    meal_auto = "meal_auto"
    manual = "manual"
    adjustment = "adjustment"
    carryover = "carryover"


class MealFeeCalculationType(str, Enum):
    monthly_fixed = "monthly_fixed"
    attendance_count = "attendance_count"
    manual = "manual"


class MealFeeCountSource(str, Enum):
    attendance_check_in = "attendance_check_in"
    daily_contact_present = "daily_contact_present"
    attendance_verification_present = "attendance_verification_present"
    verification_then_check_in = "verification_then_check_in"
    manual = "manual"


class MealFeeProrationPolicy(str, Enum):
    none = "none"
    daily_by_enrolled_days = "daily_by_enrolled_days"
    manual_adjustment = "manual_adjustment"


class ProrationRounding(str, Enum):
    round = "round"
    floor = "floor"
    ceil = "ceil"


class ZenginExportStatus(str, Enum):
    created = "created"
    submitted = "submitted"
    result_imported = "result_imported"
    superseded = "superseded"
    canceled = "canceled"


class BillingSetting(SQLModel, table=True):
    __tablename__ = "billing_settings"

    id: Optional[int] = Field(default=None, primary_key=True)
    facility_name: str
    collector_code: str
    collector_name_kana: str
    customer_number_facility_code: str = Field(default="000", max_length=3)
    withdrawal_bank_code: str
    withdrawal_bank_name_kana: Optional[str] = None
    withdrawal_branch_code: str
    withdrawal_branch_name_kana: Optional[str] = None
    collector_account_type: str = Field(default="1", max_length=1)
    collector_account_type_allowed_values: list[str] = Field(
        default_factory=lambda: ["1", "2"],
        sa_column=Column(JSON),
    )
    collector_account_number: str
    code_type: str = Field(default="0", max_length=1)
    file_encoding: str = Field(default="cp932")
    line_separator: str = Field(default="CRLF")
    content_hash_algorithm: str = Field(default="sha256")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class FamilyBillingProfile(SQLModel, table=True):
    __tablename__ = "family_billing_profiles"
    __table_args__ = (
        UniqueConstraint("family_id", name="uq_family_billing_profile_family"),
        UniqueConstraint("customer_number", name="uq_family_billing_profile_customer_number"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    family_id: int = Field(foreign_key="families.id", index=True)
    payment_method: BillingPaymentMethod = Field(default=BillingPaymentMethod.direct_debit)
    direct_debit_status: DirectDebitStatus = Field(default=DirectDebitStatus.not_set)
    bank_code: Optional[str] = None
    bank_name_kana: Optional[str] = None
    branch_code: Optional[str] = None
    branch_name_kana: Optional[str] = None
    account_type: Optional[str] = None
    account_number: Optional[str] = None
    account_holder_kana: Optional[str] = None
    customer_number: str = Field(index=True)
    new_code: str = Field(default="0", max_length=1)
    new_code_consumed_by_export_id: Optional[int] = Field(
        default=None,
        foreign_key="zengin_exports.id",
        index=True,
    )
    mandate_received_on: Optional[date] = None
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class FeeItem(SQLModel, table=True):
    __tablename__ = "fee_items"
    __table_args__ = (UniqueConstraint("code", name="uq_fee_item_code"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True)
    name: str
    category: str
    charge_unit: str
    default_amount: Optional[int] = None
    taxable_type: str = Field(default="non_taxable")
    is_active: bool = Field(default=True, index=True)
    display_order: int = Field(default=100, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ExtensionFeeRule(SQLModel, table=True):
    __tablename__ = "extension_fee_rules"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    valid_from: date
    valid_to: Optional[date] = None
    base_end_time: str = Field(default="18:00")
    grace_minutes: int = Field(default=0)
    unit_minutes: int = Field(default=30)
    amount_per_unit: int
    rounding_mode: str = Field(default="ceil")
    max_daily_amount: Optional[int] = None
    max_monthly_amount: Optional[int] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MealFeeRule(SQLModel, table=True):
    __tablename__ = "meal_fee_rules"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    calculation_type: MealFeeCalculationType = Field(default=MealFeeCalculationType.monthly_fixed)
    monthly_amount: Optional[int] = None
    unit_amount: Optional[int] = None
    count_source: Optional[MealFeeCountSource] = Field(default=MealFeeCountSource.verification_then_check_in)
    proration_policy: MealFeeProrationPolicy = Field(default=MealFeeProrationPolicy.none)
    proration_rounding: ProrationRounding = Field(default=ProrationRounding.round)
    valid_from: date
    valid_to: Optional[date] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BillingCycle(SQLModel, table=True):
    __tablename__ = "billing_cycles"
    __table_args__ = (UniqueConstraint("year_month", name="uq_billing_cycle_year_month"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    year_month: str = Field(index=True)
    period_start: date
    period_end: date
    withdrawal_date: date
    due_date: Optional[date] = None
    status: BillingCycleStatus = Field(default=BillingCycleStatus.draft, index=True)
    generated_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    confirmed_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BillingClaim(SQLModel, table=True):
    __tablename__ = "billing_claims"
    __table_args__ = (UniqueConstraint("billing_cycle_id", "family_id", name="uq_billing_claim_cycle_family"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    billing_cycle_id: int = Field(foreign_key="billing_cycles.id", index=True)
    family_id: int = Field(foreign_key="families.id", index=True)
    payment_method: BillingPaymentMethod
    total_amount: int = Field(default=0)
    status: BillingClaimStatus = Field(default=BillingClaimStatus.draft, index=True)
    zengin_export_id: Optional[int] = Field(default=None, foreign_key="zengin_exports.id", index=True)
    exported_at: Optional[datetime] = None
    result_code: Optional[str] = None
    paid_at: Optional[datetime] = None
    failed_reason: Optional[str] = None
    carried_over_to_claim_id: Optional[int] = Field(default=None, foreign_key="billing_claims.id")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BillingChargeLine(SQLModel, table=True):
    __tablename__ = "billing_charge_lines"

    id: Optional[int] = Field(default=None, primary_key=True)
    billing_claim_id: int = Field(foreign_key="billing_claims.id", index=True)
    fee_item_id: int = Field(foreign_key="fee_items.id", index=True)
    child_id: Optional[int] = Field(default=None, foreign_key="children.id", index=True)
    source_type: BillingChargeSourceType
    source_date: Optional[date] = None
    source_claim_id: Optional[int] = Field(default=None, foreign_key="billing_claims.id")
    description: str
    quantity: int = Field(default=1)
    unit_label: Optional[str] = None
    unit_price: int = Field(default=0)
    amount: int = Field(default=0)
    is_locked: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ZenginExport(SQLModel, table=True):
    __tablename__ = "zengin_exports"

    id: Optional[int] = Field(default=None, primary_key=True)
    billing_cycle_id: int = Field(foreign_key="billing_cycles.id", index=True)
    withdrawal_date: date
    file_name: str
    total_count: int = Field(default=0)
    total_amount: int = Field(default=0)
    status: ZenginExportStatus = Field(default=ZenginExportStatus.created, index=True)
    superseded_by_export_id: Optional[int] = Field(default=None, foreign_key="zengin_exports.id")
    reissue_reason: Optional[str] = None
    canceled_reason: Optional[str] = None
    submitted_at: Optional[datetime] = None
    result_imported_at: Optional[datetime] = None
    content_hash: str
    settings_snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_by: str
    created_at: datetime = Field(default_factory=utc_now)


class ZenginExportLine(SQLModel, table=True):
    __tablename__ = "zengin_export_lines"
    __table_args__ = (UniqueConstraint("zengin_export_id", "billing_claim_id", name="uq_zengin_line_export_claim"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    zengin_export_id: int = Field(foreign_key="zengin_exports.id", index=True)
    billing_claim_id: int = Field(foreign_key="billing_claims.id", index=True)
    family_id: int = Field(foreign_key="families.id", index=True)
    customer_number: str = Field(index=True)
    amount: int
    bank_snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result_code: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class Notice(SQLModel, table=True):
    __tablename__ = "notices"

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    body: str
    priority: NoticePriority = Field(default=NoticePriority.normal)
    status: NoticeStatus = Field(default=NoticeStatus.draft)
    publish_start_at: Optional[datetime] = None
    publish_end_at: Optional[datetime] = None
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    targets: List["NoticeTarget"] = Relationship(back_populates="notice")
    reads: List["NoticeRead"] = Relationship(back_populates="notice")


class MeetingNote(SQLModel, table=True):
    __tablename__ = "meeting_notes"

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(default="無題の議事録")
    content: Optional[bytes] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class NoticeTarget(SQLModel, table=True):
    __tablename__ = "notice_targets"

    id: Optional[int] = Field(default=None, primary_key=True)
    notice_id: int = Field(foreign_key="notices.id", index=True)
    target_type: NoticeTargetType = Field(default=NoticeTargetType.all)
    target_value: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)

    notice: Optional[Notice] = Relationship(back_populates="targets")


class NoticeRead(SQLModel, table=True):
    __tablename__ = "notice_reads"
    __table_args__ = (UniqueConstraint("notice_id", "parent_account_id", name="uq_notice_parent_read"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    notice_id: int = Field(foreign_key="notices.id", index=True)
    parent_account_id: int = Field(foreign_key="parent_accounts.id", index=True)
    read_at: datetime = Field(default_factory=utc_now)

    notice: Optional[Notice] = Relationship(back_populates="reads")
    parent_account: Optional[ParentAccount] = Relationship(back_populates="notice_reads")


class Survey(SQLModel, table=True):
    __tablename__ = "surveys"

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    description: Optional[str] = None
    status: SurveyStatus = Field(default=SurveyStatus.draft, index=True)
    audience_type: SurveyAudienceType = Field(default=SurveyAudienceType.parent, index=True)
    answer_unit: SurveyAnswerUnit = Field(default=SurveyAnswerUnit.family)
    opens_at: Optional[datetime] = None
    closes_at: Optional[datetime] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    targets: List["SurveyTarget"] = Relationship(back_populates="survey")
    questions: List["SurveyQuestion"] = Relationship(back_populates="survey")
    answers: List["SurveyAnswer"] = Relationship(back_populates="survey")


class SurveyTarget(SQLModel, table=True):
    __tablename__ = "survey_targets"
    __table_args__ = (
        UniqueConstraint("survey_id", "target_type", "target_value", name="uq_survey_target"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    survey_id: int = Field(foreign_key="surveys.id", index=True)
    target_type: SurveyTargetType = Field(default=SurveyTargetType.all)
    target_value: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)

    survey: Optional[Survey] = Relationship(back_populates="targets")


class SurveyQuestion(SQLModel, table=True):
    __tablename__ = "survey_questions"

    id: Optional[int] = Field(default=None, primary_key=True)
    survey_id: int = Field(foreign_key="surveys.id", index=True)
    order: int = Field(default=1, index=True)
    question_type: QuestionType = Field(default=QuestionType.text_short)
    label: str
    description: Optional[str] = None
    is_required: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    survey: Optional[Survey] = Relationship(back_populates="questions")
    options: List["SurveyQuestionOption"] = Relationship(back_populates="question")
    responses: List["SurveyResponse"] = Relationship(back_populates="question")


class SurveyQuestionOption(SQLModel, table=True):
    __tablename__ = "survey_question_options"
    __table_args__ = (
        UniqueConstraint("question_id", "option_key", name="uq_question_option_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    question_id: int = Field(foreign_key="survey_questions.id", index=True)
    order: int = Field(default=1)
    option_key: str
    label: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    question: Optional[SurveyQuestion] = Relationship(back_populates="options")


class SurveyAnswer(SQLModel, table=True):
    __tablename__ = "survey_answers"
    __table_args__ = (
        UniqueConstraint("survey_id", "family_id", name="uq_survey_family_answer"),
        UniqueConstraint("survey_id", "child_id", name="uq_survey_child_answer"),
        UniqueConstraint("survey_id", "staff_user_id", name="uq_survey_staff_user_answer"),
        CheckConstraint(
            "(family_id IS NOT NULL AND child_id IS NULL AND staff_user_id IS NULL) OR "
            "(family_id IS NULL AND child_id IS NOT NULL AND staff_user_id IS NULL) OR "
            "(family_id IS NULL AND child_id IS NULL AND staff_user_id IS NOT NULL)",
            name="ck_survey_answer_scope",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    survey_id: int = Field(foreign_key="surveys.id", index=True)
    family_id: Optional[int] = Field(default=None, foreign_key="families.id", index=True)
    child_id: Optional[int] = Field(default=None, foreign_key="children.id", index=True)
    staff_user_id: Optional[uuid.UUID] = Field(default=None, foreign_key="users.id", index=True)
    created_by_parent_account_id: Optional[int] = Field(default=None, foreign_key="parent_accounts.id", index=True)
    created_by_staff_user_id: Optional[uuid.UUID] = Field(default=None, foreign_key="users.id", index=True)
    submitted_by_parent_account_id: Optional[int] = Field(default=None, foreign_key="parent_accounts.id", index=True)
    submitted_by_staff_user_id: Optional[uuid.UUID] = Field(default=None, foreign_key="users.id", index=True)
    submitted_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    survey: Optional[Survey] = Relationship(back_populates="answers")
    responses: List["SurveyResponse"] = Relationship(back_populates="answer")


class SurveyResponse(SQLModel, table=True):
    __tablename__ = "survey_responses"
    __table_args__ = (
        UniqueConstraint("answer_id", "question_id", name="uq_answer_question"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    answer_id: int = Field(foreign_key="survey_answers.id", index=True)
    question_id: int = Field(foreign_key="survey_questions.id", index=True)
    value_text: Optional[str] = None
    value_option_ids: Optional[list[int]] = Field(default=None, sa_column=Column(JSON))
    value_scale: Optional[int] = None
    value_bool: Optional[bool] = None
    value_date: Optional[date] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    answer: Optional[SurveyAnswer] = Relationship(back_populates="responses")
    question: Optional[SurveyQuestion] = Relationship(back_populates="responses")


class ProfileChangeNotification(SQLModel, table=True):
    __tablename__ = "profile_change_notifications"

    id: Optional[int] = Field(default=None, primary_key=True)
    parent_account_id: int = Field(foreign_key="parent_accounts.id", index=True)
    change_summary: str
    change_details: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    is_read: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utc_now)
    read_at: Optional[datetime] = None

    parent_account: Optional[ParentAccount] = Relationship(back_populates="profile_change_notifications")


class ChildProfileChangeRequest(SQLModel, table=True):
    __tablename__ = "child_profile_change_requests"

    id: Optional[int] = Field(default=None, primary_key=True)
    child_id: int = Field(foreign_key="children.id", index=True)
    parent_account_id: int = Field(foreign_key="parent_accounts.id", index=True)
    status: ChildProfileChangeRequestStatus = Field(
        default=ChildProfileChangeRequestStatus.pending,
        index=True,
    )
    change_summary: str
    request_data: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    change_details: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    submitted_at: datetime = Field(default_factory=utc_now)
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    review_note: Optional[str] = None
    updated_at: datetime = Field(default_factory=utc_now)

    child: Optional[Child] = Relationship(back_populates="profile_change_requests")
    parent_account: Optional[ParentAccount] = Relationship(back_populates="child_profile_change_requests")


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: Optional[int] = Field(default=None, primary_key=True)
    room_id: int = Field(foreign_key="classrooms.id", index=True)
    parent_message_id: Optional[int] = Field(default=None, foreign_key="messages.id", index=True)
    author_name: str
    body: str = Field(default="")
    created_at: datetime = Field(default_factory=utc_now, index=True)
    updated_at: datetime = Field(default_factory=utc_now)
    deleted_at: Optional[datetime] = Field(default=None, index=True)
    deleted_by: Optional[str] = None

    room: Optional[Classroom] = Relationship(back_populates="messages")
    attachments: List["MessageAttachment"] = Relationship(back_populates="message")

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def display_body(self) -> str:
        if self.is_deleted:
            return "このメッセージは削除されました。"
        return self.body


class MessageAttachment(SQLModel, table=True):
    __tablename__ = "message_attachments"

    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="messages.id", index=True)
    original_filename: str
    storage_path: str
    content_type: Optional[str] = None
    file_size: int = Field(default=0)
    is_image: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utc_now)

    message: Optional[Message] = Relationship(back_populates="attachments")


class CalendarMemberRole(str, Enum):
    owner = "owner"
    editor = "editor"
    viewer = "viewer"

    @property
    def label(self) -> str:
        return {
            self.owner: "管理者",
            self.editor: "編集可",
            self.viewer: "閲覧のみ",
        }[self]


class CalendarType(str, Enum):
    staff_personal = "staff_personal"
    facility_shared = "facility_shared"

    @property
    def label(self) -> str:
        return {
            self.staff_personal: "個人カレンダー",
            self.facility_shared: "施設共用カレンダー",
        }[self]


class RecurrenceFrequency(str, Enum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"
    yearly = "yearly"

    @property
    def label(self) -> str:
        return {
            self.daily: "毎日",
            self.weekly: "毎週",
            self.monthly: "毎月",
            self.yearly: "毎年",
        }[self]


class EventKind(str, Enum):
    single = "single"
    series_master = "series_master"


class EventVisibility(str, Enum):
    normal = "normal"
    private = "private"

    @property
    def label(self) -> str:
        return {
            self.normal: "通常",
            self.private: "非公開",
        }[self]


class EventLifecycleStatus(str, Enum):
    confirmed = "confirmed"
    cancelled = "cancelled"


class ReminderMethod(str, Enum):
    in_app = "in_app"


class NotificationJobStatus(str, Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    cancelled = "cancelled"


class CalendarActivityKind(str, Enum):
    calendar_created = "calendar_created"
    calendar_updated = "calendar_updated"
    share_synced = "share_synced"
    event_created = "event_created"
    event_updated = "event_updated"
    event_deleted = "event_deleted"
    event_moved = "event_moved"
    event_resized = "event_resized"


USER_SOURCE_SYSTEM = "system"
USER_SOURCE_LOCAL_SAMPLE = "local_sample"
USER_SOURCE_WEB_DEMO = "web_demo"
USER_SOURCE_MANUAL = "manual"
USER_SOURCE_IMPORT = "import"
USER_SOURCE_EXTERNAL = "external"

USER_PROVISIONING_SOURCE_LABELS = {
    USER_SOURCE_SYSTEM: "システム",
    USER_SOURCE_LOCAL_SAMPLE: "ローカルサンプル",
    USER_SOURCE_WEB_DEMO: "WEB公開デモ",
    USER_SOURCE_MANUAL: "手動追加",
    USER_SOURCE_IMPORT: "インポート",
    USER_SOURCE_EXTERNAL: "外部連携",
}


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: str = Field(index=True, unique=True, max_length=255)
    display_name: str = Field(max_length=100)
    timezone: str = Field(default="Asia/Tokyo", max_length=64)
    locale: str = Field(default="ja-JP", max_length=16)
    default_calendar_id: Optional[uuid.UUID] = Field(default=None, foreign_key="calendars.id")
    staff_role: str = Field(default="can_edit", max_length=32)
    staff_sort_order: int = Field(default=100, index=True)
    is_calendar_admin: bool = Field(default=False)
    can_manage_child_records: bool = Field(default=False)
    provisioning_source: str = Field(default=USER_SOURCE_MANUAL, max_length=32, index=True)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def calendar_role_label(self) -> str:
        return {
            "admin": "管理者",
            "can_edit": "編集可",
            "view_only": "閲覧のみ",
        }.get(self.staff_role, "編集可")

    @property
    def can_edit_calendar(self) -> bool:
        return self.staff_role in {"admin", "can_edit"}

    @property
    def is_view_only_staff(self) -> bool:
        return self.staff_role == "view_only"

    @property
    def can_manage_child_records_effective(self) -> bool:
        return self.staff_role == "admin" or self.can_manage_child_records

    @property
    def provisioning_source_label(self) -> str:
        return USER_PROVISIONING_SOURCE_LABELS.get(
            self.provisioning_source,
            self.provisioning_source or "未分類",
        )


class Calendar(SQLModel, table=True):
    __tablename__ = "calendars"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=100)
    calendar_type: CalendarType = Field(default=CalendarType.staff_personal)
    color: str = Field(default="#2563EB", max_length=16)
    description: Optional[str] = Field(default=None)
    is_primary: bool = Field(default=False)
    is_archived: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CalendarMember(SQLModel, table=True):
    __tablename__ = "calendar_members"
    __table_args__ = (UniqueConstraint("calendar_id", "user_id", name="uq_calendar_member"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    calendar_id: uuid.UUID = Field(foreign_key="calendars.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    role: CalendarMemberRole = Field(default=CalendarMemberRole.viewer)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CalendarUserPreference(SQLModel, table=True):
    __tablename__ = "calendar_user_preferences"
    __table_args__ = (UniqueConstraint("calendar_id", "user_id", name="uq_calendar_user_preference"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    calendar_id: uuid.UUID = Field(foreign_key="calendars.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    is_visible: bool = Field(default=True)
    display_order: int = Field(default=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CalendarActivityLog(SQLModel, table=True):
    __tablename__ = "calendar_activity_logs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    calendar_id: uuid.UUID = Field(foreign_key="calendars.id", index=True)
    actor_user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    actor_name: str = Field(max_length=100)
    action: CalendarActivityKind = Field(index=True)
    summary: str = Field(max_length=255)
    created_at: datetime = Field(default_factory=utc_now, index=True)


class RecurrenceRule(SQLModel, table=True):
    __tablename__ = "recurrence_rules"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    freq: RecurrenceFrequency = Field(default=RecurrenceFrequency.weekly)
    interval: int = Field(default=1)
    by_weekday: Optional[str] = Field(default=None, max_length=32)
    by_month_day: Optional[str] = Field(default=None, max_length=32)
    count: Optional[int] = Field(default=None)
    until_at: Optional[datetime] = Field(default=None)
    timezone: str = Field(default="Asia/Tokyo", max_length=64)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Event(SQLModel, table=True):
    __tablename__ = "events"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    calendar_id: uuid.UUID = Field(foreign_key="calendars.id", index=True)
    created_by_user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    kind: EventKind = Field(default=EventKind.single)
    title: str = Field(max_length=200)
    description: Optional[str] = Field(default=None)
    location: Optional[str] = Field(default=None, max_length=255)
    start_at: datetime = Field(index=True)
    end_at: datetime = Field(index=True)
    timezone: str = Field(default="Asia/Tokyo", max_length=64)
    is_all_day: bool = Field(default=False)
    visibility: EventVisibility = Field(default=EventVisibility.normal)
    status: EventLifecycleStatus = Field(default=EventLifecycleStatus.confirmed)
    recurrence_rule_id: Optional[uuid.UUID] = Field(default=None, foreign_key="recurrence_rules.id")
    split_from_event_id: Optional[uuid.UUID] = Field(default=None, foreign_key="events.id")
    split_from_original_start_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class EventOverride(SQLModel, table=True):
    __tablename__ = "event_overrides"
    __table_args__ = (UniqueConstraint("series_event_id", "original_start_at", name="uq_event_override"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    series_event_id: uuid.UUID = Field(foreign_key="events.id", index=True)
    original_start_at: datetime = Field(index=True)
    title: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None)
    location: Optional[str] = Field(default=None, max_length=255)
    start_at: Optional[datetime] = Field(default=None)
    end_at: Optional[datetime] = Field(default=None)
    timezone: Optional[str] = Field(default=None, max_length=64)
    is_all_day: Optional[bool] = Field(default=None)
    visibility: Optional[EventVisibility] = Field(default=None)
    is_cancelled: bool = Field(default=False)
    created_by_user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Reminder(SQLModel, table=True):
    __tablename__ = "reminders"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "user_id",
            "method",
            "minutes_before",
            name="uq_event_user_reminder",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    event_id: uuid.UUID = Field(foreign_key="events.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    method: ReminderMethod = Field(default=ReminderMethod.in_app)
    minutes_before: int = Field(default=10)
    is_deleted: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class NotificationJob(SQLModel, table=True):
    __tablename__ = "notification_jobs"
    __table_args__ = (UniqueConstraint("reminder_id", "original_start_at", name="uq_notification_job"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    reminder_id: uuid.UUID = Field(foreign_key="reminders.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    source_event_id: uuid.UUID = Field(foreign_key="events.id", index=True)
    original_start_at: datetime = Field(index=True)
    occurrence_start_at: datetime = Field(index=True)
    scheduled_at: datetime = Field(index=True)
    sent_at: Optional[datetime] = Field(default=None)
    status: NotificationJobStatus = Field(default=NotificationJobStatus.pending)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DataTransferLog(SQLModel, table=True):
    __tablename__ = "data_transfer_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    transfer_type: str = Field(index=True)
    dataset: str = Field(index=True)
    filename: Optional[str] = None
    actor_name: Optional[str] = None
    result: str = Field(index=True)
    created_count: int = Field(default=0)
    updated_count: int = Field(default=0)
    skipped_count: int = Field(default=0)
    error_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=utc_now, index=True)
