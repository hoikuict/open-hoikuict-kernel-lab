from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from auth import (
    Role,
    clear_staff_cookies,
    get_current_staff_user,
    get_optional_current_staff_user,
    require_admin,
    require_mock_staff_auth,
    set_staff_cookies,
)
from database import get_session
from models import (
    USER_SOURCE_EXTERNAL,
    USER_SOURCE_IMPORT,
    USER_SOURCE_LOCAL_SAMPLE,
    USER_SOURCE_MANUAL,
    USER_SOURCE_SYSTEM,
    USER_SOURCE_WEB_DEMO,
    User,
)
from staff_user_service import list_active_staff_users
from time_utils import utc_now
from url_utils import safe_internal_redirect


router = APIRouter(prefix="/staff", tags=["staff-auth"])
mock_login_router = APIRouter(prefix="/staff", tags=["staff-auth-mock"])
templates = Jinja2Templates(directory="templates")

DEFAULT_STAFF_REDIRECT = "/children"
DEFAULT_LOGOUT_REDIRECT = "/staff/login"
STAFF_ROLE_OPTIONS = [
    ("admin", "管理者"),
    ("can_edit", "編集可"),
    ("view_only", "閲覧のみ"),
]
STAFF_SOURCE_FILTER_OPTIONS = [
    ("all", "すべて"),
    (USER_SOURCE_MANUAL, "手動追加"),
    (USER_SOURCE_LOCAL_SAMPLE, "ローカルサンプル"),
    (USER_SOURCE_WEB_DEMO, "WEB公開デモ"),
    (USER_SOURCE_IMPORT, "インポート"),
    (USER_SOURCE_EXTERNAL, "外部連携"),
    (USER_SOURCE_SYSTEM, "システム"),
]
STAFF_SOURCE_FILTER_VALUES = {value for value, _label in STAFF_SOURCE_FILTER_OPTIONS}


def _role_from_user(user: User) -> Role:
    if user.staff_role == "admin":
        return Role.ADMIN
    if user.staff_role == "view_only":
        return Role.VIEW_ONLY
    return Role.CAN_EDIT


def _normalize_staff_role(raw_role: str) -> str:
    allowed_roles = {role for role, _label in STAFF_ROLE_OPTIONS}
    return raw_role if raw_role in allowed_roles else "can_edit"


def _checked(raw_value: str | None) -> bool:
    return raw_value in {"1", "true", "on", "yes"}


def _normalize_source_filter(raw_source: str) -> str:
    return raw_source if raw_source in STAFF_SOURCE_FILTER_VALUES else "all"


def _staff_source_counts(session: Session) -> dict[str, int]:
    counts = {"all": 0}
    for source in session.exec(select(User.provisioning_source)).all():
        key = source or USER_SOURCE_MANUAL
        counts[key] = counts.get(key, 0) + 1
        counts["all"] += 1
    return counts


def _default_staff_source_filter(session: Session) -> str:
    has_web_demo = session.exec(
        select(User.id).where(User.provisioning_source == USER_SOURCE_WEB_DEMO)
    ).first()
    return USER_SOURCE_WEB_DEMO if has_web_demo else "all"


def _staff_form_data(
    user: User | None = None,
    *,
    display_name: str = "",
    email: str = "",
    staff_role: str = "can_edit",
    can_manage_child_records: bool = False,
    staff_sort_order: int = 100,
    is_active: bool = True,
) -> dict[str, object]:
    if user:
        return {
            "display_name": user.display_name,
            "email": user.email,
            "staff_role": user.staff_role,
            "can_manage_child_records": user.can_manage_child_records_effective,
            "staff_sort_order": user.staff_sort_order,
            "is_active": user.is_active,
        }
    return {
        "display_name": display_name,
        "email": email,
        "staff_role": staff_role,
        "can_manage_child_records": can_manage_child_records,
        "staff_sort_order": staff_sort_order,
        "is_active": is_active,
    }


def _active_admin_count(session: Session) -> int:
    admins = session.exec(
        select(User).where(User.is_active.is_(True), User.staff_role == "admin")
    ).all()
    return len(admins)


def _role_change_error(
    *,
    session: Session,
    target_user: User | None,
    current_user,
    next_role: str,
    next_is_active: bool,
) -> str:
    if target_user is None:
        return ""

    changing_self = (
        current_user
        and current_user.user_id is not None
        and target_user.id == current_user.user_id
    )
    removes_admin = target_user.staff_role == "admin" and (
        next_role != "admin" or not next_is_active
    )
    if changing_self and removes_admin:
        return "自分自身の管理者権限はこの画面では外せません。"
    if target_user.is_active and removes_admin and _active_admin_count(session) <= 1:
        return "最後の管理者は無効化または権限変更できません。"
    return ""


def _render_staff_user_form(
    request: Request,
    *,
    current_user,
    action_url: str,
    submit_label: str,
    form_data: dict[str, object],
    form_error: str = "",
):
    return templates.TemplateResponse(
        request,
        "staff_auth/user_form.html",
        {
            "request": request,
            "current_user": current_user,
            "action_url": action_url,
            "submit_label": submit_label,
            "form_data": form_data,
            "form_error": form_error,
            "staff_role_options": STAFF_ROLE_OPTIONS,
        },
    )


@mock_login_router.get(
    "/login",
    response_class=HTMLResponse,
    dependencies=[Depends(require_mock_staff_auth)],
)
def staff_login_page(
    request: Request,
    redirect: str = DEFAULT_STAFF_REDIRECT,
    current_user=Depends(get_optional_current_staff_user),
    session: Session = Depends(get_session),
):
    del redirect
    users = list_active_staff_users(session)
    return templates.TemplateResponse(
        request,
        "staff_auth/login.html",
        {
            "request": request,
            "current_user": current_user,
            "redirect_to": DEFAULT_STAFF_REDIRECT,
            "users": users,
        },
    )


@mock_login_router.post("/login", dependencies=[Depends(require_mock_staff_auth)])
def staff_login(
    user_id: str = Form(""),
    redirect_to: str = Form(DEFAULT_STAFF_REDIRECT),
    session: Session = Depends(get_session),
):
    del redirect_to
    target = DEFAULT_STAFF_REDIRECT
    try:
        user_uuid = UUID(str(user_id).strip())
    except (TypeError, ValueError):
        user_uuid = UUID(int=0)
    user = session.get(User, user_uuid)
    if user is None or not user.is_active:
        return RedirectResponse(url="/staff/login", status_code=303)

    response = RedirectResponse(url=target, status_code=303)
    set_staff_cookies(
        response,
        role=_role_from_user(user),
        name=user.display_name,
        user_id=str(user.id),
        can_manage_child_records=user.can_manage_child_records_effective,
    )
    return response


@router.post("/logout")
def staff_logout(redirect_to: str = Form(DEFAULT_LOGOUT_REDIRECT)):
    target = safe_internal_redirect(redirect_to, DEFAULT_LOGOUT_REDIRECT)
    response = RedirectResponse(url=target, status_code=303)
    clear_staff_cookies(response)
    return response


@router.get("/users", response_class=HTMLResponse)
def staff_user_list(
    request: Request,
    source: str = "",
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_admin(current_user)
    selected_source = _normalize_source_filter(source) if source else _default_staff_source_filter(session)
    statement = select(User)
    if selected_source != "all":
        statement = statement.where(User.provisioning_source == selected_source)
    users = session.exec(
        statement.order_by(User.staff_sort_order, User.display_name, User.email)
    ).all()
    return templates.TemplateResponse(
        request,
        "staff_auth/users.html",
        {
            "request": request,
            "current_user": current_user,
            "users": users,
            "source_filter": selected_source,
            "source_filter_options": STAFF_SOURCE_FILTER_OPTIONS,
            "source_counts": _staff_source_counts(session),
        },
    )


@router.get("/users/new", response_class=HTMLResponse)
def new_staff_user_form(
    request: Request,
    current_user=Depends(get_current_staff_user),
):
    require_admin(current_user)
    return _render_staff_user_form(
        request,
        current_user=current_user,
        action_url="/staff/users",
        submit_label="職員を追加",
        form_data=_staff_form_data(),
    )


@router.post("/users")
def create_staff_user(
    request: Request,
    display_name: str = Form(...),
    email: str = Form(...),
    staff_role: str = Form("can_edit"),
    can_manage_child_records: str = Form(""),
    staff_sort_order: int = Form(100),
    is_active: str = Form(""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_admin(current_user)
    next_role = _normalize_staff_role(staff_role)
    next_can_manage_child_records = _checked(can_manage_child_records) or next_role == "admin"
    next_is_active = _checked(is_active)
    form_data = _staff_form_data(
        display_name=display_name,
        email=email,
        staff_role=next_role,
        can_manage_child_records=next_can_manage_child_records,
        staff_sort_order=staff_sort_order,
        is_active=next_is_active,
    )
    if session.exec(select(User).where(User.email == email.strip())).first():
        return _render_staff_user_form(
            request,
            current_user=current_user,
            action_url="/staff/users",
            submit_label="職員を追加",
            form_data=form_data,
            form_error="このメールアドレスはすでに登録されています。",
        )

    user = User(
        display_name=display_name.strip(),
        email=email.strip(),
        staff_role=next_role,
        can_manage_child_records=next_can_manage_child_records,
        provisioning_source=USER_SOURCE_MANUAL,
        staff_sort_order=staff_sort_order,
        is_calendar_admin=next_role == "admin",
        is_active=next_is_active,
    )
    session.add(user)
    session.commit()
    return RedirectResponse(url="/staff/users", status_code=303)


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
def edit_staff_user_form(
    request: Request,
    user_id: UUID,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_admin(current_user)
    user = session.get(User, user_id)
    if user is None:
        return RedirectResponse(url="/staff/users", status_code=303)
    return _render_staff_user_form(
        request,
        current_user=current_user,
        action_url=f"/staff/users/{user_id}/edit",
        submit_label="職員を更新",
        form_data=_staff_form_data(user),
    )


@router.post("/users/{user_id}/edit")
def update_staff_user(
    request: Request,
    user_id: UUID,
    display_name: str = Form(...),
    email: str = Form(...),
    staff_role: str = Form("can_edit"),
    can_manage_child_records: str = Form(""),
    staff_sort_order: int = Form(100),
    is_active: str = Form(""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_admin(current_user)
    user = session.get(User, user_id)
    if user is None:
        return RedirectResponse(url="/staff/users", status_code=303)

    next_role = _normalize_staff_role(staff_role)
    next_can_manage_child_records = _checked(can_manage_child_records) or next_role == "admin"
    next_is_active = _checked(is_active)
    form_data = _staff_form_data(
        display_name=display_name,
        email=email,
        staff_role=next_role,
        can_manage_child_records=next_can_manage_child_records,
        staff_sort_order=staff_sort_order,
        is_active=next_is_active,
    )

    duplicate = session.exec(
        select(User).where(User.email == email.strip(), User.id != user_id)
    ).first()
    if duplicate:
        return _render_staff_user_form(
            request,
            current_user=current_user,
            action_url=f"/staff/users/{user_id}/edit",
            submit_label="職員を更新",
            form_data=form_data,
            form_error="このメールアドレスは別の職員で登録されています。",
        )

    role_error = _role_change_error(
        session=session,
        target_user=user,
        current_user=current_user,
        next_role=next_role,
        next_is_active=next_is_active,
    )
    if role_error:
        return _render_staff_user_form(
            request,
            current_user=current_user,
            action_url=f"/staff/users/{user_id}/edit",
            submit_label="職員を更新",
            form_data=form_data,
            form_error=role_error,
        )

    user.display_name = display_name.strip()
    user.email = email.strip()
    user.staff_role = next_role
    user.can_manage_child_records = next_can_manage_child_records
    user.staff_sort_order = staff_sort_order
    user.is_calendar_admin = next_role == "admin"
    user.is_active = next_is_active
    user.updated_at = utc_now()
    session.add(user)
    session.commit()
    return RedirectResponse(url="/staff/users", status_code=303)
