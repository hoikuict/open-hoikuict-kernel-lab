from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from attendance_checks_service import sync_attendance_alarm
from auth import (
    clear_parent_account_cookie,
    get_current_parent_account_id,
    set_parent_account_cookie,
)
from child_profile_changes import (
    RELATIONSHIP_OPTIONS,
    build_child_profile_change_details,
    build_child_profile_change_summary,
    build_child_profile_payload,
    merge_child_profile_form_data,
    validate_child_profile_payload,
)
from database import get_session
from models import (
    Child,
    ChildProfileChangeRequest,
    ChildProfileChangeRequestStatus,
    DailyContactEntry,
    Family,
    Notice,
    NoticeRead,
    NoticeStatus,
    ParentAccount,
    ParentAccountStatus,
    ParentContactType,
    ParentChildLink,
    ProfileChangeNotification,
    Survey,
    SurveyAnswer,
    SurveyAnswerUnit,
    SurveyAudienceType,
    SurveyQuestion,
    SurveyStatus,
)
from survey_service import (
    answer_value_for_display,
    closes_soon,
    eligible_children_for_survey,
    load_existing_survey_answer,
    resolve_parent_answer_scope,
    response_by_question,
    save_survey_answer,
    survey_is_open,
    survey_matches_parent_targets,
)
from time_utils import ensure_utc, utc_now

router = APIRouter(prefix="/parent-portal", tags=["parent_portal"])
templates = Jinja2Templates(directory="templates")

PROFILE_FIELD_LABELS = {
    "email": "メールアドレス",
    "phone": "電話番号",
    "home_address": "現住所",
    "workplace": "勤務先",
    "workplace_address": "勤務先住所",
    "workplace_phone": "勤務先電話番号",
}

CHILD_PROFILE_NOTICE_MESSAGES = {
    "submitted": "子ども情報の変更申請を送信しました。園で承認されると反映されます。",
    "updated": "子ども情報の変更申請を更新しました。",
    "cancelled": "申請中の変更を取り下げました。",
}
def _parse_target_date(raw: Optional[str]) -> date:
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="日付は YYYY-MM-DD 形式で指定してください") from exc


def _parse_optional_int(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _contact_form_data(entry: Optional[DailyContactEntry], form_data: Optional[dict[str, str]] = None) -> dict[str, str]:
    if form_data is not None:
        return form_data
    return {
        "contact_type": entry.contact_type.value if entry and entry.contact_type else ParentContactType.present.value,
        "temperature": entry.temperature or "" if entry else "",
        "sleep_notes": entry.sleep_notes or "" if entry else "",
        "breakfast_status": entry.breakfast_status or "" if entry else "",
        "bowel_movement_status": entry.bowel_movement_status or "" if entry else "",
        "mood": entry.mood or "" if entry else "",
        "cough": entry.cough or "" if entry else "",
        "runny_nose": entry.runny_nose or "" if entry else "",
        "medication": entry.medication or "" if entry else "",
        "condition_note": entry.condition_note or "" if entry else "",
        "contact_note": entry.contact_note or "" if entry else "",
        "absence_temperature": entry.absence_temperature or "" if entry else "",
        "absence_symptoms": entry.absence_symptoms or "" if entry else "",
        "absence_diagnosis": entry.absence_diagnosis or "" if entry else "",
        "absence_note": entry.absence_note or "" if entry else "",
    }


def _render_contact_form(
    request: Request,
    *,
    current_parent_user: ParentAccount,
    child: Child,
    entry: Optional[DailyContactEntry],
    target_date_value: str,
    notice: str = "",
    form_error: str = "",
    form_data: Optional[dict[str, str]] = None,
):
    return templates.TemplateResponse(
        request,
        "parent_portal/contact_form.html",
        {
            "request": request,
            "current_parent_user": current_parent_user,
            "parent_portal_mode": True,
            "child": child,
            "entry": entry,
            "target_date_value": target_date_value,
            "notice": notice,
            "form_error": form_error,
            "form_data": _contact_form_data(entry, form_data),
            "parent_contact_types": list(ParentContactType),
        },
    )


def _get_parent_account(request: Request, session: Session) -> Optional[ParentAccount]:
    parent_account_id = get_current_parent_account_id(request)
    if not parent_account_id:
        return None

    statement = (
        select(ParentAccount)
        .options(
            selectinload(ParentAccount.family)
            .selectinload(Family.children)
            .selectinload(Child.classroom),
            selectinload(ParentAccount.family)
            .selectinload(Family.children)
            .selectinload(Child.guardians),
            selectinload(ParentAccount.family).selectinload(Family.parent_accounts),
            selectinload(ParentAccount.child_links)
            .selectinload(ParentChildLink.child)
            .selectinload(Child.classroom),
            selectinload(ParentAccount.child_links)
            .selectinload(ParentChildLink.child)
            .selectinload(Child.guardians),
        )
        .where(ParentAccount.id == parent_account_id)
    )
    account = session.exec(statement).first()
    if not account or account.status != ParentAccountStatus.active:
        return None
    return account


def _linked_children(parent_account: ParentAccount) -> list[Child]:
    if parent_account.family and parent_account.family.children:
        children = list(parent_account.family.children)
    else:
        children = [link.child for link in parent_account.child_links if link.child is not None]

    unique_children: dict[int, Child] = {}
    for child in children:
        if child and child.id is not None:
            unique_children[child.id] = child
    return sorted(
        unique_children.values(),
        key=lambda child: (
            child.classroom.display_order if child.classroom else 999,
            child.last_name_kana,
            child.first_name_kana,
        ),
    )


def _child_ids(parent_account: ParentAccount) -> set[int]:
    return {child.id for child in _linked_children(parent_account) if child.id is not None}


def _classroom_ids(parent_account: ParentAccount) -> set[int]:
    return {
        child.classroom_id
        for child in _linked_children(parent_account)
        if child.classroom_id is not None
    }


def _load_accessible_child(parent_account: ParentAccount, child_id: int) -> Child:
    for child in _linked_children(parent_account):
        if child.id == child_id:
            return child
    raise HTTPException(status_code=404, detail="対象の園児にアクセスできません")


def _load_pending_child_profile_request(
    session: Session,
    *,
    parent_account_id: int,
    child_id: int,
) -> Optional[ChildProfileChangeRequest]:
    return session.exec(
        select(ChildProfileChangeRequest)
        .where(
            ChildProfileChangeRequest.parent_account_id == parent_account_id,
            ChildProfileChangeRequest.child_id == child_id,
            ChildProfileChangeRequest.status == ChildProfileChangeRequestStatus.pending,
        )
        .order_by(ChildProfileChangeRequest.submitted_at.desc())
    ).first()


def _load_pending_child_profile_requests_by_child_id(
    session: Session,
    *,
    parent_account_id: int,
    child_ids: list[int],
) -> dict[int, ChildProfileChangeRequest]:
    if not child_ids:
        return {}
    requests = session.exec(
        select(ChildProfileChangeRequest)
        .where(
            ChildProfileChangeRequest.parent_account_id == parent_account_id,
            ChildProfileChangeRequest.child_id.in_(child_ids),
            ChildProfileChangeRequest.status == ChildProfileChangeRequestStatus.pending,
        )
        .order_by(ChildProfileChangeRequest.submitted_at.desc())
    ).all()
    request_by_child_id: dict[int, ChildProfileChangeRequest] = {}
    for change_request in requests:
        request_by_child_id.setdefault(change_request.child_id, change_request)
    return request_by_child_id


def _notice_is_active(notice: Notice, now: datetime) -> bool:
    publish_start_at = ensure_utc(notice.publish_start_at)
    publish_end_at = ensure_utc(notice.publish_end_at)
    if notice.status != NoticeStatus.published:
        return False
    if publish_start_at and publish_start_at > now:
        return False
    if publish_end_at and publish_end_at < now:
        return False
    return True


def _notice_matches_account(notice: Notice, parent_account: ParentAccount) -> bool:
    child_ids = _child_ids(parent_account)
    classroom_ids = _classroom_ids(parent_account)

    if not notice.targets:
        return True

    for target in notice.targets:
        if target.target_type.value == "all":
            return True
        if target.target_type.value == "classroom":
            value = _parse_optional_int(target.target_value)
            if value is not None and value in classroom_ids:
                return True
        if target.target_type.value == "child":
            value = _parse_optional_int(target.target_value)
            if value is not None and value in child_ids:
                return True
    return False


def _load_visible_notices(session: Session, parent_account: ParentAccount) -> list[Notice]:
    now = utc_now()
    notices = session.exec(
        select(Notice)
        .options(selectinload(Notice.targets), selectinload(Notice.reads))
        .order_by(Notice.priority.desc(), Notice.publish_start_at.desc(), Notice.created_at.desc())
    ).all()
    return [notice for notice in notices if _notice_is_active(notice, now) and _notice_matches_account(notice, parent_account)]


def _read_notice_ids(parent_account: ParentAccount, notices: list[Notice]) -> set[int]:
    account_id = parent_account.id
    read_ids: set[int] = set()
    for notice in notices:
        for read in notice.reads:
            if read.parent_account_id == account_id:
                read_ids.add(notice.id)
                break
    return read_ids


def _normalized_text(value: Optional[str]) -> Optional[str]:
    cleaned = (value or "").strip()
    return cleaned or None


def _profile_change_details(account: ParentAccount, updated_values: dict[str, Optional[str]]) -> dict[str, dict[str, str]]:
    details: dict[str, dict[str, str]] = {}
    for field_name, label in PROFILE_FIELD_LABELS.items():
        old_value = _normalized_text(getattr(account, field_name, None))
        new_value = _normalized_text(updated_values.get(field_name))
        if old_value != new_value:
            details[field_name] = {
                "label": label,
                "old": old_value or "未登録",
                "new": new_value or "未登録",
            }
    return details


@router.get("/login", response_class=HTMLResponse)
def parent_login_page(
    request: Request,
    parent_account_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    accounts = session.exec(
        select(ParentAccount).where(ParentAccount.status == ParentAccountStatus.active).order_by(ParentAccount.display_name)
    ).all()
    return templates.TemplateResponse(
        request,
        "parent_portal/login.html",
        {
            "request": request,
            "accounts": accounts,
            "selected_parent_account_id": parent_account_id,
            "current_parent_user": current_parent_user,
            "parent_portal_mode": True,
        },
    )


@router.post("/login")
def parent_login(
    parent_account_id: int = Form(...),
    session: Session = Depends(get_session),
):
    account = session.get(ParentAccount, parent_account_id)
    if not account or account.status != ParentAccountStatus.active:
        raise HTTPException(status_code=404, detail="保護者アカウントが見つかりません")

    account.last_login_at = utc_now()
    account.updated_at = utc_now()
    session.add(account)
    session.commit()

    response = RedirectResponse(url="/parent-portal/", status_code=303)
    set_parent_account_cookie(response, parent_account_id)
    return response


@router.get("/mock-login/{parent_account_id}")
def parent_mock_login(
    parent_account_id: int,
    session: Session = Depends(get_session),
):
    account = session.get(ParentAccount, parent_account_id)
    if not account or account.status != ParentAccountStatus.active:
        raise HTTPException(status_code=404, detail="保護者アカウントが見つかりません")

    account.last_login_at = utc_now()
    account.updated_at = utc_now()
    session.add(account)
    session.commit()

    response = RedirectResponse(url="/parent-portal/", status_code=303)
    set_parent_account_cookie(response, parent_account_id)
    return response


@router.post("/logout")
def parent_logout():
    response = RedirectResponse(url="/parent-portal/login", status_code=303)
    clear_parent_account_cookie(response)
    return response


@router.get("/", response_class=HTMLResponse)
def parent_home(
    request: Request,
    target_date: Optional[str] = Query(default=None, alias="date"),
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    if not current_parent_user:
        return RedirectResponse(url="/parent-portal/login", status_code=303)

    day = _parse_target_date(target_date)
    children = _linked_children(current_parent_user)
    child_ids = [child.id for child in children if child.id is not None]

    entries = (
        session.exec(
            select(DailyContactEntry).where(
                DailyContactEntry.child_id.in_(child_ids) if child_ids else False,
                DailyContactEntry.target_date == day,
            )
        ).all()
        if child_ids
        else []
    )
    entry_by_child_id = {entry.child_id: entry for entry in entries}
    pending_request_by_child_id = _load_pending_child_profile_requests_by_child_id(
        session,
        parent_account_id=current_parent_user.id,
        child_ids=child_ids,
    )

    notices = _load_visible_notices(session, current_parent_user)
    read_notice_ids = _read_notice_ids(current_parent_user, notices)

    return templates.TemplateResponse(
        request,
        "parent_portal/home.html",
        {
            "request": request,
            "current_parent_user": current_parent_user,
            "parent_portal_mode": True,
            "target_date": day,
            "target_date_value": day.isoformat(),
            "children": children,
            "entry_by_child_id": entry_by_child_id,
            "pending_request_by_child_id": pending_request_by_child_id,
            "latest_notices": notices[:5],
            "read_notice_ids": read_notice_ids,
            "unread_notice_count": sum(1 for notice in notices if notice.id not in read_notice_ids),
        },
    )


@router.get("/profile", response_class=HTMLResponse)
def parent_profile_form(
    request: Request,
    notice: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    if not current_parent_user:
        return RedirectResponse(url="/parent-portal/login", status_code=303)

    return templates.TemplateResponse(
        request,
        "parent_portal/profile.html",
        {
            "request": request,
            "current_parent_user": current_parent_user,
            "parent_portal_mode": True,
            "notice": "プロフィールを更新しました。" if notice == "updated" else "",
            "form_error": "",
        },
    )


@router.get("/children/profile", response_class=HTMLResponse)
def parent_child_profile_selector(
    request: Request,
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    if not current_parent_user:
        return RedirectResponse(url="/parent-portal/login", status_code=303)

    children = _linked_children(current_parent_user)
    if len(children) == 1:
        return RedirectResponse(url=f"/parent-portal/children/{children[0].id}/profile", status_code=303)

    child_ids = [child.id for child in children if child.id is not None]
    pending_request_by_child_id = _load_pending_child_profile_requests_by_child_id(
        session,
        parent_account_id=current_parent_user.id,
        child_ids=child_ids,
    )
    return templates.TemplateResponse(
        request,
        "parent_portal/child_profile_selector.html",
        {
            "request": request,
            "current_parent_user": current_parent_user,
            "parent_portal_mode": True,
            "children": children,
            "pending_request_by_child_id": pending_request_by_child_id,
        },
    )


@router.post("/profile")
def save_parent_profile(
    request: Request,
    email: str = Form(...),
    phone: str = Form(""),
    home_address: str = Form(""),
    workplace: str = Form(""),
    workplace_address: str = Form(""),
    workplace_phone: str = Form(""),
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    if not current_parent_user:
        return RedirectResponse(url="/parent-portal/login", status_code=303)

    normalized_email = (email or "").strip()
    existing = session.exec(
        select(ParentAccount).where(
            ParentAccount.email == normalized_email,
            ParentAccount.id != current_parent_user.id,
        )
    ).first()
    if existing:
        return templates.TemplateResponse(
            request,
            "parent_portal/profile.html",
            {
                "request": request,
                "current_parent_user": current_parent_user,
                "parent_portal_mode": True,
                "notice": "",
                "form_error": "このメールアドレスは別の保護者アカウントで利用されています。",
                "form_data": {
                    "email": normalized_email,
                    "phone": phone,
                    "home_address": home_address,
                    "workplace": workplace,
                    "workplace_address": workplace_address,
                    "workplace_phone": workplace_phone,
                },
            },
            status_code=400,
        )

    updated_values = {
        "email": normalized_email,
        "phone": phone,
        "home_address": home_address,
        "workplace": workplace,
        "workplace_address": workplace_address,
        "workplace_phone": workplace_phone,
    }
    change_details = _profile_change_details(current_parent_user, updated_values)

    current_parent_user.email = normalized_email
    current_parent_user.phone = _normalized_text(phone)
    current_parent_user.home_address = _normalized_text(home_address)
    current_parent_user.workplace = _normalized_text(workplace)
    current_parent_user.workplace_address = _normalized_text(workplace_address)
    current_parent_user.workplace_phone = _normalized_text(workplace_phone)
    current_parent_user.updated_at = utc_now()
    session.add(current_parent_user)

    if change_details:
        changed_labels = [detail["label"] for detail in change_details.values()]
        session.add(
            ProfileChangeNotification(
                parent_account_id=current_parent_user.id,
                change_summary=f"{current_parent_user.display_name} がプロフィールを変更しました: {', '.join(changed_labels)}",
                change_details=change_details,
            )
        )

    session.commit()
    return RedirectResponse(url="/parent-portal/profile?notice=updated", status_code=303)


@router.get("/children/{child_id}/profile", response_class=HTMLResponse)
def parent_child_profile_form(
    request: Request,
    child_id: int,
    notice: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    if not current_parent_user:
        return RedirectResponse(url="/parent-portal/login", status_code=303)

    child = _load_accessible_child(current_parent_user, child_id)
    pending_request = _load_pending_child_profile_request(
        session,
        parent_account_id=current_parent_user.id,
        child_id=child_id,
    )

    return templates.TemplateResponse(
        request,
        "parent_portal/child_profile_form.html",
        {
            "request": request,
            "current_parent_user": current_parent_user,
            "parent_portal_mode": True,
            "child": child,
            "form_data": merge_child_profile_form_data(child, pending_request.request_data if pending_request else None),
            "pending_request": pending_request,
            "relationship_options": RELATIONSHIP_OPTIONS,
            "notice": CHILD_PROFILE_NOTICE_MESSAGES.get(notice or "", ""),
            "form_error": "",
        },
    )


@router.post("/children/{child_id}/profile")
def save_parent_child_profile_request(
    request: Request,
    child_id: int,
    last_name: str = Form(...),
    first_name: str = Form(...),
    last_name_kana: str = Form(...),
    first_name_kana: str = Form(...),
    birth_date: Optional[str] = Form(None),
    enrollment_date: Optional[str] = Form(None),
    withdrawal_date: Optional[str] = Form(None),
    status: str = Form("enrolled"),
    home_address: Optional[str] = Form(""),
    home_phone: Optional[str] = Form(""),
    allergy: str = Form(""),
    medical_notes: str = Form(""),
    g1_last_name: str = Form(""),
    g1_first_name: str = Form(""),
    g1_last_name_kana: str = Form(""),
    g1_first_name_kana: str = Form(""),
    g1_relationship: str = Form("父"),
    g1_phone: str = Form(""),
    g1_workplace: str = Form(""),
    g1_workplace_address: str = Form(""),
    g1_workplace_phone: str = Form(""),
    g2_last_name: str = Form(""),
    g2_first_name: str = Form(""),
    g2_last_name_kana: str = Form(""),
    g2_first_name_kana: str = Form(""),
    g2_relationship: str = Form("母"),
    g2_phone: str = Form(""),
    g2_workplace: str = Form(""),
    g2_workplace_address: str = Form(""),
    g2_workplace_phone: str = Form(""),
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    if not current_parent_user:
        return RedirectResponse(url="/parent-portal/login", status_code=303)

    child = _load_accessible_child(current_parent_user, child_id)
    pending_request = _load_pending_child_profile_request(
        session,
        parent_account_id=current_parent_user.id,
        child_id=child_id,
    )
    payload = build_child_profile_payload(
        last_name=last_name,
        first_name=first_name,
        last_name_kana=last_name_kana,
        first_name_kana=first_name_kana,
        birth_date=birth_date,
        enrollment_date=enrollment_date,
        withdrawal_date=withdrawal_date,
        status=status,
        home_address=home_address,
        home_phone=home_phone,
        allergy=allergy,
        medical_notes=medical_notes,
        g1_last_name=g1_last_name,
        g1_first_name=g1_first_name,
        g1_last_name_kana=g1_last_name_kana,
        g1_first_name_kana=g1_first_name_kana,
        g1_relationship=g1_relationship,
        g1_phone=g1_phone,
        g1_workplace=g1_workplace,
        g1_workplace_address=g1_workplace_address,
        g1_workplace_phone=g1_workplace_phone,
        g2_last_name=g2_last_name,
        g2_first_name=g2_first_name,
        g2_last_name_kana=g2_last_name_kana,
        g2_first_name_kana=g2_first_name_kana,
        g2_relationship=g2_relationship,
        g2_phone=g2_phone,
        g2_workplace=g2_workplace,
        g2_workplace_address=g2_workplace_address,
        g2_workplace_phone=g2_workplace_phone,
    )

    validation_error = validate_child_profile_payload(payload)
    if validation_error:
        return templates.TemplateResponse(
            request,
            "parent_portal/child_profile_form.html",
            {
                "request": request,
                "current_parent_user": current_parent_user,
                "parent_portal_mode": True,
                "child": child,
                "form_data": payload,
                "pending_request": pending_request,
                "relationship_options": RELATIONSHIP_OPTIONS,
                "notice": "",
                "form_error": validation_error,
            },
            status_code=400,
        )

    change_details = build_child_profile_change_details(child, payload)
    if not change_details:
        if pending_request:
            session.delete(pending_request)
            session.commit()
            return RedirectResponse(
                url=f"/parent-portal/children/{child_id}/profile?notice=cancelled",
                status_code=303,
            )

        return templates.TemplateResponse(
            request,
            "parent_portal/child_profile_form.html",
            {
                "request": request,
                "current_parent_user": current_parent_user,
                "parent_portal_mode": True,
                "child": child,
                "form_data": payload,
                "pending_request": pending_request,
                "relationship_options": RELATIONSHIP_OPTIONS,
                "notice": "",
                "form_error": "変更箇所がありません。",
            },
            status_code=400,
        )

    now = utc_now()
    change_summary = build_child_profile_change_summary(
        current_parent_user.display_name,
        child.full_name,
        change_details,
    )
    if pending_request:
        pending_request.change_summary = change_summary
        pending_request.request_data = payload
        pending_request.change_details = change_details
        pending_request.submitted_at = now
        pending_request.updated_at = now
        session.add(pending_request)
        notice_key = "updated"
    else:
        session.add(
            ChildProfileChangeRequest(
                child_id=child_id,
                parent_account_id=current_parent_user.id,
                status=ChildProfileChangeRequestStatus.pending,
                change_summary=change_summary,
                request_data=payload,
                change_details=change_details,
                submitted_at=now,
                updated_at=now,
            )
        )
        notice_key = "submitted"

    session.commit()
    return RedirectResponse(
        url=f"/parent-portal/children/{child_id}/profile?notice={notice_key}",
        status_code=303,
    )


@router.get("/children/{child_id}/contact", response_class=HTMLResponse)
def parent_contact_form(
    request: Request,
    child_id: int,
    target_date: Optional[str] = Query(default=None, alias="date"),
    notice: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    if not current_parent_user:
        return RedirectResponse(url="/parent-portal/login", status_code=303)

    child = _load_accessible_child(current_parent_user, child_id)
    day = _parse_target_date(target_date)
    entry = session.exec(
        select(DailyContactEntry).where(
            DailyContactEntry.child_id == child_id,
            DailyContactEntry.target_date == day,
        )
    ).first()

    return _render_contact_form(
        request,
        current_parent_user=current_parent_user,
        child=child,
        entry=entry,
        target_date_value=day.isoformat(),
        notice="日次連絡を保存しました。" if notice == "saved" else "",
    )


@router.post("/children/{child_id}/contact")
def save_parent_contact(
    request: Request,
    child_id: int,
    target_date: str = Form(..., alias="date"),
    contact_type: str = Form(ParentContactType.present.value),
    temperature: str = Form(""),
    sleep_notes: str = Form(""),
    breakfast_status: str = Form(""),
    bowel_movement_status: str = Form(""),
    mood: str = Form(""),
    cough: str = Form(""),
    runny_nose: str = Form(""),
    medication: str = Form(""),
    condition_note: str = Form(""),
    contact_note: str = Form(""),
    absence_temperature: str = Form(""),
    absence_symptoms: str = Form(""),
    absence_diagnosis: str = Form(""),
    absence_note: str = Form(""),
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    if not current_parent_user:
        return RedirectResponse(url="/parent-portal/login", status_code=303)

    child = _load_accessible_child(current_parent_user, child_id)
    day = _parse_target_date(target_date)
    form_data = {
        "contact_type": contact_type,
        "temperature": temperature,
        "sleep_notes": sleep_notes,
        "breakfast_status": breakfast_status,
        "bowel_movement_status": bowel_movement_status,
        "mood": mood,
        "cough": cough,
        "runny_nose": runny_nose,
        "medication": medication,
        "condition_note": condition_note,
        "contact_note": contact_note,
        "absence_temperature": absence_temperature,
        "absence_symptoms": absence_symptoms,
        "absence_diagnosis": absence_diagnosis,
        "absence_note": absence_note,
    }

    try:
        selected_contact_type = ParentContactType(contact_type)
    except ValueError:
        return _render_contact_form(
            request,
            current_parent_user=current_parent_user,
            child=child,
            entry=None,
            target_date_value=day.isoformat(),
            form_error="出席または欠席を選択してください。",
            form_data=form_data,
        )

    entry = session.exec(
        select(DailyContactEntry).where(
            DailyContactEntry.child_id == child_id,
            DailyContactEntry.target_date == day,
        )
    ).first()
    now = utc_now()
    if not entry:
        entry = DailyContactEntry(
            child_id=child_id,
            parent_account_id=current_parent_user.id,
            target_date=day,
            submitted_at=now,
        )
    else:
        entry.parent_account_id = current_parent_user.id
        if not entry.submitted_at:
            entry.submitted_at = now

    entry.contact_type = selected_contact_type
    normalized_absence_temperature = (absence_temperature or "").strip()
    normalized_absence_symptoms = (absence_symptoms or "").strip()
    normalized_absence_diagnosis = (absence_diagnosis or "").strip()
    normalized_absence_note = (absence_note or "").strip()

    if selected_contact_type == ParentContactType.present:
        entry.temperature = (temperature or "").strip() or None
        entry.sleep_notes = (sleep_notes or "").strip() or None
        entry.breakfast_status = (breakfast_status or "").strip() or None
        entry.bowel_movement_status = (bowel_movement_status or "").strip() or None
        entry.mood = (mood or "").strip() or None
        entry.cough = (cough or "").strip() or None
        entry.runny_nose = (runny_nose or "").strip() or None
        entry.medication = (medication or "").strip() or None
        entry.condition_note = (condition_note or "").strip() or None
        entry.contact_note = (contact_note or "").strip() or None
        entry.absence_temperature = None
        entry.absence_symptoms = None
        entry.absence_diagnosis = None
        entry.absence_note = None
    else:
        if selected_contact_type == ParentContactType.absent_sick and not normalized_absence_temperature:
            return _render_contact_form(
                request,
                current_parent_user=current_parent_user,
                child=child,
                entry=entry,
                target_date_value=day.isoformat(),
                form_error="病欠の場合は現在の体温を入力してください。",
                form_data=form_data,
            )
        if selected_contact_type == ParentContactType.absent_sick and not normalized_absence_symptoms:
            return _render_contact_form(
                request,
                current_parent_user=current_parent_user,
                child=child,
                entry=entry,
                target_date_value=day.isoformat(),
                form_error="病欠の場合は症状を入力してください。",
                form_data=form_data,
            )

        entry.temperature = None
        entry.sleep_notes = None
        entry.breakfast_status = None
        entry.bowel_movement_status = None
        entry.mood = None
        entry.cough = None
        entry.runny_nose = None
        entry.medication = None
        entry.condition_note = None
        entry.contact_note = None
        entry.absence_temperature = normalized_absence_temperature or None
        entry.absence_symptoms = normalized_absence_symptoms or None
        entry.absence_diagnosis = normalized_absence_diagnosis or None
        entry.absence_note = normalized_absence_note or None

    entry.updated_at = now
    session.add(entry)
    sync_attendance_alarm(session, child_id=child_id, target_date=day, entry=entry, now=now)
    session.commit()

    return RedirectResponse(
        url=f"/parent-portal/children/{child_id}/contact?date={day.isoformat()}&notice=saved",
        status_code=303,
    )


@router.get("/history", response_class=HTMLResponse)
def parent_contact_history(
    request: Request,
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    if not current_parent_user:
        return RedirectResponse(url="/parent-portal/login", status_code=303)

    child_ids = list(_child_ids(current_parent_user))
    entries = (
        session.exec(
            select(DailyContactEntry)
            .options(selectinload(DailyContactEntry.child))
            .where(DailyContactEntry.child_id.in_(child_ids) if child_ids else False)
            .order_by(DailyContactEntry.target_date.desc(), DailyContactEntry.updated_at.desc())
        ).all()
        if child_ids
        else []
    )

    return templates.TemplateResponse(
        request,
        "parent_portal/history.html",
        {
            "request": request,
            "current_parent_user": current_parent_user,
            "parent_portal_mode": True,
            "entries": entries,
        },
    )


def _load_parent_survey(session: Session, survey_id: int) -> Optional[Survey]:
    return session.exec(
        select(Survey)
        .options(
            selectinload(Survey.targets),
            selectinload(Survey.questions).selectinload(SurveyQuestion.options),
            selectinload(Survey.answers).selectinload(SurveyAnswer.responses),
        )
        .where(Survey.id == survey_id)
    ).first()


def _load_visible_parent_surveys(session: Session, parent_account: ParentAccount) -> list[Survey]:
    surveys = session.exec(
        select(Survey)
        .options(
            selectinload(Survey.targets),
            selectinload(Survey.questions).selectinload(SurveyQuestion.options),
            selectinload(Survey.answers),
        )
        .where(
            Survey.audience_type == SurveyAudienceType.parent,
            Survey.status == SurveyStatus.published,
        )
        .order_by(Survey.closes_at, Survey.updated_at.desc())
    ).all()
    return [
        survey
        for survey in surveys
        if survey_is_open(survey, utc_now()) and survey_matches_parent_targets(survey, parent_account)
    ]


@router.get("/surveys", response_class=HTMLResponse)
def parent_survey_list(
    request: Request,
    notice: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    if not current_parent_user:
        return RedirectResponse(url="/parent-portal/login", status_code=303)

    survey_cards = []
    for survey in _load_visible_parent_surveys(session, current_parent_user):
        if survey.answer_unit == SurveyAnswerUnit.family:
            scope = resolve_parent_answer_scope(survey, current_parent_user)
            answer = load_existing_survey_answer(session, survey, scope) if scope else None
            survey_cards.append(
                {
                    "survey": survey,
                    "answer": answer,
                    "blocked": scope is None,
                    "children": [],
                    "closes_soon": closes_soon(survey) and answer is None,
                }
            )
        else:
            children = []
            for child in eligible_children_for_survey(survey, current_parent_user):
                scope = resolve_parent_answer_scope(survey, current_parent_user, child.id)
                answer = load_existing_survey_answer(session, survey, scope) if scope else None
                children.append({"child": child, "answer": answer})
            survey_cards.append(
                {
                    "survey": survey,
                    "answer": None,
                    "blocked": False,
                    "children": children,
                    "closes_soon": closes_soon(survey) and any(item["answer"] is None for item in children),
                }
            )

    return templates.TemplateResponse(
        request,
        "parent_portal/surveys.html",
        {
            "request": request,
            "current_parent_user": current_parent_user,
            "parent_portal_mode": True,
            "survey_cards": survey_cards,
            "notice": "回答しました。" if notice == "saved" else "",
        },
    )


@router.get("/surveys/{survey_id}", response_class=HTMLResponse)
def parent_survey_form(
    request: Request,
    survey_id: int,
    child_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    if not current_parent_user:
        return RedirectResponse(url="/parent-portal/login", status_code=303)

    survey = _load_parent_survey(session, survey_id)
    if not survey or not survey_is_open(survey, utc_now()) or not survey_matches_parent_targets(survey, current_parent_user):
        raise HTTPException(status_code=404, detail="アンケートが見つかりません")

    eligible_children = eligible_children_for_survey(survey, current_parent_user)
    if survey.answer_unit == SurveyAnswerUnit.child and child_id is None:
        if len(eligible_children) == 1 and eligible_children[0].id is not None:
            return RedirectResponse(url=f"/parent-portal/surveys/{survey_id}?child_id={eligible_children[0].id}", status_code=303)
        return templates.TemplateResponse(
            request,
            "parent_portal/survey_child_selector.html",
            {
                "request": request,
                "current_parent_user": current_parent_user,
                "parent_portal_mode": True,
                "survey": survey,
                "children": eligible_children,
            },
        )

    scope = resolve_parent_answer_scope(survey, current_parent_user, child_id)
    if scope is None:
        return templates.TemplateResponse(
            request,
            "parent_portal/survey_form.html",
            {
                "request": request,
                "current_parent_user": current_parent_user,
                "parent_portal_mode": True,
                "survey": survey,
                "questions": sorted(survey.questions, key=lambda item: (item.order, item.id or 0)),
                "answer": None,
                "responses": {},
                "child_id": child_id,
                "errors": ["家族情報を特定できないため、このアンケートには回答できません。園へお問い合わせください。"],
                "blocked": True,
                "answer_value_for_display": answer_value_for_display,
            },
            status_code=409,
        )

    answer = load_existing_survey_answer(session, survey, scope)
    return templates.TemplateResponse(
        request,
        "parent_portal/survey_form.html",
        {
            "request": request,
            "current_parent_user": current_parent_user,
            "parent_portal_mode": True,
            "survey": survey,
            "questions": sorted(survey.questions, key=lambda item: (item.order, item.id or 0)),
            "answer": answer,
            "responses": response_by_question(answer),
            "child_id": child_id,
            "errors": [],
            "blocked": False,
            "answer_value_for_display": answer_value_for_display,
        },
    )


@router.post("/surveys/{survey_id}")
async def save_parent_survey_answer(
    request: Request,
    survey_id: int,
    child_id: Optional[int] = Form(None),
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    if not current_parent_user:
        return RedirectResponse(url="/parent-portal/login", status_code=303)

    survey = _load_parent_survey(session, survey_id)
    if not survey or not survey_is_open(survey, utc_now()) or not survey_matches_parent_targets(survey, current_parent_user):
        raise HTTPException(status_code=404, detail="アンケートが見つかりません")

    scope = resolve_parent_answer_scope(survey, current_parent_user, child_id)
    if scope is None:
        return templates.TemplateResponse(
            request,
            "parent_portal/survey_form.html",
            {
                "request": request,
                "current_parent_user": current_parent_user,
                "parent_portal_mode": True,
                "survey": survey,
                "questions": sorted(survey.questions, key=lambda item: (item.order, item.id or 0)),
                "answer": None,
                "responses": {},
                "child_id": child_id,
                "errors": ["家族情報を特定できないため、このアンケートには回答できません。園へお問い合わせください。"],
                "blocked": True,
                "answer_value_for_display": answer_value_for_display,
            },
            status_code=409,
        )

    form_data = await request.form()
    result = save_survey_answer(
        session,
        survey=survey,
        scope=scope,
        form_data=form_data,
        parent_account=current_parent_user,
    )
    if result.errors:
        answer = load_existing_survey_answer(session, survey, scope)
        return templates.TemplateResponse(
            request,
            "parent_portal/survey_form.html",
            {
                "request": request,
                "current_parent_user": current_parent_user,
                "parent_portal_mode": True,
                "survey": survey,
                "questions": sorted(survey.questions, key=lambda item: (item.order, item.id or 0)),
                "answer": answer,
                "responses": response_by_question(answer),
                "child_id": child_id,
                "errors": result.errors,
                "blocked": False,
                "answer_value_for_display": answer_value_for_display,
            },
            status_code=400,
        )

    session.commit()
    return RedirectResponse(url="/parent-portal/surveys?notice=saved", status_code=303)


@router.get("/notices", response_class=HTMLResponse)
def parent_notice_list(
    request: Request,
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    if not current_parent_user:
        return RedirectResponse(url="/parent-portal/login", status_code=303)

    notices = _load_visible_notices(session, current_parent_user)
    read_notice_ids = _read_notice_ids(current_parent_user, notices)

    return templates.TemplateResponse(
        request,
        "parent_portal/notices.html",
        {
            "request": request,
            "current_parent_user": current_parent_user,
            "parent_portal_mode": True,
            "notices": notices,
            "read_notice_ids": read_notice_ids,
        },
    )


@router.get("/notices/{notice_id}", response_class=HTMLResponse)
def parent_notice_detail(
    request: Request,
    notice_id: int,
    session: Session = Depends(get_session),
):
    current_parent_user = _get_parent_account(request, session)
    if not current_parent_user:
        return RedirectResponse(url="/parent-portal/login", status_code=303)

    notice = session.exec(
        select(Notice)
        .options(selectinload(Notice.targets), selectinload(Notice.reads))
        .where(Notice.id == notice_id)
    ).first()
    if not notice or not _notice_is_active(notice, utc_now()) or not _notice_matches_account(notice, current_parent_user):
        raise HTTPException(status_code=404, detail="お知らせが見つかりません")

    existing_read = session.exec(
        select(NoticeRead).where(
            NoticeRead.notice_id == notice_id,
            NoticeRead.parent_account_id == current_parent_user.id,
        )
    ).first()
    if not existing_read:
        session.add(
            NoticeRead(
                notice_id=notice_id,
                parent_account_id=current_parent_user.id,
                read_at=utc_now(),
            )
        )
        session.commit()

    return templates.TemplateResponse(
        request,
        "parent_portal/notice_detail.html",
        {
            "request": request,
            "current_parent_user": current_parent_user,
            "parent_portal_mode": True,
            "notice": notice,
        },
    )
