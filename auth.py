from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Optional, Protocol
from urllib.parse import quote, unquote
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response

from staff_user_service import STAFF_USER_SORT_ORDER_LIMIT


class Role(str, Enum):
    VIEW_ONLY = "view_only"
    CAN_EDIT = "can_edit"
    ADMIN = "admin"


ROLE_LABELS = {
    Role.VIEW_ONLY: "閲覧のみ",
    Role.CAN_EDIT: "編集可",
    Role.ADMIN: "管理者",
}

MOCK_ROLE_COOKIE = "mock_role"
MOCK_PARENT_ACCOUNT_COOKIE = "mock_parent_account_id"
MOCK_CALENDAR_USER_COOKIE = "mock_calendar_user_id"
MOCK_STAFF_NAME_COOKIE = "mock_staff_name"


@dataclass(slots=True)
class StaffUser:
    role: Role
    name: str = "モック職員"
    user_id: Optional[UUID] = None

    @property
    def staff_id(self) -> Optional[str]:
        return str(self.user_id) if self.user_id is not None else None

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
    def can_manage_attendance_checks(self) -> bool:
        return self.can_edit


class StaffAuthBackend(Protocol):
    def get_current_user(self, request: Request) -> StaffUser: ...


class ParentPortalAuthBackend(Protocol):
    def get_parent_account_id(self, request: Request) -> Optional[int]: ...

    def set_parent_session(self, response: Response, parent_account_id: int) -> None: ...

    def clear_parent_session(self, response: Response) -> None: ...


class MockStaffAuthBackend:
    def get_current_user(self, request: Request) -> StaffUser:
        role = Role.CAN_EDIT
        user_id = get_current_staff_user_id(request)
        raw_name = request.cookies.get(MOCK_STAFF_NAME_COOKIE)
        name = unquote(raw_name) if raw_name else "モック職員"
        as_param = request.query_params.get("as")
        valid_roles = {item.value for item in Role}
        if as_param and as_param in valid_roles:
            role = Role(as_param)
        else:
            cookie_role = request.cookies.get(MOCK_ROLE_COOKIE)
            if cookie_role and cookie_role in valid_roles:
                role = Role(cookie_role)
        return StaffUser(role=role, name=name, user_id=user_id)


class MockParentPortalAuthBackend:
    def get_parent_account_id(self, request: Request) -> Optional[int]:
        raw_id = request.cookies.get(MOCK_PARENT_ACCOUNT_COOKIE)
        if not raw_id:
            return None
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            return None

    def set_parent_session(self, response: Response, parent_account_id: int) -> None:
        response.set_cookie(MOCK_PARENT_ACCOUNT_COOKIE, str(parent_account_id), max_age=60 * 60 * 24)

    def clear_parent_session(self, response: Response) -> None:
        response.delete_cookie(MOCK_PARENT_ACCOUNT_COOKIE)


_staff_auth_backend: StaffAuthBackend = MockStaffAuthBackend()
_parent_portal_auth_backend: ParentPortalAuthBackend = MockParentPortalAuthBackend()


def configure_staff_auth_backend(backend: StaffAuthBackend) -> None:
    global _staff_auth_backend
    _staff_auth_backend = backend


def configure_parent_portal_auth_backend(backend: ParentPortalAuthBackend) -> None:
    global _parent_portal_auth_backend
    _parent_portal_auth_backend = backend


def reset_auth_backends() -> None:
    configure_staff_auth_backend(MockStaffAuthBackend())
    configure_parent_portal_auth_backend(MockParentPortalAuthBackend())


def get_current_staff_user(request: Request) -> StaffUser:
    return _staff_auth_backend.get_current_user(request)


def get_current_parent_account_id(request: Request) -> Optional[int]:
    return _parent_portal_auth_backend.get_parent_account_id(request)


def set_parent_account_cookie(response: Response, parent_account_id: int) -> None:
    _parent_portal_auth_backend.set_parent_session(response, parent_account_id)


def clear_parent_account_cookie(response: Response) -> None:
    _parent_portal_auth_backend.clear_parent_session(response)


def get_calendar_user_cookie(request: Request) -> Optional[str]:
    return request.cookies.get(MOCK_CALENDAR_USER_COOKIE)


def get_current_staff_user_id(request: Request) -> Optional[UUID]:
    raw_user_id = get_calendar_user_cookie(request)
    if not raw_user_id:
        return None
    try:
        return UUID(str(raw_user_id))
    except (TypeError, ValueError):
        return None


def get_current_staff_user_record(request: Request, session):
    from models import User

    staff_user_id = get_current_staff_user_id(request)
    if staff_user_id is None:
        return None
    user = session.get(User, staff_user_id)
    if user is None or not user.is_active or user.staff_sort_order >= STAFF_USER_SORT_ORDER_LIMIT:
        return None
    return user


def set_calendar_user_cookie(response: Response, user_id: str) -> None:
    response.set_cookie(MOCK_CALENDAR_USER_COOKIE, user_id, max_age=60 * 60 * 24)


def clear_calendar_user_cookie(response: Response) -> None:
    response.delete_cookie(MOCK_CALENDAR_USER_COOKIE)


def set_staff_cookies(response: Response, *, role: Role, name: str, user_id: str) -> None:
    response.set_cookie(MOCK_ROLE_COOKIE, role.value, max_age=60 * 60 * 24)
    response.set_cookie(MOCK_STAFF_NAME_COOKIE, quote(name, safe=""), max_age=60 * 60 * 24)
    set_calendar_user_cookie(response, user_id)


def clear_staff_cookies(response: Response) -> None:
    response.delete_cookie(MOCK_ROLE_COOKIE)
    response.delete_cookie(MOCK_STAFF_NAME_COOKIE)
    clear_calendar_user_cookie(response)


MockUser = StaffUser
get_mock_current_user = get_current_staff_user
get_mock_parent_account_id = get_current_parent_account_id
set_mock_parent_cookie = set_parent_account_cookie
clear_mock_parent_cookie = clear_parent_account_cookie

CurrentUser = Annotated[StaffUser, Depends(get_current_staff_user)]


def require_can_edit(user: CurrentUser) -> None:
    if not user.can_edit:
        raise HTTPException(status_code=403, detail="編集権限がありません")


def require_admin(user: CurrentUser) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="管理者権限が必要です")


def require_attendance_check_editor(user: CurrentUser) -> None:
    if not user.can_manage_attendance_checks:
        raise HTTPException(status_code=403, detail="出欠確認を更新できる権限がありません")
