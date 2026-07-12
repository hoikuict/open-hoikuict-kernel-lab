import os
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal, Optional, Protocol
from urllib.parse import quote, unquote
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import HTTPConnection

from csrf import rotate_csrf_token
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
MOCK_CHILD_RECORDS_PERMISSION_COOKIE = "mock_can_manage_child_records"


def _auth_cookie_kwargs() -> dict[str, object]:
    return {
        "httponly": True,
        "secure": os.getenv("HOIKUICT_COOKIE_SECURE") == "1",
        "samesite": "lax",
        "path": "/",
    }


def mock_auth_enabled() -> bool:
    return os.getenv("HOIKUICT_ENABLE_MOCK_AUTH") == "1"


@dataclass(slots=True)
class StaffUser:
    role: Role
    name: str = "モック職員"
    user_id: Optional[UUID] = None
    can_manage_child_records: bool = False

    def __post_init__(self) -> None:
        if self.role == Role.ADMIN:
            self.can_manage_child_records = True

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


@dataclass(frozen=True, slots=True)
class StaffSessionSubject:
    user_id: UUID
    display_name: str
    role: Role
    can_manage_child_records: bool = False


class StaffAuthBackend(Protocol):
    mode: Literal["mock", "external"]

    def resolve_principal(self, connection: HTTPConnection) -> StaffUser | None: ...

    def establish_session(self, response: Response, subject: StaffSessionSubject) -> None: ...

    def clear_session(self, response: Response) -> None: ...


class ParentPortalAuthBackend(Protocol):
    mode: Literal["mock", "external"]

    def get_parent_account_id(self, request: Request) -> Optional[int]: ...

    def set_parent_session(self, response: Response, parent_account_id: int) -> None: ...

    def clear_parent_session(self, response: Response) -> None: ...


class MockStaffAuthBackend:
    mode: Literal["mock"] = "mock"

    def resolve_principal(self, connection: HTTPConnection) -> StaffUser | None:
        if not mock_auth_enabled():
            return None
        raw_user_id = connection.cookies.get(MOCK_CALENDAR_USER_COOKIE)
        try:
            user_id = UUID(str(raw_user_id)) if raw_user_id else None
        except (TypeError, ValueError):
            return None
        if user_id is None:
            return None

        valid_roles = {item.value for item in Role}
        raw_role = connection.cookies.get(MOCK_ROLE_COOKIE)
        if raw_role not in valid_roles:
            return None
        role = Role(raw_role)
        if os.getenv("HOIKUICT_ENABLE_MOCK_ROLE_OVERRIDE") == "1":
            as_param = connection.query_params.get("as")
            if as_param in valid_roles:
                role = Role(as_param)

        raw_name = connection.cookies.get(MOCK_STAFF_NAME_COOKIE)
        name = unquote(raw_name) if raw_name else "モック職員"
        can_manage_child_records = (
            connection.cookies.get(MOCK_CHILD_RECORDS_PERMISSION_COOKIE) == "1"
        )
        return StaffUser(
            role=role,
            name=name,
            user_id=user_id,
            can_manage_child_records=can_manage_child_records,
        )

    def establish_session(self, response: Response, subject: StaffSessionSubject) -> None:
        kwargs = _auth_cookie_kwargs()
        response.set_cookie(MOCK_ROLE_COOKIE, subject.role.value, max_age=60 * 60 * 24, **kwargs)
        response.set_cookie(
            MOCK_STAFF_NAME_COOKIE,
            quote(subject.display_name, safe=""),
            max_age=60 * 60 * 24,
            **kwargs,
        )
        response.set_cookie(
            MOCK_CHILD_RECORDS_PERMISSION_COOKIE,
            "1" if subject.can_manage_child_records or subject.role == Role.ADMIN else "0",
            max_age=60 * 60 * 24,
            **kwargs,
        )
        response.set_cookie(
            MOCK_CALENDAR_USER_COOKIE,
            str(subject.user_id),
            max_age=60 * 60 * 24,
            **kwargs,
        )
        rotate_csrf_token(response)

    def clear_session(self, response: Response) -> None:
        for cookie_name in (
            MOCK_ROLE_COOKIE,
            MOCK_STAFF_NAME_COOKIE,
            MOCK_CHILD_RECORDS_PERMISSION_COOKIE,
            MOCK_CALENDAR_USER_COOKIE,
        ):
            response.delete_cookie(cookie_name, path="/")
        rotate_csrf_token(response)


class MockParentPortalAuthBackend:
    mode: Literal["mock"] = "mock"

    def get_parent_account_id(self, request: Request) -> Optional[int]:
        if not mock_auth_enabled():
            return None
        raw_id = request.cookies.get(MOCK_PARENT_ACCOUNT_COOKIE)
        if not raw_id:
            return None
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            return None

    def set_parent_session(self, response: Response, parent_account_id: int) -> None:
        response.set_cookie(
            MOCK_PARENT_ACCOUNT_COOKIE,
            str(parent_account_id),
            max_age=60 * 60 * 24,
            **_auth_cookie_kwargs(),
        )
        rotate_csrf_token(response)

    def clear_parent_session(self, response: Response) -> None:
        response.delete_cookie(MOCK_PARENT_ACCOUNT_COOKIE, path="/")
        rotate_csrf_token(response)


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


def staff_auth_is_mock() -> bool:
    return getattr(_staff_auth_backend, "mode", None) == "mock"


def parent_auth_is_mock() -> bool:
    return getattr(_parent_portal_auth_backend, "mode", None) == "mock"


def require_mock_staff_auth() -> None:
    if not mock_auth_enabled() or not staff_auth_is_mock():
        raise HTTPException(status_code=404, detail="Not Found")


def require_mock_parent_auth() -> None:
    if not mock_auth_enabled() or not parent_auth_is_mock():
        raise HTTPException(status_code=404, detail="Not Found")


async def staff_auth_http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
):
    accepts_html = "text/html" in request.headers.get("accept", "").lower()
    if (
        exc.status_code == 401
        and request.method.upper() == "GET"
        and accepts_html
        and mock_auth_enabled()
        and staff_auth_is_mock()
    ):
        return RedirectResponse(url="/staff/login", status_code=303)
    return await http_exception_handler(request, exc)


def resolve_staff_principal(connection: HTTPConnection) -> StaffUser | None:
    return _staff_auth_backend.resolve_principal(connection)


def get_optional_current_staff_user(request: Request) -> StaffUser | None:
    return resolve_staff_principal(request)


def get_current_staff_user(request: Request) -> StaffUser:
    principal = resolve_staff_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="職員ログインが必要です")
    return principal


def get_current_parent_account_id(request: Request) -> Optional[int]:
    return _parent_portal_auth_backend.get_parent_account_id(request)


def set_parent_account_cookie(response: Response, parent_account_id: int) -> None:
    _parent_portal_auth_backend.set_parent_session(response, parent_account_id)


def clear_parent_account_cookie(response: Response) -> None:
    _parent_portal_auth_backend.clear_parent_session(response)


def get_current_staff_user_id(request: Request) -> Optional[UUID]:
    principal = resolve_staff_principal(request)
    return principal.user_id if principal else None


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
    response.set_cookie(
        MOCK_CALENDAR_USER_COOKIE,
        user_id,
        max_age=60 * 60 * 24,
        **_auth_cookie_kwargs(),
    )


def clear_calendar_user_cookie(response: Response) -> None:
    response.delete_cookie(MOCK_CALENDAR_USER_COOKIE, path="/")


def set_staff_cookies(
    response: Response,
    *,
    role: Role,
    name: str,
    user_id: str,
    can_manage_child_records: bool = False,
) -> None:
    _staff_auth_backend.establish_session(
        response,
        StaffSessionSubject(
            user_id=UUID(str(user_id)),
            display_name=name,
            role=role,
            can_manage_child_records=can_manage_child_records,
        ),
    )


def clear_staff_cookies(response: Response) -> None:
    _staff_auth_backend.clear_session(response)


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


def require_child_record_manager(user: CurrentUser) -> None:
    if not user.can_manage_child_records:
        raise HTTPException(status_code=403, detail="園児台帳管理権限が必要です")


def require_attendance_check_editor(user: CurrentUser) -> None:
    if not user.can_manage_attendance_checks:
        raise HTTPException(status_code=403, detail="出欠確認を更新できる権限がありません")
