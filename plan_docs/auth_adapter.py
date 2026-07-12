from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from sqlmodel import Session, select

from auth import StaffUser as OpenHoikuictStaffUser
from auth import get_optional_current_staff_user
from auth import Role as OpenHoikuictRole
from database import get_session
from models import Classroom

from .contracts import ROLE_LABELS, Role


DEFAULT_NURSERY_REF = "ひかり保育園"
DEFAULT_CLASSROOM_REFS = ("ひよこ組", "うさぎ組", "きりん組")

assert {item.value for item in Role} == {item.value for item in OpenHoikuictRole}


@dataclass(slots=True)
class StaffUser:
    role: Role
    actor_ref: str | None
    nursery_ref: str
    classroom_refs: tuple[str, ...]
    name: str

    @property
    def can_view(self) -> bool:
        return True

    @property
    def can_edit(self) -> bool:
        return self.role in (Role.CAN_EDIT, Role.ADMIN)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role.value)

    @property
    def classroom_refs_text(self) -> str:
        return ",".join(self.classroom_refs)

    @property
    def nursery_label(self) -> str:
        return self.nursery_ref

    @property
    def classroom_label(self) -> str:
        return self.classroom_refs_text

    @property
    def staff_id(self) -> str | None:
        if self.actor_ref and self.actor_ref.startswith("staff:"):
            return self.actor_ref.removeprefix("staff:")
        return None

    def can_access_classroom(self, classroom_ref: str) -> bool:
        if self.is_admin:
            return True
        if not self.classroom_refs:
            return True
        return classroom_ref in self.classroom_refs


def _nursery_ref() -> str:
    return os.getenv("HOIKU_NURSERY_REF", DEFAULT_NURSERY_REF)


def _classroom_refs(session: Session) -> tuple[str, ...]:
    classrooms = session.exec(select(Classroom).order_by(Classroom.display_order, Classroom.id)).all()
    refs = tuple(classroom.name for classroom in classrooms if classroom.name)
    return refs or DEFAULT_CLASSROOM_REFS


def resolve_plan_docs_staff_user(
    request: Request,
    current_user: Annotated[
        OpenHoikuictStaffUser | None,
        Depends(get_optional_current_staff_user),
    ],
    session: Annotated[Session, Depends(get_session)],
) -> StaffUser:
    if current_user is None:
        return StaffUser(
            role=Role.VIEW_ONLY,
            actor_ref=None,
            nursery_ref=_nursery_ref(),
            classroom_refs=_classroom_refs(session),
            name="未ログイン",
        )
    actor_ref = f"staff:{current_user.user_id}" if current_user.user_id is not None else None
    return StaffUser(
        role=Role(current_user.role.value),
        actor_ref=actor_ref,
        nursery_ref=_nursery_ref(),
        classroom_refs=_classroom_refs(session),
        name=current_user.name,
    )


CurrentUser = Annotated[StaffUser, Depends(resolve_plan_docs_staff_user)]


def _login_url(request: Request | None) -> str:
    if request is None:
        return "/staff/login?redirect=/plans/"
    redirect = request.url.path
    if request.url.query:
        redirect = f"{redirect}?{request.url.query}"
    return f"/staff/login?redirect={quote(redirect, safe='')}"


def _is_htmx(request: Request | None) -> bool:
    return bool(request and request.headers.get("HX-Request", "").lower() == "true")


def _raise_login_required(request: Request | None) -> None:
    login_url = _login_url(request)
    if _is_htmx(request):
        raise HTTPException(
            status_code=401,
            detail="職員を選択してください",
            headers={"HX-Redirect": login_url},
        )
    raise HTTPException(
        status_code=303,
        detail="職員を選択してください",
        headers={"Location": login_url},
    )


def require_actor(user: StaffUser, request: Request | None = None) -> None:
    if not user.actor_ref:
        _raise_login_required(request)


def require_can_edit(user: StaffUser, request: Request | None = None) -> None:
    require_actor(user, request)
    if not user.can_edit:
        raise HTTPException(status_code=403, detail="編集権限がありません")


def require_admin(user: StaffUser, request: Request | None = None) -> None:
    require_actor(user, request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="管理者権限が必要です")


def require_classroom_access(user: StaffUser, classroom_ref: str) -> None:
    if not user.can_access_classroom(classroom_ref):
        raise HTTPException(status_code=403, detail="このクラスの文書にアクセスできません")

