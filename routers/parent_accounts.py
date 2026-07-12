from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from auth import get_current_staff_user, require_child_record_manager
from database import get_session
from family_support import sync_parent_child_links
from models import Family, ParentAccount, ParentAccountStatus, ProfileChangeNotification
from time_utils import utc_now

router = APIRouter(prefix="/parent-accounts", tags=["parent_accounts"])
templates = Jinja2Templates(directory="templates")
def _all_families(session: Session) -> list[Family]:
    return session.exec(
        select(Family)
        .options(selectinload(Family.children), selectinload(Family.parent_accounts))
        .order_by(Family.family_name, Family.id)
    ).all()


def _load_account(session: Session, account_id: int) -> ParentAccount:
    account = session.exec(
        select(ParentAccount)
        .options(
            selectinload(ParentAccount.family).selectinload(Family.children),
            selectinload(ParentAccount.family).selectinload(Family.parent_accounts),
        )
        .where(ParentAccount.id == account_id)
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="保護者アカウントが見つかりません")
    return account


def _sync_related_families(session: Session, family_ids: set[int]) -> None:
    for family_id in sorted(family_ids):
        family = session.exec(
            select(Family)
            .options(selectinload(Family.children), selectinload(Family.parent_accounts))
            .where(Family.id == family_id)
        ).first()
        if family:
            sync_parent_child_links(session, family)


@router.get("/", response_class=HTMLResponse)
def parent_account_list(
    request: Request,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    accounts = session.exec(
        select(ParentAccount)
        .options(selectinload(ParentAccount.family).selectinload(Family.children))
        .order_by(ParentAccount.display_name)
    ).all()
    notifications = session.exec(
        select(ProfileChangeNotification)
        .options(selectinload(ProfileChangeNotification.parent_account))
        .where(ProfileChangeNotification.is_read == False)  # noqa: E712
        .order_by(ProfileChangeNotification.created_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "parent_accounts/list.html",
        {
            "request": request,
            "accounts": accounts,
            "notifications": notifications,
            "current_user": current_user,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_parent_account_form(
    request: Request,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_child_record_manager(current_user)
    return templates.TemplateResponse(
        request,
        "parent_accounts/form.html",
        {
            "request": request,
            "account": None,
            "families": _all_families(session),
            "selected_family_id": "",
            "action_url": "/parent-accounts/",
            "submit_label": "登録する",
            "current_user": current_user,
            "status_options": list(ParentAccountStatus),
        },
    )


@router.post("/")
def create_parent_account(
    display_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    home_address: str = Form(""),
    workplace: str = Form(""),
    workplace_address: str = Form(""),
    workplace_phone: str = Form(""),
    status: str = Form("active"),
    family_id: str = Form(""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_child_record_manager(current_user)
    try:
        normalized_status = ParentAccountStatus(status)
    except ValueError:
        normalized_status = ParentAccountStatus.active

    selected_family_id = int(family_id) if family_id and family_id.isdigit() else None
    account = ParentAccount(
        display_name=display_name.strip(),
        email=email.strip(),
        phone=(phone or "").strip() or None,
        home_address=(home_address or "").strip() or None,
        workplace=(workplace or "").strip() or None,
        workplace_address=(workplace_address or "").strip() or None,
        workplace_phone=(workplace_phone or "").strip() or None,
        family_id=selected_family_id,
        status=normalized_status,
        invited_at=utc_now(),
    )
    session.add(account)
    session.flush()

    if selected_family_id:
        _sync_related_families(session, {selected_family_id})

    session.commit()
    return RedirectResponse(url="/parent-accounts/", status_code=303)


@router.get("/{account_id}/edit", response_class=HTMLResponse)
def edit_parent_account_form(
    request: Request,
    account_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_child_record_manager(current_user)
    account = _load_account(session, account_id)
    return templates.TemplateResponse(
        request,
        "parent_accounts/form.html",
        {
            "request": request,
            "account": account,
            "families": _all_families(session),
            "selected_family_id": account.family_id if account.family_id else "",
            "action_url": f"/parent-accounts/{account_id}/edit",
            "submit_label": "更新する",
            "current_user": current_user,
            "status_options": list(ParentAccountStatus),
        },
    )


@router.post("/{account_id}/edit")
def update_parent_account(
    account_id: int,
    display_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    home_address: str = Form(""),
    workplace: str = Form(""),
    workplace_address: str = Form(""),
    workplace_phone: str = Form(""),
    status: str = Form("active"),
    family_id: str = Form(""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_child_record_manager(current_user)
    account = _load_account(session, account_id)
    old_family_id = account.family_id

    try:
        normalized_status = ParentAccountStatus(status)
    except ValueError:
        normalized_status = ParentAccountStatus.active

    account.display_name = display_name.strip()
    account.email = email.strip()
    account.phone = (phone or "").strip() or None
    account.home_address = (home_address or "").strip() or None
    account.workplace = (workplace or "").strip() or None
    account.workplace_address = (workplace_address or "").strip() or None
    account.workplace_phone = (workplace_phone or "").strip() or None
    account.family_id = int(family_id) if family_id and family_id.isdigit() else None
    account.status = normalized_status
    account.updated_at = utc_now()
    session.add(account)
    session.flush()

    family_ids = {family_id for family_id in [old_family_id, account.family_id] if family_id is not None}
    _sync_related_families(session, family_ids)

    session.commit()
    return RedirectResponse(url="/parent-accounts/", status_code=303)


@router.post("/notifications/{notification_id}/read")
def mark_profile_notification_read(
    notification_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_child_record_manager(current_user)
    notification = session.get(ProfileChangeNotification, notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="通知が見つかりません")

    notification.is_read = True
    notification.read_at = utc_now()
    session.add(notification)
    session.commit()
    return RedirectResponse(url="/parent-accounts/", status_code=303)
