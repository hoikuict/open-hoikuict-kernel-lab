from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from attendance_checks_service import alarm_reason_labels, sync_attendance_alarm
from attendance_checks_service import verification_label as attendance_verification_label
from auth import get_current_staff_user, require_attendance_check_editor
from database import get_session
from models import (
    AttendanceAlarmState,
    AttendanceRecord,
    AttendanceVerification,
    AttendanceVerificationHistory,
    AttendanceVerificationStatus,
    Child,
    ChildStatus,
    Classroom,
    DailyContactEntry,
)
from time_utils import local_today, utc_now

router = APIRouter(prefix="/attendance-checks", tags=["attendance_checks"])
templates = Jinja2Templates(directory="templates")

LAYOUT_OPTIONS = [
    {"value": "flat", "label": "全園児を一覧"},
    {"value": "classroom", "label": "クラス別に表示"},
]

STATUS_FILTER_OPTIONS = [
    {"value": "all", "label": "すべて"},
    {"value": "present", "label": "出席のみ"},
    {"value": "absent", "label": "欠席のみ"},
    {"value": "private_absent", "label": "私用休み"},
    {"value": "sick_absent", "label": "病欠"},
    {"value": "unknown", "label": "未確認"},
    {"value": "alarm", "label": "アラームあり"},
]

VERIFICATION_OPTIONS = [
    {"value": AttendanceVerificationStatus.present.value, "label": "出席"},
    {"value": AttendanceVerificationStatus.private_absent.value, "label": "私用休み"},
    {"value": AttendanceVerificationStatus.sick_absent.value, "label": "病欠"},
    {"value": AttendanceVerificationStatus.unknown.value, "label": "不明"},
]

VALID_LAYOUTS = {option["value"] for option in LAYOUT_OPTIONS}
VALID_FILTERS = {option["value"] for option in STATUS_FILTER_OPTIONS}
VALID_VERIFICATION_KEYS = {option["value"] for option in VERIFICATION_OPTIONS}


@dataclass
class AttendanceCheckRow:
    child: Child
    classroom_name: str
    entry: Optional[DailyContactEntry]
    verification: Optional[AttendanceVerification]
    verification_key: str
    verification_label: str
    verification_updated_at: Optional[datetime]
    verification_updated_by_name: Optional[str]
    has_check_in: bool
    check_in_at: Optional[datetime]
    alarm_is_active: bool
    alarm_reasons: list[str]
    history_items: list[AttendanceVerificationHistory]


def _parse_target_date(raw: Optional[str]) -> date:
    if not raw:
        return local_today()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="日付は YYYY-MM-DD 形式で指定してください") from exc


def _parse_layout(raw: Optional[str]) -> str:
    if raw in VALID_LAYOUTS:
        return raw
    return "flat"


def _parse_filter(raw: Optional[str]) -> str:
    if raw in VALID_FILTERS:
        return raw
    return "all"


def _parse_optional_int(raw: Optional[str]) -> Optional[int]:
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_status(raw: str) -> AttendanceVerificationStatus:
    if raw not in VALID_VERIFICATION_KEYS:
        raise HTTPException(status_code=400, detail="不正な出欠確認ステータスです")
    return AttendanceVerificationStatus(raw)


def _is_hx_request(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _build_redirect_url(
    *,
    target_day: date,
    selected_layout: str,
    selected_filter: str,
    selected_classroom_id: Optional[int],
) -> str:
    params = {
        "date": target_day.isoformat(),
        "layout": selected_layout,
        "filter": selected_filter,
    }
    if selected_classroom_id is not None:
        params["classroom_id"] = str(selected_classroom_id)
    return f"/attendance-checks/?{urlencode(params)}"


def _load_rows(
    session: Session,
    *,
    target_day: date,
    selected_classroom_id: Optional[int],
) -> list[AttendanceCheckRow]:
    children = session.exec(
        select(Child)
        .options(selectinload(Child.classroom))
        .where(Child.status == ChildStatus.enrolled)
    ).all()
    enrolled_children = [
        child
        for child in children
        if child.id is not None and (selected_classroom_id is None or child.classroom_id == selected_classroom_id)
    ]
    enrolled_children.sort(
        key=lambda child: (
            child.classroom.display_order if child.classroom else 999,
            child.classroom.name if child.classroom else "クラス未設定",
            child.last_name_kana,
            child.first_name_kana,
            child.id or 0,
        )
    )

    child_ids = [child.id for child in enrolled_children if child.id is not None]
    if not child_ids:
        return []

    records = session.exec(
        select(AttendanceRecord).where(
            AttendanceRecord.attendance_date == target_day,
            AttendanceRecord.child_id.in_(child_ids),
        )
    ).all()
    entries = session.exec(
        select(DailyContactEntry)
        .options(selectinload(DailyContactEntry.parent_account))
        .where(
            DailyContactEntry.target_date == target_day,
            DailyContactEntry.child_id.in_(child_ids),
        )
    ).all()
    verifications = session.exec(
        select(AttendanceVerification).where(
            AttendanceVerification.target_date == target_day,
            AttendanceVerification.child_id.in_(child_ids),
        )
    ).all()
    histories = session.exec(
        select(AttendanceVerificationHistory)
        .where(
            AttendanceVerificationHistory.target_date == target_day,
            AttendanceVerificationHistory.child_id.in_(child_ids),
        )
        .order_by(AttendanceVerificationHistory.created_at.desc(), AttendanceVerificationHistory.id.desc())
    ).all()

    record_by_child_id = {record.child_id: record for record in records}
    entry_by_child_id = {entry.child_id: entry for entry in entries}
    verification_by_child_id = {verification.child_id: verification for verification in verifications}

    alarm_states = session.exec(
        select(AttendanceAlarmState).where(
            AttendanceAlarmState.target_date == target_day,
            AttendanceAlarmState.child_id.in_(child_ids),
        )
    ).all()
    alarm_by_child_id = {alarm.child_id: alarm for alarm in alarm_states}

    histories_by_child_id: dict[int, list[AttendanceVerificationHistory]] = {}
    for history in histories:
        bucket = histories_by_child_id.setdefault(history.child_id, [])
        if len(bucket) < 5:
            bucket.append(history)

    rows: list[AttendanceCheckRow] = []
    for child in enrolled_children:
        child_id = child.id or 0
        verification = verification_by_child_id.get(child_id)
        alarm_state = alarm_by_child_id.get(child_id)
        record = record_by_child_id.get(child_id)
        status_key = verification.status.value if verification else AttendanceVerificationStatus.unknown.value
        rows.append(
            AttendanceCheckRow(
                child=child,
                classroom_name=child.classroom.name if child.classroom else "クラス未設定",
                entry=entry_by_child_id.get(child_id),
                verification=verification,
                verification_key=status_key,
                verification_label=attendance_verification_label(verification),
                verification_updated_at=verification.updated_at if verification else None,
                verification_updated_by_name=verification.updated_by_name if verification else None,
                has_check_in=bool(record and record.check_in_at is not None),
                check_in_at=record.check_in_at if record else None,
                alarm_is_active=bool(alarm_state and alarm_state.is_active),
                alarm_reasons=alarm_reason_labels(alarm_state.reasons if alarm_state else None),
                history_items=histories_by_child_id.get(child_id, []),
            )
        )
    return rows


def _matches_filter(row: AttendanceCheckRow, selected_filter: str) -> bool:
    if selected_filter == "all":
        return True
    if selected_filter == "alarm":
        return row.alarm_is_active
    if selected_filter == "present":
        return row.verification_key == AttendanceVerificationStatus.present.value
    if selected_filter == "absent":
        return row.verification_key in {
            AttendanceVerificationStatus.private_absent.value,
            AttendanceVerificationStatus.sick_absent.value,
        }
    if selected_filter == "private_absent":
        return row.verification_key == AttendanceVerificationStatus.private_absent.value
    if selected_filter == "sick_absent":
        return row.verification_key == AttendanceVerificationStatus.sick_absent.value
    if selected_filter == "unknown":
        return row.verification_key == AttendanceVerificationStatus.unknown.value
    return True


def _build_counts(rows: list[AttendanceCheckRow]) -> dict[str, int]:
    counts = {
        "present": 0,
        "private_absent": 0,
        "sick_absent": 0,
        "unknown": 0,
        "alarm": 0,
    }
    for row in rows:
        if row.verification_key in counts:
            counts[row.verification_key] += 1
        else:
            counts["unknown"] += 1
        if row.alarm_is_active:
            counts["alarm"] += 1
    return counts


def _group_rows(rows: list[AttendanceCheckRow]) -> list[dict[str, object]]:
    grouped: list[dict[str, object]] = []
    current_classroom: Optional[str] = None
    current_rows: list[AttendanceCheckRow] = []

    for row in rows:
        if current_classroom != row.classroom_name:
            if current_rows:
                grouped.append({"classroom_name": current_classroom, "rows": current_rows})
            current_classroom = row.classroom_name
            current_rows = [row]
        else:
            current_rows.append(row)

    if current_rows:
        grouped.append({"classroom_name": current_classroom, "rows": current_rows})
    return grouped


def _build_page_context(
    *,
    request: Request,
    session: Session,
    current_user,
    target_day: date,
    selected_layout: str,
    selected_filter: str,
    selected_classroom_id: Optional[int],
) -> dict[str, object]:
    classrooms = session.exec(select(Classroom).order_by(Classroom.display_order, Classroom.id)).all()
    scope_rows = _load_rows(
        session,
        target_day=target_day,
        selected_classroom_id=selected_classroom_id,
    )
    display_rows = [row for row in scope_rows if _matches_filter(row, selected_filter)]
    return {
        "request": request,
        "current_user": current_user,
        "target_date_value": target_day.isoformat(),
        "selected_layout": selected_layout,
        "selected_filter": selected_filter,
        "selected_classroom_id": selected_classroom_id,
        "selected_classroom_id_value": str(selected_classroom_id or ""),
        "layout_options": LAYOUT_OPTIONS,
        "status_filter_options": STATUS_FILTER_OPTIONS,
        "verification_options": VERIFICATION_OPTIONS,
        "classrooms": classrooms,
        "counts": _build_counts(scope_rows),
        "rows": display_rows,
        "grouped_rows": _group_rows(display_rows) if selected_layout == "classroom" else [],
    }


@router.get("/", response_class=HTMLResponse)
def attendance_checks_list(
    request: Request,
    target_date: Optional[str] = Query(default=None, alias="date"),
    layout: Optional[str] = Query(default="flat"),
    status_filter: Optional[str] = Query(default="all", alias="filter"),
    classroom_id: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    target_day = _parse_target_date(target_date)
    selected_layout = _parse_layout(layout)
    selected_filter = _parse_filter(status_filter)
    selected_classroom_id = _parse_optional_int(classroom_id)
    context = _build_page_context(
        request=request,
        session=session,
        current_user=current_user,
        target_day=target_day,
        selected_layout=selected_layout,
        selected_filter=selected_filter,
        selected_classroom_id=selected_classroom_id,
    )
    template_name = "attendance_checks/_board.html" if _is_hx_request(request) else "attendance_checks/list.html"
    return templates.TemplateResponse(request, template_name, context)


@router.post("/{child_id}/verification", response_class=HTMLResponse)
def update_attendance_verification(
    request: Request,
    child_id: int,
    target_date: str = Form(..., alias="date"),
    status: str = Form(...),
    layout: str = Form(default="flat"),
    status_filter: str = Form(default="all", alias="filter"),
    classroom_id: str = Form(default=""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_attendance_check_editor(current_user)

    child = session.get(Child, child_id)
    if not child:
        raise HTTPException(status_code=404, detail="園児が見つかりません")
    if child.status != ChildStatus.enrolled:
        raise HTTPException(status_code=400, detail="在籍中の園児のみ更新できます")

    target_day = _parse_target_date(target_date)
    selected_layout = _parse_layout(layout)
    selected_filter = _parse_filter(status_filter)
    selected_classroom_id = _parse_optional_int(classroom_id)
    next_status = _parse_status(status)
    now = utc_now()

    verification = session.exec(
        select(AttendanceVerification).where(
            AttendanceVerification.child_id == child_id,
            AttendanceVerification.target_date == target_day,
        )
    ).first()
    if not verification:
        verification = AttendanceVerification(
            child_id=child_id,
            target_date=target_day,
            status=next_status,
            updated_by_name=current_user.name,
            created_at=now,
            updated_at=now,
        )
    else:
        verification.status = next_status
        verification.updated_by_name = current_user.name
        verification.updated_at = now

    session.add(verification)
    session.add(
        AttendanceVerificationHistory(
            child_id=child_id,
            target_date=target_day,
            status=next_status,
            updated_by_name=current_user.name,
            created_at=now,
        )
    )
    sync_attendance_alarm(
        session,
        child_id=child_id,
        target_date=target_day,
        verification=verification,
        now=now,
    )
    session.commit()

    if _is_hx_request(request):
        context = _build_page_context(
            request=request,
            session=session,
            current_user=current_user,
            target_day=target_day,
            selected_layout=selected_layout,
            selected_filter=selected_filter,
            selected_classroom_id=selected_classroom_id,
        )
        return templates.TemplateResponse(request, "attendance_checks/_board.html", context)

    return RedirectResponse(
        url=_build_redirect_url(
            target_day=target_day,
            selected_layout=selected_layout,
            selected_filter=selected_filter,
            selected_classroom_id=selected_classroom_id,
        ),
        status_code=303,
    )
