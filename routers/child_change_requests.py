from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from auth import get_current_staff_user, require_child_record_manager
from child_profile_changes import apply_child_profile_payload, merge_child_profile_form_data
from database import get_session
from models import (
    Child,
    ChildProfileChangeRequest,
    ChildProfileChangeRequestStatus,
    Family,
)
from time_utils import utc_now

router = APIRouter(prefix="/child-change-requests", tags=["child_change_requests"])
templates = Jinja2Templates(directory="templates")
def _parse_status_filter(raw_status: Optional[str]) -> Optional[ChildProfileChangeRequestStatus]:
    if not raw_status or raw_status == "all":
        return None
    try:
        return ChildProfileChangeRequestStatus(raw_status)
    except ValueError:
        return ChildProfileChangeRequestStatus.pending


def _load_change_request(session: Session, request_id: int) -> ChildProfileChangeRequest:
    change_request = session.exec(
        select(ChildProfileChangeRequest)
        .options(
            selectinload(ChildProfileChangeRequest.child).selectinload(Child.guardians),
            selectinload(ChildProfileChangeRequest.child).selectinload(Child.classroom),
            selectinload(ChildProfileChangeRequest.child).selectinload(Child.family).selectinload(Family.children),
            selectinload(ChildProfileChangeRequest.parent_account),
        )
        .where(ChildProfileChangeRequest.id == request_id)
    ).first()
    if not change_request:
        raise HTTPException(status_code=404, detail="変更申請が見つかりません")
    return change_request


@router.get("/", response_class=HTMLResponse)
def child_change_request_list(
    request: Request,
    status: str = Query(default="pending"),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_child_record_manager(current_user)
    status_filter = _parse_status_filter(status)

    statement = (
        select(ChildProfileChangeRequest)
        .options(
            selectinload(ChildProfileChangeRequest.child).selectinload(Child.classroom),
            selectinload(ChildProfileChangeRequest.child).selectinload(Child.family).selectinload(Family.children),
            selectinload(ChildProfileChangeRequest.parent_account),
        )
        .order_by(ChildProfileChangeRequest.submitted_at.desc())
    )
    if status_filter:
        statement = statement.where(ChildProfileChangeRequest.status == status_filter)

    change_requests = session.exec(statement).all()
    pending_count = session.exec(
        select(ChildProfileChangeRequest).where(
            ChildProfileChangeRequest.status == ChildProfileChangeRequestStatus.pending
        )
    ).all()

    return templates.TemplateResponse(
        request,
        "child_change_requests/list.html",
        {
            "request": request,
            "current_user": current_user,
            "change_requests": change_requests,
            "current_status": status,
            "pending_count": len(pending_count),
            "status_options": [
                ("pending", "承認待ち"),
                ("approved", "承認済み"),
                ("rejected", "差し戻し"),
                ("all", "すべて"),
            ],
        },
    )


@router.get("/{request_id}", response_class=HTMLResponse)
def child_change_request_detail(
    request: Request,
    request_id: int,
    notice: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_child_record_manager(current_user)
    change_request = _load_change_request(session, request_id)
    child = change_request.child
    current_form_data = merge_child_profile_form_data(child) if child else {}
    notice_message = {
        "approved": "変更申請を承認し、園児情報へ反映しました。",
        "rejected": "変更申請を差し戻しました。",
    }.get(notice or "", "")

    return templates.TemplateResponse(
        request,
        "child_change_requests/detail.html",
        {
            "request": request,
            "current_user": current_user,
            "change_request": change_request,
            "current_form_data": current_form_data,
            "notice": notice_message,
        },
    )


@router.post("/{request_id}/approve")
def approve_child_change_request(
    request_id: int,
    review_note: str = Form(""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_child_record_manager(current_user)
    change_request = _load_change_request(session, request_id)
    if change_request.status != ChildProfileChangeRequestStatus.pending:
        return RedirectResponse(url=f"/child-change-requests/{request_id}", status_code=303)

    child = session.exec(
        select(Child)
        .options(selectinload(Child.guardians), selectinload(Child.family))
        .where(Child.id == change_request.child_id)
    ).first()
    if not child:
        raise HTTPException(status_code=404, detail="園児が見つかりません")

    try:
        apply_child_profile_payload(
            session,
            child,
            change_request.request_data or {},
            applied_at=utc_now(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    change_request.status = ChildProfileChangeRequestStatus.approved
    change_request.review_note = (review_note or "").strip() or None
    change_request.reviewed_at = utc_now()
    change_request.reviewed_by = current_user.name
    change_request.updated_at = utc_now()
    session.add(change_request)
    session.commit()

    return RedirectResponse(url=f"/child-change-requests/{request_id}?notice=approved", status_code=303)


@router.post("/{request_id}/reject")
def reject_child_change_request(
    request_id: int,
    review_note: str = Form(""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_child_record_manager(current_user)
    change_request = _load_change_request(session, request_id)
    if change_request.status != ChildProfileChangeRequestStatus.pending:
        return RedirectResponse(url=f"/child-change-requests/{request_id}", status_code=303)

    change_request.status = ChildProfileChangeRequestStatus.rejected
    change_request.review_note = (review_note or "").strip() or None
    change_request.reviewed_at = utc_now()
    change_request.reviewed_by = current_user.name
    change_request.updated_at = utc_now()
    session.add(change_request)
    session.commit()

    return RedirectResponse(url=f"/child-change-requests/{request_id}?notice=rejected", status_code=303)
