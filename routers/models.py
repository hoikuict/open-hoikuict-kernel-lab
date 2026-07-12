from datetime import date, datetime
from enum import Enum
from typing import Any, Optional
from sqlmodel import Field, SQLModel, Column, Relationship
from sqlalchemy import JSON

from time_utils import local_today, utc_now


class ChildStatus(str, Enum):
    enrolled = "enrolled"
    graduated = "graduated"
    withdrawn = "withdrawn"

    @property
    def label(self):
        return {"enrolled": "在園", "graduated": "卒園", "withdrawn": "退園"}[self.value]


# フィールド設定（どのカラムを表示するか）
CHILD_FIELDS = [
    {"key": "last_name",        "label": "姓",        "default": True},
    {"key": "first_name",       "label": "名",        "default": True},
    {"key": "last_name_kana",   "label": "姓（カナ）", "default": True},
    {"key": "first_name_kana",  "label": "名（カナ）", "default": True},
    {"key": "birth_date",       "label": "生年月日",   "default": True},
    {"key": "age",              "label": "年齢",       "default": True},
    {"key": "enrollment_date",  "label": "入園日",     "default": False},
    {"key": "withdrawal_date",  "label": "退園日",     "default": False},
    {"key": "status",           "label": "在籍",       "default": True},
    {"key": "home_address",     "label": "自宅住所",   "default": False},
    {"key": "home_phone",       "label": "自宅電話",   "default": False},
    {"key": "guardians",        "label": "保護者",     "default": True},
    {"key": "siblings",         "label": "兄弟",       "default": False},
    {"key": "allergy",          "label": "アレルギー", "default": False},
    {"key": "medical_notes",    "label": "医療的配慮", "default": False},
]


class Child(SQLModel, table=True):
    __tablename__ = "children"

    id: Optional[int] = Field(default=None, primary_key=True)

    # コア情報
    last_name: str
    first_name: str
    last_name_kana: str
    first_name_kana: str
    birth_date: date
    enrollment_date: date
    withdrawal_date: Optional[date] = None
    status: ChildStatus = Field(default=ChildStatus.enrolled)

    # 自宅連絡先
    home_address: Optional[str] = None
    home_phone: Optional[str] = None

    # 兄弟関係（兄姉から引き継ぐ）
    older_sibling_id: Optional[int] = Field(default=None, foreign_key="children.id")
    older_sibling: Optional["Child"] = Relationship(
        back_populates="younger_siblings",
        sa_relationship_kwargs={"foreign_keys": "[Child.older_sibling_id]", "remote_side": "[Child.id]"},
    )
    younger_siblings: list["Child"] = Relationship(
        back_populates="older_sibling",
        sa_relationship_kwargs={"foreign_keys": "[Child.older_sibling_id]"},
    )

    # JSON拡張
    extra_data: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    # 保護者（1人または2人）
    guardians: list["Guardian"] = Relationship(back_populates="child")

    @property
    def full_name(self):
        return f"{self.last_name} {self.first_name}"

    @property
    def full_name_kana(self):
        return f"{self.last_name_kana} {self.first_name_kana}"

    @property
    def age(self):
        today = local_today()
        bd = self.birth_date
        return today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))

    def get_field(self, key: str):
        """フィールドキーから値を返す（extra_dataも含む）"""
        if key == "age":
            return f"{self.age}歳"
        if key == "status":
            return self.status.label
        if key == "allergy":
            return "、".join(self.extra_data.get("allergy", [])) if self.extra_data else ""
        if key == "medical_notes":
            return self.extra_data.get("medical_notes", "") if self.extra_data else ""
        if key == "guardians":
            if not self.guardians:
                return ""
            return " / ".join(g.full_name for g in sorted(self.guardians, key=lambda x: x.order))
        if key == "siblings":
            try:
                if self.older_sibling:
                    return f"兄姉: {self.older_sibling.full_name}"
            except Exception:
                pass
            return ""
        val = getattr(self, key, None)
        return str(val) if val is not None else ""


class Guardian(SQLModel, table=True):
    """保護者テーブル（保育所預け要件：就労時は勤務先情報）"""
    __tablename__ = "guardians"

    id: Optional[int] = Field(default=None, primary_key=True)
    child_id: int = Field(foreign_key="children.id")

    # 保護者情報
    last_name: str
    first_name: str
    last_name_kana: Optional[str] = None
    first_name_kana: Optional[str] = None
    relationship: str = "父"  # 父、母、祖父、祖母、その他
    phone: Optional[str] = None  # 保護者連絡先

    # 就労の場合（保育所に預けている要件）
    workplace: Optional[str] = None          # 勤務先
    workplace_address: Optional[str] = None  # 勤務先住所
    workplace_phone: Optional[str] = None    # 勤務先電話番号

    order: int = 1  # 1=第一保護者, 2=第二保護者

    child: Optional["Child"] = Relationship(back_populates="guardians")

    @property
    def full_name(self) -> str:
        return f"{self.last_name} {self.first_name}"
