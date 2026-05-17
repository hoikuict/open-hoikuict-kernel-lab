from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from auth import Role, clear_staff_cookies, get_current_staff_user, set_staff_cookies
from database import get_session
from models import User
from staff_user_service import list_active_staff_users


router = APIRouter(prefix="/staff", tags=["staff-auth"])
templates = Jinja2Templates(directory="templates")

DEFAULT_STAFF_REDIRECT = "/children"
DEFAULT_LOGOUT_REDIRECT = "/staff/login"


def _normalize_redirect(redirect_to: str | None, fallback: str) -> str:
    if redirect_to and redirect_to.startswith("/") and not redirect_to.startswith("//"):
        return redirect_to
    return fallback


def _role_from_user(user: User) -> Role:
    if user.staff_role == "admin":
        return Role.ADMIN
    if user.staff_role == "view_only":
        return Role.VIEW_ONLY
    return Role.CAN_EDIT


@router.get("/login", response_class=HTMLResponse)
def staff_login_page(
    request: Request,
    redirect: str = DEFAULT_STAFF_REDIRECT,
    current_user=Depends(get_current_staff_user),
    session: Session = Depends(get_session),
):
    users = list_active_staff_users(session)
    return templates.TemplateResponse(
        request,
        "staff_auth/login.html",
        {
            "request": request,
            "current_user": current_user,
            "redirect_to": _normalize_redirect(redirect, DEFAULT_STAFF_REDIRECT),
            "users": users,
        },
    )


@router.post("/login")
def staff_login(
    user_id: str = Form(""),
    redirect_to: str = Form(DEFAULT_STAFF_REDIRECT),
    session: Session = Depends(get_session),
):
    target = _normalize_redirect(redirect_to, DEFAULT_STAFF_REDIRECT)
    try:
        user_uuid = UUID(str(user_id).strip())
    except (TypeError, ValueError):
        user_uuid = UUID(int=0)
    user = session.get(User, user_uuid)
    if user is None or not user.is_active:
        return RedirectResponse(url="/staff/login", status_code=303)

    response = RedirectResponse(url=target, status_code=303)
    set_staff_cookies(response, role=_role_from_user(user), name=user.display_name, user_id=str(user.id))
    return response


@router.post("/logout")
def staff_logout(redirect_to: str = Form(DEFAULT_LOGOUT_REDIRECT)):
    target = _normalize_redirect(redirect_to, DEFAULT_LOGOUT_REDIRECT)
    response = RedirectResponse(url=target, status_code=303)
    clear_staff_cookies(response)
    return response
