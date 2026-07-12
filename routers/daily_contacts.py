from datetime import date
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from auth import get_current_staff_user
from daily_contact_reply_fields import (
    reply_field_definitions,
    reply_items_for_display,
    reply_values_for_form,
    reply_values_from_mapping,
)
from database import get_session
from models import Child, ChildStatus, Classroom, DailyContactEntry, DailyContactReply, DailyContactReplyStatus
from time_utils import local_today, utc_now

router = APIRouter(prefix="/daily-contacts", tags=["daily_contacts"])
templates = Jinja2Templates(directory="templates")

DAILY_CONTACT_SORT_OPTIONS = {
    "classroom": "クラス・園児順",
    "unsubmitted_first": "未提出を先頭",
    "submitted_first": "提出済みを先頭",
}

DAILY_CONTACT_REPLY_NOTICE_MESSAGES = {
    "reply_saved": "園からの返信を下書き保存しました。",
    "reply_published": "園からの返信を保護者に公開しました。",
}


def _parse_target_date(raw: Optional[str]) -> date:
    if not raw:
        return local_today()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return local_today()


def _normalize_sort(raw: Optional[str]) -> str:
    return raw if raw in DAILY_CONTACT_SORT_OPTIONS else "classroom"


def _parse_optional_int(raw: Optional[str]) -> Optional[int]:
    cleaned = str(raw).strip() if raw is not None else ""
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _child_sort_key(child: Child) -> tuple:
    classroom = child.classroom
    return (
        classroom.display_order if classroom else 999,
        child.classroom_id or 0,
        child.last_name_kana or "",
        child.first_name_kana or "",
        child.id or 0,
    )


def _sort_children_by_contact_status(
    children: list[Child],
    entry_by_child_id: dict[int, DailyContactEntry],
    sort: str,
) -> list[Child]:
    if sort == "unsubmitted_first":
        return sorted(
            children,
            key=lambda child: (
                1 if child.id in entry_by_child_id else 0,
                _child_sort_key(child),
            ),
        )
    if sort == "submitted_first":
        return sorted(
            children,
            key=lambda child: (
                0 if child.id in entry_by_child_id else 1,
                _child_sort_key(child),
            ),
        )
    return sorted(children, key=_child_sort_key)


def _daily_contact_query(day: date, classroom_id: Optional[int], sort: str, **extra: str) -> str:
    params: dict[str, str] = {
        "date": day.isoformat(),
        "sort": _normalize_sort(sort),
    }
    if classroom_id is not None:
        params["classroom_id"] = str(classroom_id)
    params.update({key: value for key, value in extra.items() if value})
    return urlencode(params)


def _load_daily_contact_reply(session: Session, child_id: int, day: date) -> DailyContactReply | None:
    return session.exec(
        select(DailyContactReply).where(
            DailyContactReply.child_id == child_id,
            DailyContactReply.target_date == day,
        )
    ).first()


@router.get("/", response_class=HTMLResponse)
def daily_contact_list(
    request: Request,
    target_date: Optional[str] = Query(default=None, alias="date"),
    classroom_id: Optional[str] = Query(default=None),
    sort: Optional[str] = Query(default="classroom"),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    day = _parse_target_date(target_date)
    selected_classroom_id = _parse_optional_int(classroom_id)
    selected_sort = _normalize_sort(sort)
    classrooms = session.exec(select(Classroom).order_by(Classroom.display_order, Classroom.id)).all()
    children_query = (
        select(Child)
        .options(selectinload(Child.classroom))
        .where(Child.status == ChildStatus.enrolled)
    )
    if selected_classroom_id:
        children_query = children_query.where(Child.classroom_id == selected_classroom_id)
    children = session.exec(children_query).all()
    child_ids = [child.id for child in children if child.id is not None]
    entries = session.exec(
        select(DailyContactEntry)
        .options(selectinload(DailyContactEntry.parent_account), selectinload(DailyContactEntry.child))
        .where(
            DailyContactEntry.target_date == day,
            DailyContactEntry.child_id.in_(child_ids) if child_ids else False,
        )
    ).all() if child_ids else []
    entry_by_child_id = {entry.child_id: entry for entry in entries}
    replies = session.exec(
        select(DailyContactReply).where(
            DailyContactReply.target_date == day,
            DailyContactReply.child_id.in_(child_ids) if child_ids else False,
        )
    ).all() if child_ids else []
    reply_by_child_id = {reply.child_id: reply for reply in replies}
    children = _sort_children_by_contact_status(children, entry_by_child_id, selected_sort)

    return templates.TemplateResponse(
        request,
        "daily_contacts/list.html",
        {
            "request": request,
            "current_user": current_user,
            "target_date": day,
            "target_date_value": day.isoformat(),
            "classrooms": classrooms,
            "selected_classroom_id": selected_classroom_id,
            "selected_sort": selected_sort,
            "sort_options": DAILY_CONTACT_SORT_OPTIONS,
            "detail_query_string": _daily_contact_query(day, selected_classroom_id, selected_sort),
            "children": children,
            "entry_by_child_id": entry_by_child_id,
            "reply_by_child_id": reply_by_child_id,
            "published_reply_status": DailyContactReplyStatus.published,
        },
    )


@router.get("/{child_id}", response_class=HTMLResponse)
def daily_contact_detail(
    request: Request,
    child_id: int,
    target_date: Optional[str] = Query(default=None, alias="date"),
    classroom_id: Optional[str] = Query(default=None),
    sort: Optional[str] = Query(default="classroom"),
    notice: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    day = _parse_target_date(target_date)
    selected_classroom_id = _parse_optional_int(classroom_id)
    selected_sort = _normalize_sort(sort)
    child = session.exec(
        select(Child)
        .options(selectinload(Child.classroom))
        .where(Child.id == child_id)
    ).first()
    if not child:
        raise HTTPException(status_code=404, detail="園児が見つかりません")

    entry = session.exec(
        select(DailyContactEntry)
        .options(selectinload(DailyContactEntry.parent_account))
        .where(
            DailyContactEntry.child_id == child_id,
            DailyContactEntry.target_date == day,
        )
    ).first()
    reply = _load_daily_contact_reply(session, child_id, day)

    return templates.TemplateResponse(
        request,
        "daily_contacts/detail.html",
        {
            "request": request,
            "current_user": current_user,
            "target_date": day,
            "target_date_value": day.isoformat(),
            "selected_classroom_id": selected_classroom_id,
            "selected_sort": selected_sort,
            "list_url": f"/daily-contacts/?{_daily_contact_query(day, selected_classroom_id, selected_sort)}",
            "child": child,
            "entry": entry,
            "reply": reply,
            "reply_fields": reply_field_definitions(),
            "reply_form_values": reply_values_for_form(reply),
            "reply_items": reply_items_for_display(reply),
            "notice": DAILY_CONTACT_REPLY_NOTICE_MESSAGES.get(notice or "", ""),
        },
    )


@router.post("/{child_id}/reply")
def save_daily_contact_reply(
    child_id: int,
    target_date: str = Form(..., alias="date"),
    reply_nap_time: str = Form(""),
    reply_temperature: str = Form(""),
    reply_bowel_movement: str = Form(""),
    reply_appetite: str = Form(""),
    reply_message: str = Form(""),
    classroom_id: str = Form(""),
    sort: str = Form("classroom"),
    action: str = Form("draft"),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    if not getattr(current_user, "can_edit", False):
        raise HTTPException(status_code=403, detail="編集権限がありません")

    day = _parse_target_date(target_date)
    selected_classroom_id = _parse_optional_int(classroom_id)
    selected_sort = _normalize_sort(sort)
    child = session.exec(select(Child).where(Child.id == child_id, Child.status == ChildStatus.enrolled)).first()
    if not child:
        raise HTTPException(status_code=404, detail="園児が見つかりません")

    entry = session.exec(
        select(DailyContactEntry).where(
            DailyContactEntry.child_id == child_id,
            DailyContactEntry.target_date == day,
        )
    ).first()
    if not entry:
        raise HTTPException(status_code=404, detail="返信対象の日次連絡が見つかりません")

    values = reply_values_from_mapping(
        {
            "reply_nap_time": reply_nap_time,
            "reply_temperature": reply_temperature,
            "reply_bowel_movement": reply_bowel_movement,
            "reply_appetite": reply_appetite,
        }
    )
    now = utc_now()
    reply = _load_daily_contact_reply(session, child_id, day)
    if reply is None:
        reply = DailyContactReply(
            child_id=child_id,
            daily_contact_entry_id=entry.id,
            target_date=day,
            created_at=now,
        )

    next_status = DailyContactReplyStatus.published if action == "publish" else DailyContactReplyStatus.draft
    reply.daily_contact_entry_id = entry.id
    reply.status = next_status
    reply.field_values = values
    reply.message = reply_message.strip() or None
    reply.staff_user_id = getattr(current_user, "user_id", None)
    reply.staff_name = getattr(current_user, "name", None)
    reply.published_at = now if next_status == DailyContactReplyStatus.published else None
    reply.updated_at = now
    session.add(reply)
    session.commit()

    notice_key = "reply_published" if next_status == DailyContactReplyStatus.published else "reply_saved"
    return RedirectResponse(
        url=f"/daily-contacts/{child_id}?{_daily_contact_query(day, selected_classroom_id, selected_sort, notice=notice_key)}",
        status_code=303,
    )
