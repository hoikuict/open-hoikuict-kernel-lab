from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from auth import (
    Role,
    clear_staff_cookies,
    get_current_staff_user_id,
    require_mock_staff_auth,
    resolve_staff_principal,
    set_staff_cookies,
)
from calendar_service import (
    CalendarContext,
    EventOccurrence,
    active_reminders_for_event,
    combine_local_date,
    default_create_context,
    ensure_calendar_user_preferences,
    find_occurrence,
    format_date_local,
    format_datetime_local,
    list_calendar_contexts,
    list_occurrences,
    local_today,
    localize_datetime,
    normalize_utc,
    parse_iso_date,
    parse_iso_datetime,
    rebuild_notification_jobs_for_event,
    search_occurrences,
    shift_anchor_date,
    split_csv_numbers,
    sync_event_reminders,
    to_utc_from_local,
    update_default_calendar_if_needed,
    view_window_dates,
    view_window_utc,
    get_calendar_context,
)
from database import get_session
from models import (
    Calendar,
    CalendarActivityKind,
    CalendarActivityLog,
    CalendarMember,
    CalendarMemberRole,
    CalendarType,
    CalendarUserPreference,
    Event,
    EventKind,
    EventLifecycleStatus,
    EventOverride,
    EventVisibility,
    NotificationJob,
    RecurrenceFrequency,
    RecurrenceRule,
    Reminder,
    User,
)
from time_utils import utc_now
from url_utils import safe_internal_redirect
from security_config import websocket_origin_allowed

router = APIRouter(tags=["calendar"])
mock_login_router = APIRouter(tags=["calendar-mock"])
templates = Jinja2Templates(directory="templates")
VALID_VIEW_MODES = {"month", "week", "day"}
WEEKDAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"]
VIEW_MODE_LABELS = {
    "month": "月",
    "week": "週",
    "day": "日",
}
DEFAULT_CALENDAR_COLOR = "#2563EB"
CALENDAR_COLOR_OPTIONS = [
    {"value": "#2563EB", "label": "ブルー"},
    {"value": "#059669", "label": "グリーン"},
    {"value": "#DC2626", "label": "レッド"},
    {"value": "#D97706", "label": "オレンジ"},
    {"value": "#7C3AED", "label": "バイオレット"},
    {"value": "#DB2777", "label": "ピンク"},
    {"value": "#0891B2", "label": "シアン"},
    {"value": "#4B5563", "label": "グレー"},
]
CALENDAR_COLOR_VALUES = {item["value"] for item in CALENDAR_COLOR_OPTIONS}


class CalendarSocketManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, calendar_id: UUID, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[str(calendar_id)].add(websocket)

    async def disconnect(self, calendar_id: UUID, websocket: WebSocket) -> None:
        async with self._lock:
            bucket = self._connections.get(str(calendar_id))
            if not bucket:
                return
            bucket.discard(websocket)
            if not bucket:
                self._connections.pop(str(calendar_id), None)

    async def broadcast(self, calendar_ids: set[UUID], payload: dict[str, Any]) -> None:
        targets: list[tuple[str, WebSocket]] = []
        async with self._lock:
            for calendar_id in calendar_ids:
                for websocket in self._connections.get(str(calendar_id), set()):
                    targets.append((str(calendar_id), websocket))
        dead: list[tuple[str, WebSocket]] = []
        for calendar_id, websocket in targets:
            try:
                await websocket.send_json(payload)
            except Exception:
                dead.append((calendar_id, websocket))
        if not dead:
            return
        async with self._lock:
            for calendar_id, websocket in dead:
                bucket = self._connections.get(calendar_id)
                if not bucket:
                    continue
                bucket.discard(websocket)
                if not bucket:
                    self._connections.pop(calendar_id, None)


socket_manager = CalendarSocketManager()


def _coerce_uuid(raw_value: str | None) -> UUID | None:
    if not raw_value:
        return None
    try:
        return UUID(str(raw_value).strip())
    except (TypeError, ValueError):
        return None


def _current_calendar_user(session: Session, request: Request) -> User | None:
    user_id = get_current_staff_user_id(request)
    if user_id is None:
        return None
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


def _require_calendar_user(session: Session, request: Request) -> User:
    user = _current_calendar_user(session, request)
    if user is None:
        raise HTTPException(status_code=401, detail="モックログインが必要です。")
    return user


def _mock_login_redirect(request: Request, fallback: str = "/calendar") -> RedirectResponse:
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    encoded = quote(target or fallback, safe="/?:=&")
    return RedirectResponse(url=f"/staff/login?redirect={encoded}", status_code=303)


def _valid_mode(raw_mode: str | None) -> str:
    return raw_mode if raw_mode in VALID_VIEW_MODES else "month"


def _calendar_type_from_form(raw_value: str | None) -> CalendarType:
    if raw_value == CalendarType.facility_shared.value:
        return CalendarType.facility_shared
    return CalendarType.staff_personal


def _normalize_calendar_color(raw_value: str | None) -> str:
    normalized = (raw_value or DEFAULT_CALENDAR_COLOR).strip().upper()
    return normalized if normalized in CALENDAR_COLOR_VALUES else DEFAULT_CALENDAR_COLOR


def _calendar_display_order(calendar_type: CalendarType) -> int:
    return 30 if calendar_type == CalendarType.facility_shared else 10


def _day_heading(value: date) -> str:
    return f"{value.year}年{value.month}月{value.day}日（{WEEKDAY_LABELS[value.weekday()]}）"


def _range_heading(start_value: date, end_value: date) -> str:
    return f"{start_value.year}年{start_value.month}月{start_value.day}日からの週"


def _ensure_calendar_member(
    session: Session,
    *,
    calendar: Calendar,
    target_user: User,
    role: CalendarMemberRole,
) -> CalendarMember:
    member = session.exec(
        select(CalendarMember).where(
            CalendarMember.calendar_id == calendar.id,
            CalendarMember.user_id == target_user.id,
        )
    ).first()
    if member is None:
        member = CalendarMember(
            calendar_id=calendar.id,
            user_id=target_user.id,
            role=role,
        )
    else:
        member.role = role
        member.updated_at = utc_now()
    session.add(member)
    session.flush()
    return member


def _ensure_facility_shared_memberships(session: Session, calendar: Calendar) -> set[UUID]:
    active_users = session.exec(
        select(User)
        .where(User.is_active.is_(True), User.staff_sort_order < 200)
        .order_by(User.staff_sort_order, User.display_name, User.email)
    ).all()
    broadcast_ids = {calendar.id}
    for user in active_users:
        role = CalendarMemberRole.owner if user.id == calendar.owner_user_id else (
            CalendarMemberRole.editor if user.can_edit_calendar else CalendarMemberRole.viewer
        )
        _ensure_calendar_member(session, calendar=calendar, target_user=user, role=role)
        ensure_calendar_user_preferences(
            session,
            calendar_id=calendar.id,
            user_id=user.id,
            is_visible=True,
            display_order=_calendar_display_order(CalendarType.facility_shared),
        )
    return broadcast_ids


def _can_manage_calendar_settings(user: User, context: CalendarContext) -> bool:
    if context.calendar.calendar_type == CalendarType.facility_shared and user.is_calendar_admin:
        return True
    return context.can_manage_sharing


def _can_delete_calendar(user: User, context: CalendarContext) -> bool:
    if context.calendar.calendar_type == CalendarType.facility_shared:
        return user.is_calendar_admin
    return context.calendar.owner_user_id == user.id


def _loggable_event_label(title: str, visibility: EventVisibility) -> str:
    if visibility == EventVisibility.private:
        return "非公開予定"
    return f"「{(title or '名称未設定の予定').strip() or '名称未設定の予定'}」"


def _record_shared_calendar_log(
    session: Session,
    *,
    calendar: Calendar,
    actor: User,
    action: CalendarActivityKind,
    summary: str,
) -> None:
    if calendar.calendar_type != CalendarType.facility_shared:
        return
    session.add(
        CalendarActivityLog(
            calendar_id=calendar.id,
            actor_user_id=actor.id,
            actor_name=actor.display_name,
            action=action,
            summary=summary,
        )
    )


def _shared_logs_by_calendar(session: Session, contexts: list[CalendarContext], *, include_for_admin: bool) -> dict[UUID, list[CalendarActivityLog]]:
    if not include_for_admin:
        return {}
    shared_ids = [item.calendar.id for item in contexts if item.is_facility_shared]
    if not shared_ids:
        return {}
    logs = session.exec(
        select(CalendarActivityLog)
        .where(CalendarActivityLog.calendar_id.in_(shared_ids))
        .order_by(CalendarActivityLog.created_at.desc())
    ).all()
    grouped: dict[UUID, list[CalendarActivityLog]] = {}
    for log in logs:
        bucket = grouped.setdefault(log.calendar_id, [])
        if len(bucket) >= 12:
            continue
        bucket.append(log)
    return grouped


def _delete_calendar_records(session: Session, calendar: Calendar) -> None:
    event_rows = session.exec(select(Event).where(Event.calendar_id == calendar.id)).all()
    event_ids = [item.id for item in event_rows]
    recurrence_rule_ids = {item.recurrence_rule_id for item in event_rows if item.recurrence_rule_id}

    if event_ids:
        for job in session.exec(select(NotificationJob).where(NotificationJob.source_event_id.in_(event_ids))).all():
            session.delete(job)
        for reminder in session.exec(select(Reminder).where(Reminder.event_id.in_(event_ids))).all():
            session.delete(reminder)
        for override in session.exec(select(EventOverride).where(EventOverride.series_event_id.in_(event_ids))).all():
            session.delete(override)
        for event in event_rows:
            session.delete(event)

    for rule_id in recurrence_rule_ids:
        remaining = session.exec(
            select(Event).where(
                Event.recurrence_rule_id == rule_id,
                Event.calendar_id != calendar.id,
            )
        ).first()
        if remaining is not None:
            continue
        rule = session.get(RecurrenceRule, rule_id)
        if rule is not None:
            session.delete(rule)

    for preference in session.exec(select(CalendarUserPreference).where(CalendarUserPreference.calendar_id == calendar.id)).all():
        session.delete(preference)
    for member in session.exec(select(CalendarMember).where(CalendarMember.calendar_id == calendar.id)).all():
        session.delete(member)
    for log in session.exec(select(CalendarActivityLog).where(CalendarActivityLog.calendar_id == calendar.id)).all():
        session.delete(log)
    session.delete(calendar)


def _anchor_date(raw_date: str | None, user: User) -> date:
    return parse_iso_date(raw_date) or local_today(user.timezone)


def _visible_calendar_ids(contexts: list[CalendarContext]) -> set[UUID]:
    visible = {item.calendar.id for item in contexts if item.is_visible and not item.calendar.is_archived}
    if visible:
        return visible
    fallback = next((item.calendar.id for item in contexts if not item.calendar.is_archived), None)
    return {fallback} if fallback else set()


def _calendar_state(
    session: Session,
    user: User,
    *,
    mode: str,
    anchor: date,
) -> dict[str, Any]:
    contexts = list_calendar_contexts(session, user.id, include_archived=True)
    update_default_calendar_if_needed(session, user, contexts)
    contexts = list_calendar_contexts(session, user.id, include_archived=True)
    selected_calendar_ids = _visible_calendar_ids(contexts)
    window_start, window_end = view_window_utc(mode, anchor, user.timezone)
    occurrences = list_occurrences(
        session,
        contexts,
        user,
        window_start,
        window_end,
        calendar_ids=selected_calendar_ids,
    )
    return {
        "contexts": contexts,
        "selected_calendar_ids": selected_calendar_ids,
        "window_start": window_start,
        "window_end": window_end,
        "occurrences": occurrences,
        "default_create_context": default_create_context(contexts, user),
        "mode": mode,
        "anchor": anchor,
    }


def _day_occurrences_map(
    occurrences: list[EventOccurrence],
    *,
    timezone_name: str,
    start_date: date,
    end_date: date,
) -> dict[date, list[dict[str, Any]]]:
    result: dict[date, list[dict[str, Any]]] = {
        start_date + timedelta(days=offset): [] for offset in range((end_date - start_date).days)
    }
    for occurrence in occurrences:
        for day_value in list(result.keys()):
            day_start = combine_local_date(day_value, timezone_name)
            day_end = combine_local_date(day_value + timedelta(days=1), timezone_name)
            if not (occurrence.start_at < day_end and occurrence.end_at > day_start):
                continue
            local_start = localize_datetime(occurrence.start_at, timezone_name)
            local_end = localize_datetime(occurrence.end_at, timezone_name)
            result[day_value].append(
                {
                    "source_event_id": str(occurrence.source_event_id),
                    "original_start_at": occurrence.original_start_at.isoformat(),
                    "title": occurrence.display_title,
                    "time_label": "終日"
                    if occurrence.is_all_day
                    else f"{local_start:%H:%M}-{local_end:%H:%M}",
                    "is_all_day": occurrence.is_all_day,
                    "color": occurrence.calendar.color,
                    "can_view_details": occurrence.can_view_details,
                    "can_edit": occurrence.can_edit,
                    "can_delete": occurrence.can_delete,
                    "status": occurrence.status.value,
                    "local_start": local_start,
                    "local_end": local_end,
                    "description": occurrence.description if occurrence.can_view_details else None,
                    "location": occurrence.location if occurrence.can_view_details else None,
                    "calendar_name": occurrence.calendar.name,
                }
            )
    for items in result.values():
        items.sort(key=lambda item: (item["is_all_day"] is False, item["local_start"], item["title"].lower()))
    return result


def _main_fragment_context(
    session: Session,
    user: User,
    *,
    mode: str,
    anchor: date,
) -> dict[str, Any]:
    state = _calendar_state(session, user, mode=mode, anchor=anchor)
    start_date, end_date = view_window_dates(mode, anchor)
    day_map = _day_occurrences_map(
        state["occurrences"],
        timezone_name=user.timezone,
        start_date=start_date,
        end_date=end_date,
    )
    dates = [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days)]
    month_weeks = [dates[index : index + 7] for index in range(0, len(dates), 7)]
    return {
        **state,
        "dates": dates,
        "month_weeks": month_weeks,
        "day_map": day_map,
        "prev_anchor": shift_anchor_date(mode, anchor, -1),
        "next_anchor": shift_anchor_date(mode, anchor, 1),
        "today": local_today(user.timezone),
        "hours": list(range(24)),
        "week_dates": dates if mode in {"week", "day"} else [],
        "start_date": start_date,
        "end_date": end_date,
        "weekday_labels": WEEKDAY_LABELS,
        "view_mode_labels": VIEW_MODE_LABELS,
        "month_title": f"{anchor.year}年{anchor.month}月",
        "week_title": _range_heading(start_date, end_date),
        "day_title": _day_heading(anchor),
    }


def _sidebar_context(
    session: Session,
    user: User,
    *,
    mode: str,
    anchor: date,
    toast_message: str = "",
) -> dict[str, Any]:
    state = _calendar_state(session, user, mode=mode, anchor=anchor)
    active_contexts = [item for item in state["contexts"] if not item.calendar.is_archived]
    personal_contexts = [item for item in active_contexts if item.is_staff_personal]
    shared_contexts = [item for item in active_contexts if item.is_facility_shared]
    owned_contexts = [item for item in state["contexts"] if item.membership.role == CalendarMemberRole.owner]
    shared_management_contexts = [item for item in state["contexts"] if item.is_facility_shared] if user.is_calendar_admin else [
        item for item in owned_contexts if item.is_facility_shared
    ]
    manageable_contexts = [item for item in owned_contexts if item.is_staff_personal] + shared_management_contexts
    return {
        **state,
        "personal_contexts": personal_contexts,
        "shared_contexts": shared_contexts,
        "owned_personal_contexts": [item for item in owned_contexts if item.is_staff_personal],
        "owned_shared_contexts": [item for item in owned_contexts if item.is_facility_shared],
        "shared_management_contexts": shared_management_contexts,
        "manageable_active_contexts": [item for item in manageable_contexts if not item.calendar.is_archived],
        "manageable_archived_contexts": [item for item in manageable_contexts if item.calendar.is_archived],
        "shared_logs_by_calendar": _shared_logs_by_calendar(session, state["contexts"], include_for_admin=user.is_calendar_admin),
        "can_create_calendars": user.is_calendar_admin,
        "calendar_role_label": user.calendar_role_label,
        "calendar_color_options": CALENDAR_COLOR_OPTIONS,
        "default_calendar_color": DEFAULT_CALENDAR_COLOR,
        "toast_message": toast_message,
    }


def _bundle_response(
    request: Request,
    session: Session,
    user: User,
    *,
    mode: str,
    anchor: date,
    toast_message: str = "",
    ) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "calendar/_bundle.html",
        {
            "request": request,
            "current_user": user,
            "main": _main_fragment_context(session, user, mode=mode, anchor=anchor),
            "sidebar": _sidebar_context(session, user, mode=mode, anchor=anchor, toast_message=toast_message),
            "format_datetime_local": format_datetime_local,
            "toast_message": toast_message,
        },
    )


def _shell_response(
    request: Request,
    session: Session,
    user: User,
    *,
    mode: str,
    anchor: date,
    toast_message: str = "",
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "calendar/_shell.html",
        {
            "request": request,
            "current_user": user,
            "main": _main_fragment_context(session, user, mode=mode, anchor=anchor),
            "sidebar": _sidebar_context(session, user, mode=mode, anchor=anchor, toast_message=toast_message),
            "format_datetime_local": format_datetime_local,
            "toast_message": toast_message,
        },
    )


def _apply_hx_triggers(
    response: HTMLResponse | RedirectResponse | JSONResponse,
    *,
    triggers: dict[str, Any] | None = None,
) -> HTMLResponse | RedirectResponse | JSONResponse:
    if not triggers:
        return response
    response.headers["HX-Trigger"] = json.dumps(triggers, ensure_ascii=False)
    return response


def _redirect_or_bundle(
    request: Request,
    session: Session,
    user: User,
    *,
    mode: str,
    anchor: date,
    toast_message: str = "",
    redirect_to: str = "/calendar",
    triggers: dict[str, Any] | None = None,
) -> HTMLResponse | RedirectResponse:
    if request.headers.get("HX-Request") == "true":
        if request.headers.get("HX-Target") == "event-modal":
            response = _bundle_response(request, session, user, mode=mode, anchor=anchor, toast_message=toast_message)
        else:
            response = _shell_response(request, session, user, mode=mode, anchor=anchor, toast_message=toast_message)
        return _apply_hx_triggers(response, triggers=triggers)
    return RedirectResponse(url=redirect_to, status_code=303)


def _parse_local_datetime(value: str | None) -> datetime | None:
    if not value or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _event_times_from_form(
    *,
    start_value: str | None,
    end_value: str | None,
    timezone_name: str,
    is_all_day: bool,
) -> tuple[datetime, datetime]:
    start_local = _parse_local_datetime(start_value)
    end_local = _parse_local_datetime(end_value)
    if start_local is None or end_local is None:
        raise ValueError("開始日時と終了日時を入力してください。")
    if is_all_day:
        start_at = combine_local_date(start_local.date(), timezone_name)
        end_at = combine_local_date(end_local.date() + timedelta(days=1), timezone_name)
    else:
        start_at = to_utc_from_local(start_local, timezone_name)
        end_at = to_utc_from_local(end_local, timezone_name)
    if end_at <= start_at:
        raise ValueError("終了日時は開始日時より後にしてください。")
    return start_at, end_at


def _recurrence_rule_from_form(
    session: Session,
    *,
    existing: RecurrenceRule | None,
    recurrence_mode: str,
    recurrence_interval: int,
    recurrence_by_weekday: str,
    recurrence_by_month_day: str,
    recurrence_count: str,
    recurrence_until: str,
    timezone_name: str,
) -> RecurrenceRule | None:
    if recurrence_mode not in {item.value for item in RecurrenceFrequency}:
        return None

    until_date = parse_iso_date(recurrence_until)
    until_at = None
    if until_date:
        until_at = combine_local_date(until_date + timedelta(days=1), timezone_name) - timedelta(seconds=1)

    count_value = None
    if recurrence_count and recurrence_count.strip():
        try:
            count_value = max(int(recurrence_count.strip()), 1)
        except ValueError:
            count_value = None

    rule = existing or RecurrenceRule()
    rule.freq = RecurrenceFrequency(recurrence_mode)
    rule.interval = max(recurrence_interval, 1)
    rule.by_weekday = recurrence_by_weekday.strip() or None
    rule.by_month_day = recurrence_by_month_day.strip() or None
    rule.count = count_value
    rule.until_at = until_at
    rule.timezone = timezone_name
    rule.updated_at = utc_now()
    session.add(rule)
    session.flush()
    return rule


def _scope_value(event: Event, scope: str | None) -> str:
    if event.kind != EventKind.series_master:
        return "single"
    if scope in {"one", "following", "all"}:
        return scope
    return "all"


def _serialize_reminders(reminders: list[int]) -> str:
    return ", ".join(str(item) for item in reminders)


def _event_form_defaults(
    session: Session,
    user: User,
    *,
    source_context: CalendarContext | None,
    occurrence: EventOccurrence | None = None,
    event: Event | None = None,
) -> dict[str, Any]:
    context = source_context
    timezone_name = (
        occurrence.timezone if occurrence else event.timezone if event else user.timezone
    ) if (occurrence or event) else user.timezone
    if occurrence:
        start_at = occurrence.start_at
        end_at = occurrence.end_at
        title = occurrence.title
        description = occurrence.description or ""
        location = occurrence.location or ""
        visibility = occurrence.visibility.value
        is_all_day = occurrence.is_all_day
    elif event:
        start_at = event.start_at
        end_at = event.end_at
        title = event.title
        description = event.description or ""
        location = event.location or ""
        visibility = event.visibility.value
        is_all_day = event.is_all_day
    else:
        local_now = localize_datetime(utc_now(), user.timezone).replace(minute=0, second=0, microsecond=0)
        local_end = local_now + timedelta(hours=1)
        start_at = to_utc_from_local(local_now.replace(tzinfo=None), user.timezone)
        end_at = to_utc_from_local(local_end.replace(tzinfo=None), user.timezone)
        title = ""
        description = ""
        location = ""
        visibility = EventVisibility.normal.value
        is_all_day = False

    reminder_values = []
    if event is not None:
        reminder_values = [
            item.minutes_before
            for item in active_reminders_for_event(session, event.id)
            if item.user_id == user.id
        ]

    return {
        "calendar_id": str(context.calendar.id) if context else (str(event.calendar_id) if event else ""),
        "title": title,
        "description": description,
        "location": location,
        "timezone": timezone_name,
        "visibility": visibility,
        "is_all_day": is_all_day,
        "start_value": format_datetime_local(start_at, timezone_name, "%Y-%m-%dT%H:%M"),
        "end_value": format_datetime_local(end_at, timezone_name, "%Y-%m-%dT%H:%M"),
        "reminders": _serialize_reminders(reminder_values),
    }


def _event_form_context(
    request: Request,
    session: Session,
    user: User,
    *,
    event: Event | None,
    occurrence: EventOccurrence | None,
    calendar_context: CalendarContext | None,
    anchor: date,
    mode: str,
    action_url: str,
    submit_label: str,
    form_error: str = "",
    form_values: dict[str, Any] | None = None,
) -> HTMLResponse:
    contexts = list_calendar_contexts(session, user.id, include_archived=False)
    editable_contexts = [item for item in contexts if item.can_create_events]
    editable_personal_contexts = [item for item in editable_contexts if item.is_staff_personal]
    editable_shared_contexts = [item for item in editable_contexts if item.is_facility_shared]
    defaults = form_values or _event_form_defaults(
        session,
        user,
        source_context=calendar_context,
        occurrence=occurrence,
        event=event,
    )
    recurrence_rule = session.get(RecurrenceRule, event.recurrence_rule_id) if event and event.recurrence_rule_id else None
    recurrence_mode = recurrence_rule.freq.value if recurrence_rule else ""
    recurrence_interval = recurrence_rule.interval if recurrence_rule else 1
    recurrence_by_weekday = recurrence_rule.by_weekday if recurrence_rule and recurrence_rule.by_weekday else ""
    recurrence_by_month_day = recurrence_rule.by_month_day if recurrence_rule and recurrence_rule.by_month_day else ""
    recurrence_count = recurrence_rule.count if recurrence_rule and recurrence_rule.count is not None else ""
    recurrence_until = (
        format_date_local(recurrence_rule.until_at, recurrence_rule.timezone, "%Y-%m-%d")
        if recurrence_rule and recurrence_rule.until_at
        else ""
    )

    return templates.TemplateResponse(
        request,
        "calendar/_event_modal.html",
        {
            "request": request,
            "current_user": user,
            "editable_contexts": editable_contexts,
            "editable_personal_contexts": editable_personal_contexts,
            "editable_shared_contexts": editable_shared_contexts,
            "event": event,
            "occurrence": occurrence,
            "action_url": action_url,
            "submit_label": submit_label,
            "form_error": form_error,
            "form_values": defaults,
            "recurrence_mode": recurrence_mode,
            "recurrence_interval": recurrence_interval,
            "recurrence_by_weekday": recurrence_by_weekday,
            "recurrence_by_month_day": recurrence_by_month_day,
            "recurrence_count": recurrence_count,
            "recurrence_until": recurrence_until,
            "scope_default": "one" if occurrence and event and event.kind == EventKind.series_master else "all",
            "view_mode": mode,
            "anchor_date": anchor.isoformat(),
        },
    )


def _load_event(session: Session, event_id: UUID) -> Event:
    event = session.get(Event, event_id)
    if event is None or event.is_deleted:
        raise HTTPException(status_code=404, detail="予定が見つかりません。")
    return event


def _ensure_owner_membership(calendar: Calendar, member: CalendarMember) -> None:
    if member.role == CalendarMemberRole.owner and member.user_id != calendar.owner_user_id:
        raise HTTPException(status_code=400, detail="管理者情報が一致していません。")


def _broadcast_payload(user: User, *, mode: str, anchor: date) -> dict[str, Any]:
    return {
        "type": "calendar-updated",
        "mode": mode,
        "anchor_date": anchor.isoformat(),
        "user_id": str(user.id),
    }


@mock_login_router.get(
    "/mock-login",
    response_class=HTMLResponse,
    dependencies=[Depends(require_mock_staff_auth)],
)
def mock_login_page(
    request: Request,
    redirect: str = "/calendar",
    session: Session = Depends(get_session),
):
    target = safe_internal_redirect(redirect, "/calendar")
    return RedirectResponse(url=f"/staff/login?redirect={quote(target, safe='/?:=&')}", status_code=303)


@mock_login_router.post(
    "/session/mock-login",
    dependencies=[Depends(require_mock_staff_auth)],
)
def mock_login(
    user_id: str = Form(...),
    redirect_to: str = Form("/calendar"),
    session: Session = Depends(get_session),
):
    user = session.get(User, _coerce_uuid(user_id) or UUID(int=0))
    if user is None or not user.is_active or user.staff_sort_order >= 200:
        return RedirectResponse(url="/staff/login", status_code=303)
    response = RedirectResponse(url=safe_internal_redirect(redirect_to, "/calendar"), status_code=303)
    role = Role.ADMIN if user.staff_role == "admin" else Role.CAN_EDIT if user.staff_role == "can_edit" else Role.VIEW_ONLY
    set_staff_cookies(response, role=role, name=user.display_name, user_id=str(user.id))
    return response


@mock_login_router.post("/session/logout")
def mock_logout():
    response = RedirectResponse(url="/staff/login", status_code=303)
    clear_staff_cookies(response)
    return response


@router.get("/calendar", response_class=HTMLResponse)
def calendar_index(
    request: Request,
    mode: str = Query(default="month"),
    date_value: str | None = Query(default=None, alias="date"),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    valid_mode = _valid_mode(mode)
    anchor = _anchor_date(date_value, user)
    return templates.TemplateResponse(
        request,
        "calendar/index.html",
        {
            "request": request,
            "current_user": user,
            "main": _main_fragment_context(session, user, mode=valid_mode, anchor=anchor),
            "sidebar": _sidebar_context(session, user, mode=valid_mode, anchor=anchor),
            "format_datetime_local": format_datetime_local,
        },
    )


@router.get("/calendar/shell", response_class=HTMLResponse)
def calendar_shell(
    request: Request,
    mode: str = Query(default="month"),
    date_value: str | None = Query(default=None, alias="date"),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    valid_mode = _valid_mode(mode)
    anchor = _anchor_date(date_value, user)
    return _shell_response(request, session, user, mode=valid_mode, anchor=anchor)


@router.get("/calendar/view", response_class=HTMLResponse)
def calendar_view(
    request: Request,
    mode: str = Query(default="month"),
    date_value: str | None = Query(default=None, alias="date"),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    valid_mode = _valid_mode(mode)
    anchor = _anchor_date(date_value, user)
    return templates.TemplateResponse(
        request,
        "calendar/_calendar_main.html",
        {
            "request": request,
            "current_user": user,
            **_main_fragment_context(session, user, mode=valid_mode, anchor=anchor),
        },
    )


@router.post("/calendar/preferences/{calendar_id}/visibility")
def toggle_calendar_visibility(
    request: Request,
    calendar_id: str,
    is_visible: bool = Form(False),
    mode: str = Form("month"),
    anchor_date: str = Form(""),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)

    calendar_uuid = _coerce_uuid(calendar_id)
    context = get_calendar_context(session, user.id, calendar_uuid, include_archived=True) if calendar_uuid else None
    if context is None:
        raise HTTPException(status_code=404, detail="カレンダーが見つかりません。")
    preference = ensure_calendar_user_preferences(
        session,
        calendar_id=context.calendar.id,
        user_id=user.id,
        is_visible=True,
        display_order=context.display_order or 0,
    )
    preference.is_visible = is_visible
    preference.updated_at = utc_now()
    session.add(preference)
    session.commit()
    return _redirect_or_bundle(
        request,
        session,
        user,
        mode=_valid_mode(mode),
        anchor=_anchor_date(anchor_date, user),
        toast_message="表示設定を更新しました。",
    )


@router.post("/calendars")
async def create_calendar(
    request: Request,
    name: str = Form(...),
    calendar_type: str = Form(CalendarType.staff_personal.value),
    color: str = Form(DEFAULT_CALENDAR_COLOR),
    description: str = Form(""),
    mode: str = Form("month"),
    anchor_date: str = Form(""),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    if not user.is_calendar_admin:
        raise HTTPException(status_code=403, detail="カレンダーを作成できるのは管理者のみです。")

    normalized_calendar_type = _calendar_type_from_form(calendar_type)
    calendar = Calendar(
        owner_user_id=user.id,
        name=name.strip() or "名称未設定のカレンダー",
        calendar_type=normalized_calendar_type,
        color=_normalize_calendar_color(color),
        description=description.strip() or None,
        is_primary=False,
        is_archived=False,
    )
    session.add(calendar)
    session.flush()
    _ensure_calendar_member(session, calendar=calendar, target_user=user, role=CalendarMemberRole.owner)
    broadcast_calendar_ids = {calendar.id}
    if normalized_calendar_type == CalendarType.facility_shared:
        broadcast_calendar_ids = _ensure_facility_shared_memberships(session, calendar)
    else:
        ensure_calendar_user_preferences(
            session,
            calendar_id=calendar.id,
            user_id=user.id,
            is_visible=True,
            display_order=_calendar_display_order(normalized_calendar_type),
        )
    if user.default_calendar_id is None and normalized_calendar_type == CalendarType.staff_personal:
        user.default_calendar_id = calendar.id
        user.updated_at = utc_now()
        session.add(user)
    _record_shared_calendar_log(
        session,
        calendar=calendar,
        actor=user,
        action=CalendarActivityKind.calendar_created,
        summary=f"共有カレンダー「{calendar.name}」を作成しました。",
    )
    session.commit()
    await socket_manager.broadcast(
        broadcast_calendar_ids,
        _broadcast_payload(user, mode=_valid_mode(mode), anchor=_anchor_date(anchor_date, user)),
    )
    return _redirect_or_bundle(
        request,
        session,
        user,
        mode=_valid_mode(mode),
        anchor=_anchor_date(anchor_date, user),
        toast_message="カレンダーを作成しました。",
    )


@router.post("/calendars/{calendar_id}")
async def update_calendar(
    request: Request,
    calendar_id: str,
    name: str = Form(...),
    color: str = Form(DEFAULT_CALENDAR_COLOR),
    description: str = Form(""),
    mode: str = Form("month"),
    anchor_date: str = Form(""),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    calendar_uuid = _coerce_uuid(calendar_id)
    context = get_calendar_context(session, user.id, calendar_uuid, include_archived=True) if calendar_uuid else None
    if context is None or not _can_manage_calendar_settings(user, context):
        raise HTTPException(status_code=403, detail="カレンダー設定を変更できません。")
    context.calendar.name = name.strip() or context.calendar.name
    context.calendar.color = _normalize_calendar_color(color or context.calendar.color)
    context.calendar.description = description.strip() or None
    context.calendar.updated_at = utc_now()
    session.add(context.calendar)
    _record_shared_calendar_log(
        session,
        calendar=context.calendar,
        actor=user,
        action=CalendarActivityKind.calendar_updated,
        summary=f"共有カレンダー「{context.calendar.name}」の設定を更新しました。",
    )
    session.commit()
    await socket_manager.broadcast({context.calendar.id}, _broadcast_payload(user, mode=_valid_mode(mode), anchor=_anchor_date(anchor_date, user)))
    return _redirect_or_bundle(
        request,
        session,
        user,
        mode=_valid_mode(mode),
        anchor=_anchor_date(anchor_date, user),
        toast_message="カレンダー設定を更新しました。",
    )


@router.post("/calendars/{calendar_id}/archive")
async def archive_calendar(
    request: Request,
    calendar_id: str,
    mode: str = Form("month"),
    anchor_date: str = Form(""),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    calendar_uuid = _coerce_uuid(calendar_id)
    context = get_calendar_context(session, user.id, calendar_uuid, include_archived=True) if calendar_uuid else None
    if context is None or not _can_manage_calendar_settings(user, context):
        raise HTTPException(status_code=403, detail="カレンダーをアーカイブできません。")
    affected_users = session.exec(select(User).where(User.default_calendar_id == context.calendar.id)).all()
    context.calendar.is_archived = True
    context.calendar.updated_at = utc_now()
    session.add(context.calendar)
    _record_shared_calendar_log(
        session,
        calendar=context.calendar,
        actor=user,
        action=CalendarActivityKind.calendar_updated,
        summary=f"共有カレンダー「{context.calendar.name}」をアーカイブしました。",
    )
    for affected_user in affected_users:
        affected_user.default_calendar_id = None
        affected_user.updated_at = utc_now()
        session.add(affected_user)
    session.flush()
    for affected_user in affected_users:
        update_default_calendar_if_needed(
            session,
            affected_user,
            list_calendar_contexts(session, affected_user.id, include_archived=True),
        )
    session.commit()
    await socket_manager.broadcast({context.calendar.id}, _broadcast_payload(user, mode=_valid_mode(mode), anchor=_anchor_date(anchor_date, user)))
    return _redirect_or_bundle(
        request,
        session,
        user,
        mode=_valid_mode(mode),
        anchor=_anchor_date(anchor_date, user),
        toast_message="カレンダーをアーカイブしました。",
    )


@router.post("/calendars/{calendar_id}/restore")
async def restore_calendar(
    request: Request,
    calendar_id: str,
    mode: str = Form("month"),
    anchor_date: str = Form(""),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    calendar_uuid = _coerce_uuid(calendar_id)
    context = get_calendar_context(session, user.id, calendar_uuid, include_archived=True) if calendar_uuid else None
    if context is None or not _can_manage_calendar_settings(user, context):
        raise HTTPException(status_code=403, detail="選択したカレンダーは復元できません。")

    context.calendar.is_archived = False
    context.calendar.updated_at = utc_now()
    session.add(context.calendar)
    _record_shared_calendar_log(
        session,
        calendar=context.calendar,
        actor=user,
        action=CalendarActivityKind.calendar_updated,
        summary=f"共有カレンダー「{context.calendar.name}」を復元しました。",
    )
    member_user_ids = [
        item.user_id
        for item in session.exec(select(CalendarMember).where(CalendarMember.calendar_id == context.calendar.id)).all()
    ]
    if member_user_ids:
        member_users = session.exec(select(User).where(User.id.in_(member_user_ids), User.is_active.is_(True))).all()
        for member_user in member_users:
            update_default_calendar_if_needed(
                session,
                member_user,
                list_calendar_contexts(session, member_user.id, include_archived=True),
            )
    session.commit()
    await socket_manager.broadcast({context.calendar.id}, _broadcast_payload(user, mode=_valid_mode(mode), anchor=_anchor_date(anchor_date, user)))
    return _redirect_or_bundle(
        request,
        session,
        user,
        mode=_valid_mode(mode),
        anchor=_anchor_date(anchor_date, user),
        toast_message="カレンダーを復元しました。",
    )


@router.post("/calendars/{calendar_id}/delete")
async def delete_calendar(
    request: Request,
    calendar_id: str,
    mode: str = Form("month"),
    anchor_date: str = Form(""),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    calendar_uuid = _coerce_uuid(calendar_id)
    context = get_calendar_context(session, user.id, calendar_uuid, include_archived=True) if calendar_uuid else None
    if context is None or not _can_delete_calendar(user, context):
        raise HTTPException(status_code=403, detail="このカレンダーは削除できません。")

    deleted_calendar = context.calendar
    affected_users = session.exec(select(User).where(User.default_calendar_id == deleted_calendar.id)).all()
    for affected_user in affected_users:
        affected_user.default_calendar_id = None
        affected_user.updated_at = utc_now()
        session.add(affected_user)

    _delete_calendar_records(session, deleted_calendar)
    session.flush()

    for affected_user in affected_users:
        update_default_calendar_if_needed(
            session,
            affected_user,
            list_calendar_contexts(session, affected_user.id, include_archived=True),
        )

    session.commit()
    await socket_manager.broadcast({deleted_calendar.id}, _broadcast_payload(user, mode=_valid_mode(mode), anchor=_anchor_date(anchor_date, user)))
    return _redirect_or_bundle(
        request,
        session,
        user,
        mode=_valid_mode(mode),
        anchor=_anchor_date(anchor_date, user),
        toast_message="共有カレンダーを削除しました。" if deleted_calendar.calendar_type == CalendarType.facility_shared else "個人カレンダーを削除しました。",
    )


@router.post("/calendars/{calendar_id}/share")
async def share_calendar(
    request: Request,
    calendar_id: str,
    email: str = Form(...),
    role: str = Form("viewer"),
    mode: str = Form("month"),
    anchor_date: str = Form(""),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    calendar_uuid = _coerce_uuid(calendar_id)
    context = get_calendar_context(session, user.id, calendar_uuid, include_archived=True) if calendar_uuid else None
    if context is None or not _can_manage_calendar_settings(user, context):
        raise HTTPException(status_code=403, detail="共有設定を変更できません。")

    if context.calendar.calendar_type == CalendarType.facility_shared:
        _ensure_facility_shared_memberships(session, context.calendar)
        _record_shared_calendar_log(
            session,
            calendar=context.calendar,
            actor=user,
            action=CalendarActivityKind.share_synced,
            summary=f"共有カレンダー「{context.calendar.name}」の共有状態を再同期しました。",
        )
        session.commit()
        return _redirect_or_bundle(
            request,
            session,
            user,
            mode=_valid_mode(mode),
            anchor=_anchor_date(anchor_date, user),
            toast_message="施設共用カレンダーは全職員に自動共有されます。",
        )
    return _redirect_or_bundle(
        request,
        session,
        user,
        mode=_valid_mode(mode),
        anchor=_anchor_date(anchor_date, user),
        toast_message="個人カレンダーの手動共有は現在無効です。施設共用カレンダーをご利用ください。",
    )


@router.get("/events/new", response_class=HTMLResponse)
def new_event_form(
    request: Request,
    mode: str = Query(default="month"),
    date_value: str | None = Query(default=None, alias="date"),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    valid_mode = _valid_mode(mode)
    anchor = _anchor_date(date_value, user)
    contexts = list_calendar_contexts(session, user.id, include_archived=False)
    context = default_create_context(contexts, user)
    if context is None:
        raise HTTPException(status_code=403, detail="予定を登録できるカレンダーがありません。")
    return _event_form_context(
        request,
        session,
        user,
        event=None,
        occurrence=None,
        calendar_context=context,
        anchor=anchor,
        mode=valid_mode,
        action_url="/events",
        submit_label="予定を保存",
    )


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(
    request: Request,
    event_id: str,
    original_start_at: str | None = Query(default=None),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    event_uuid = _coerce_uuid(event_id)
    if event_uuid is None:
        raise HTTPException(status_code=404, detail="予定が見つかりません。")
    event = _load_event(session, event_uuid)
    context = get_calendar_context(session, user.id, event.calendar_id, include_archived=True)
    if context is None:
        raise HTTPException(status_code=404, detail="予定が見つかりません。")
    occurrence = find_occurrence(session, context, user, event, parse_iso_datetime(original_start_at))
    if occurrence is None:
        raise HTTPException(status_code=404, detail="対象の予定が見つかりません。")
    return templates.TemplateResponse(
        request,
        "calendar/_event_detail.html",
        {
            "request": request,
            "current_user": user,
            "occurrence": occurrence,
            "event": event,
            "user_timezone": user.timezone,
            "format_datetime_local": format_datetime_local,
        },
    )


@router.get("/events/{event_id}/edit", response_class=HTMLResponse)
def edit_event_form(
    request: Request,
    event_id: str,
    original_start_at: str | None = Query(default=None),
    mode: str = Query(default="month"),
    date_value: str | None = Query(default=None, alias="date"),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    event_uuid = _coerce_uuid(event_id)
    if event_uuid is None:
        raise HTTPException(status_code=404, detail="予定が見つかりません。")
    event = _load_event(session, event_uuid)
    context = get_calendar_context(session, user.id, event.calendar_id, include_archived=True)
    if context is None:
        raise HTTPException(status_code=404, detail="予定が見つかりません。")
    occurrence = find_occurrence(session, context, user, event, parse_iso_datetime(original_start_at))
    if occurrence is None or not occurrence.can_edit:
        raise HTTPException(status_code=403, detail="この予定は編集できません。")
    return _event_form_context(
        request,
        session,
        user,
        event=event,
        occurrence=occurrence,
        calendar_context=context,
        anchor=_anchor_date(date_value, user),
        mode=_valid_mode(mode),
        action_url=f"/events/{event.id}",
        submit_label="予定を保存",
    )


async def _finalize_event_save(
    request: Request,
    session: Session,
    user: User,
    *,
    event: Event,
    calendar: Calendar,
    reminder_values: list[int],
    mode: str,
    anchor: date,
    toast_message: str,
    broadcast_calendar_ids: set[UUID],
    activity_action: CalendarActivityKind | None = None,
    activity_summary: str = "",
) -> HTMLResponse | RedirectResponse:
    sync_event_reminders(session, event=event, user_id=user.id, minutes_before_values=reminder_values)
    rebuild_notification_jobs_for_event(session, event, calendar=calendar)
    if activity_action and activity_summary:
        _record_shared_calendar_log(
            session,
            calendar=calendar,
            actor=user,
            action=activity_action,
            summary=activity_summary,
        )
    session.commit()
    await socket_manager.broadcast(broadcast_calendar_ids, _broadcast_payload(user, mode=mode, anchor=anchor))
    return _redirect_or_bundle(
        request,
        session,
        user,
        mode=mode,
        anchor=anchor,
        toast_message=toast_message,
        triggers={"calendar-close-modal": True},
    )


@router.post("/events")
async def create_event(
    request: Request,
    calendar_id: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    location: str = Form(""),
    timezone_name: str = Form("Asia/Tokyo", alias="timezone"),
    start_value: str = Form(..., alias="start_value"),
    end_value: str = Form(..., alias="end_value"),
    is_all_day: bool = Form(False),
    visibility: str = Form("normal"),
    reminders: str = Form(""),
    recurrence_mode: str = Form(""),
    recurrence_interval: int = Form(1),
    recurrence_by_weekday: str = Form(""),
    recurrence_by_month_day: str = Form(""),
    recurrence_count: str = Form(""),
    recurrence_until: str = Form(""),
    mode: str = Form("month"),
    anchor_date: str = Form(""),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    anchor = _anchor_date(anchor_date, user)
    valid_mode = _valid_mode(mode)
    calendar_uuid = _coerce_uuid(calendar_id)
    context = get_calendar_context(session, user.id, calendar_uuid, include_archived=False) if calendar_uuid else None
    if context is None or not context.can_create_events or context.calendar.is_archived:
        raise HTTPException(status_code=403, detail="選択したカレンダーには予定を登録できません。")

    try:
        start_at, end_at = _event_times_from_form(
            start_value=start_value,
            end_value=end_value,
            timezone_name=timezone_name,
            is_all_day=is_all_day,
        )
    except ValueError as exc:
        return _event_form_context(
            request,
            session,
            user,
            event=None,
            occurrence=None,
            calendar_context=context,
            anchor=anchor,
            mode=valid_mode,
            action_url="/events",
            submit_label="予定を保存",
            form_error=str(exc),
            form_values={
                **_event_form_defaults(session, user, source_context=context),
                "calendar_id": calendar_id,
                "title": title,
                "description": description,
                "location": location,
                "timezone": timezone_name,
                "visibility": visibility,
                "is_all_day": is_all_day,
                "start_value": start_value,
                "end_value": end_value,
                "reminders": reminders,
            },
        )

    recurrence_rule = _recurrence_rule_from_form(
        session,
        existing=None,
        recurrence_mode=recurrence_mode,
        recurrence_interval=recurrence_interval,
        recurrence_by_weekday=recurrence_by_weekday,
        recurrence_by_month_day=recurrence_by_month_day,
        recurrence_count=recurrence_count,
        recurrence_until=recurrence_until,
        timezone_name=timezone_name,
    )
    event = Event(
        calendar_id=context.calendar.id,
        created_by_user_id=user.id,
        kind=EventKind.series_master if recurrence_rule else EventKind.single,
        title=title.strip() or "名称未設定の予定",
        description=description.strip() or None,
        location=location.strip() or None,
        start_at=start_at,
        end_at=end_at,
        timezone=timezone_name,
        is_all_day=is_all_day,
        visibility=EventVisibility.private if visibility == EventVisibility.private.value else EventVisibility.normal,
        status=EventLifecycleStatus.confirmed,
        recurrence_rule_id=recurrence_rule.id if recurrence_rule else None,
    )
    session.add(event)
    session.flush()
    reminder_values = split_csv_numbers(reminders)
    return await _finalize_event_save(
        request,
        session,
        user,
        event=event,
        calendar=context.calendar,
        reminder_values=reminder_values,
        mode=valid_mode,
        anchor=anchor,
        toast_message="予定を登録しました。",
        broadcast_calendar_ids={context.calendar.id},
        activity_action=CalendarActivityKind.event_created,
        activity_summary=f"{_loggable_event_label(event.title, event.visibility)}を登録しました。",
    )


@router.post("/events/{event_id}")
async def update_event(
    request: Request,
    event_id: str,
    calendar_id: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    location: str = Form(""),
    timezone_name: str = Form("Asia/Tokyo", alias="timezone"),
    start_value: str = Form(..., alias="start_value"),
    end_value: str = Form(..., alias="end_value"),
    is_all_day: bool = Form(False),
    visibility: str = Form("normal"),
    reminders: str = Form(""),
    recurrence_mode: str = Form(""),
    recurrence_interval: int = Form(1),
    recurrence_by_weekday: str = Form(""),
    recurrence_by_month_day: str = Form(""),
    recurrence_count: str = Form(""),
    recurrence_until: str = Form(""),
    scope: str = Form("all"),
    original_start_at: str = Form(""),
    mode: str = Form("month"),
    anchor_date: str = Form(""),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    valid_mode = _valid_mode(mode)
    anchor = _anchor_date(anchor_date, user)
    event_uuid = _coerce_uuid(event_id)
    if event_uuid is None:
        raise HTTPException(status_code=404, detail="予定が見つかりません。")
    event = _load_event(session, event_uuid)
    current_context = get_calendar_context(session, user.id, event.calendar_id, include_archived=True)
    if current_context is None:
        raise HTTPException(status_code=404, detail="予定が見つかりません。")
    target_calendar_uuid = _coerce_uuid(calendar_id)
    target_context = get_calendar_context(session, user.id, target_calendar_uuid, include_archived=False) if target_calendar_uuid else None
    if target_context is None or not target_context.can_create_events or target_context.calendar.is_archived:
        raise HTTPException(status_code=403, detail="選択したカレンダーには予定を登録できません。")

    target_original_start = parse_iso_datetime(original_start_at)
    normalized_scope = _scope_value(event, scope)
    if event.kind == EventKind.series_master and normalized_scope in {"one", "following"} and target_original_start is None:
        raise HTTPException(status_code=400, detail="繰り返し予定の更新対象を指定してください。")
    occurrence = find_occurrence(session, current_context, user, event, target_original_start)
    if occurrence is None or not occurrence.can_edit:
        raise HTTPException(status_code=403, detail="この予定は編集できません。")

    start_at, end_at = _event_times_from_form(
        start_value=start_value,
        end_value=end_value,
        timezone_name=timezone_name,
        is_all_day=is_all_day,
        )
    reminder_values = split_csv_numbers(reminders)
    old_calendar_id = event.calendar_id

    if event.kind != EventKind.series_master or normalized_scope == "single":
        recurrence_rule = _recurrence_rule_from_form(
            session,
            existing=session.get(RecurrenceRule, event.recurrence_rule_id) if event.recurrence_rule_id else None,
            recurrence_mode=recurrence_mode,
            recurrence_interval=recurrence_interval,
            recurrence_by_weekday=recurrence_by_weekday,
            recurrence_by_month_day=recurrence_by_month_day,
            recurrence_count=recurrence_count,
            recurrence_until=recurrence_until,
            timezone_name=timezone_name,
        )
        event.calendar_id = target_context.calendar.id
        event.kind = EventKind.series_master if recurrence_rule else EventKind.single
        event.title = title.strip() or event.title
        event.description = description.strip() or None
        event.location = location.strip() or None
        event.start_at = start_at
        event.end_at = end_at
        event.timezone = timezone_name
        event.is_all_day = is_all_day
        event.visibility = EventVisibility.private if visibility == EventVisibility.private.value else EventVisibility.normal
        event.recurrence_rule_id = recurrence_rule.id if recurrence_rule else None
        event.updated_at = utc_now()
        session.add(event)
        return await _finalize_event_save(
            request,
            session,
            user,
            event=event,
            calendar=target_context.calendar,
            reminder_values=reminder_values,
            mode=valid_mode,
            anchor=anchor,
            toast_message="予定を更新しました。",
            broadcast_calendar_ids={old_calendar_id, target_context.calendar.id},
            activity_action=CalendarActivityKind.event_updated,
            activity_summary=f"{_loggable_event_label(event.title, event.visibility)}を更新しました。",
        )

    if target_original_start is None:
        raise HTTPException(status_code=400, detail="繰り返し予定の更新対象を指定してください。")

    if normalized_scope == "one":
        override = session.exec(
            select(EventOverride).where(
                EventOverride.series_event_id == event.id,
                EventOverride.original_start_at == target_original_start,
            )
        ).first()
        if override is None:
            override = EventOverride(
                series_event_id=event.id,
                original_start_at=target_original_start,
                created_by_user_id=user.id,
            )
        override.title = title.strip() or event.title
        override.description = description.strip() or None
        override.location = location.strip() or None
        override.start_at = start_at
        override.end_at = end_at
        override.timezone = timezone_name
        override.is_all_day = is_all_day
        override.visibility = EventVisibility.private if visibility == EventVisibility.private.value else EventVisibility.normal
        override.is_cancelled = False
        override.updated_at = utc_now()
        session.add(override)
        return await _finalize_event_save(
            request,
            session,
            user,
            event=event,
            calendar=current_context.calendar,
            reminder_values=[item.minutes_before for item in active_reminders_for_event(session, event.id) if item.user_id == user.id],
            mode=valid_mode,
            anchor=anchor,
            toast_message="この予定だけ更新しました。",
            broadcast_calendar_ids={current_context.calendar.id},
            activity_action=CalendarActivityKind.event_updated,
            activity_summary=f"{_loggable_event_label(override.title or event.title, override.visibility or event.visibility)}を更新しました。",
        )

    target_original_start_utc = normalize_utc(target_original_start)
    event_start_utc = normalize_utc(event.start_at)

    if normalized_scope == "following" and target_original_start_utc != event_start_utc:
        recurrence_rule = session.get(RecurrenceRule, event.recurrence_rule_id) if event.recurrence_rule_id else None
        if recurrence_rule is None:
            raise HTTPException(status_code=400, detail="繰り返し設定が見つかりません。")
        recurrence_rule.until_at = target_original_start - timedelta(seconds=1)
        recurrence_rule.updated_at = utc_now()
        session.add(recurrence_rule)

        new_rule = _recurrence_rule_from_form(
            session,
            existing=None,
            recurrence_mode=recurrence_mode or recurrence_rule.freq.value,
            recurrence_interval=recurrence_interval or recurrence_rule.interval,
            recurrence_by_weekday=recurrence_by_weekday or recurrence_rule.by_weekday or "",
            recurrence_by_month_day=recurrence_by_month_day or recurrence_rule.by_month_day or "",
            recurrence_count=recurrence_count,
            recurrence_until=recurrence_until,
            timezone_name=timezone_name,
        )
        new_event = Event(
            calendar_id=target_context.calendar.id,
            created_by_user_id=user.id,
            kind=EventKind.series_master if new_rule else EventKind.single,
            title=title.strip() or event.title,
            description=description.strip() or None,
            location=location.strip() or None,
            start_at=start_at,
            end_at=end_at,
            timezone=timezone_name,
            is_all_day=is_all_day,
            visibility=EventVisibility.private if visibility == EventVisibility.private.value else EventVisibility.normal,
            status=EventLifecycleStatus.confirmed,
            recurrence_rule_id=new_rule.id if new_rule else None,
            split_from_event_id=event.id,
            split_from_original_start_at=target_original_start,
        )
        session.add(new_event)
        session.flush()
        split_reminders = reminder_values or [item.minutes_before for item in active_reminders_for_event(session, event.id) if item.user_id == user.id]
        sync_event_reminders(session, event=new_event, user_id=user.id, minutes_before_values=split_reminders)
        rebuild_notification_jobs_for_event(session, new_event, calendar=target_context.calendar)
        rebuild_notification_jobs_for_event(session, event, calendar=current_context.calendar)
        _record_shared_calendar_log(
            session,
            calendar=current_context.calendar,
            actor=user,
            action=CalendarActivityKind.event_updated,
            summary=f"{_loggable_event_label(event.title, event.visibility)}をこの予定以降で分割しました。",
        )
        if target_context.calendar.id != current_context.calendar.id:
            _record_shared_calendar_log(
                session,
                calendar=target_context.calendar,
                actor=user,
                action=CalendarActivityKind.event_created,
                summary=f"{_loggable_event_label(new_event.title, new_event.visibility)}を分割して登録しました。",
            )
        session.commit()
        await socket_manager.broadcast({old_calendar_id, target_context.calendar.id}, _broadcast_payload(user, mode=valid_mode, anchor=anchor))
        return _redirect_or_bundle(
            request,
            session,
            user,
            mode=valid_mode,
            anchor=anchor,
            toast_message="この予定以降を新しい予定として分割しました。",
        )

    recurrence_rule = _recurrence_rule_from_form(
        session,
        existing=session.get(RecurrenceRule, event.recurrence_rule_id) if event.recurrence_rule_id else None,
        recurrence_mode=recurrence_mode,
        recurrence_interval=recurrence_interval,
        recurrence_by_weekday=recurrence_by_weekday,
        recurrence_by_month_day=recurrence_by_month_day,
        recurrence_count=recurrence_count,
        recurrence_until=recurrence_until,
        timezone_name=timezone_name,
    )
    event.calendar_id = target_context.calendar.id
    event.kind = EventKind.series_master if recurrence_rule else EventKind.single
    event.title = title.strip() or event.title
    event.description = description.strip() or None
    event.location = location.strip() or None
    event.start_at = start_at
    event.end_at = end_at
    event.timezone = timezone_name
    event.is_all_day = is_all_day
    event.visibility = EventVisibility.private if visibility == EventVisibility.private.value else EventVisibility.normal
    event.recurrence_rule_id = recurrence_rule.id if recurrence_rule else None
    event.updated_at = utc_now()
    session.add(event)
    return await _finalize_event_save(
        request,
        session,
        user,
        event=event,
        calendar=target_context.calendar,
        reminder_values=reminder_values,
        mode=valid_mode,
        anchor=anchor,
        toast_message="繰り返し予定を更新しました。",
        broadcast_calendar_ids={old_calendar_id, target_context.calendar.id},
        activity_action=CalendarActivityKind.event_updated,
        activity_summary=f"{_loggable_event_label(event.title, event.visibility)}を更新しました。",
    )


@router.post("/events/{event_id}/delete")
async def delete_event(
    request: Request,
    event_id: str,
    scope: str = Form("all"),
    original_start_at: str = Form(""),
    mode: str = Form("month"),
    anchor_date: str = Form(""),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    valid_mode = _valid_mode(mode)
    anchor = _anchor_date(anchor_date, user)
    event_uuid = _coerce_uuid(event_id)
    if event_uuid is None:
        raise HTTPException(status_code=404, detail="予定が見つかりません。")
    event = _load_event(session, event_uuid)
    context = get_calendar_context(session, user.id, event.calendar_id, include_archived=True)
    if context is None:
        raise HTTPException(status_code=404, detail="予定が見つかりません。")
    target_original_start = parse_iso_datetime(original_start_at)
    normalized_scope = _scope_value(event, scope)
    if event.kind == EventKind.series_master and normalized_scope in {"one", "following"} and target_original_start is None:
        raise HTTPException(status_code=400, detail="繰り返し予定の削除対象を指定してください。")
    occurrence = find_occurrence(session, context, user, event, target_original_start)
    if occurrence is None or not occurrence.can_delete:
        raise HTTPException(status_code=403, detail="この予定は削除できません。")
    if event.kind != EventKind.series_master or normalized_scope in {"single", "all"}:
        event.is_deleted = True
        event.status = EventLifecycleStatus.cancelled
        event.updated_at = utc_now()
        session.add(event)
        rebuild_notification_jobs_for_event(session, event, calendar=context.calendar)
        _record_shared_calendar_log(
            session,
            calendar=context.calendar,
            actor=user,
            action=CalendarActivityKind.event_deleted,
            summary=f"{_loggable_event_label(event.title, event.visibility)}を削除しました。",
        )
        session.commit()
        await socket_manager.broadcast({context.calendar.id}, _broadcast_payload(user, mode=valid_mode, anchor=anchor))
        return _redirect_or_bundle(
            request,
            session,
            user,
            mode=valid_mode,
            anchor=anchor,
            toast_message="予定を削除しました。",
            triggers={"calendar-close-modal": True},
        )

    if target_original_start is None:
        raise HTTPException(status_code=400, detail="繰り返し予定の削除対象を指定してください。")

    if normalized_scope == "one":
        override = session.exec(
            select(EventOverride).where(
                EventOverride.series_event_id == event.id,
                EventOverride.original_start_at == target_original_start,
            )
        ).first()
        if override is None:
            override = EventOverride(
                series_event_id=event.id,
                original_start_at=target_original_start,
                created_by_user_id=user.id,
            )
        override.is_cancelled = True
        override.updated_at = utc_now()
        session.add(override)
        rebuild_notification_jobs_for_event(session, event, calendar=context.calendar)
        _record_shared_calendar_log(
            session,
            calendar=context.calendar,
            actor=user,
            action=CalendarActivityKind.event_deleted,
            summary=f"{_loggable_event_label(occurrence.title, occurrence.visibility)}を削除しました。",
        )
        session.commit()
        await socket_manager.broadcast({context.calendar.id}, _broadcast_payload(user, mode=valid_mode, anchor=anchor))
        return _redirect_or_bundle(
            request,
            session,
            user,
            mode=valid_mode,
            anchor=anchor,
            toast_message="この予定だけ削除しました。",
            triggers={"calendar-close-modal": True},
        )

    recurrence_rule = session.get(RecurrenceRule, event.recurrence_rule_id) if event.recurrence_rule_id else None
    if recurrence_rule is None:
        raise HTTPException(status_code=400, detail="繰り返し設定が見つかりません。")
    target_original_start_utc = normalize_utc(target_original_start)
    event_start_utc = normalize_utc(event.start_at)

    if target_original_start_utc == event_start_utc:
        event.is_deleted = True
        event.status = EventLifecycleStatus.cancelled
        event.updated_at = utc_now()
        session.add(event)
    else:
        recurrence_rule.until_at = target_original_start - timedelta(seconds=1)
        recurrence_rule.updated_at = utc_now()
        session.add(recurrence_rule)
    rebuild_notification_jobs_for_event(session, event, calendar=context.calendar)
    _record_shared_calendar_log(
        session,
        calendar=context.calendar,
        actor=user,
        action=CalendarActivityKind.event_deleted,
        summary=f"{_loggable_event_label(occurrence.title, occurrence.visibility)}をこの予定以降で削除しました。",
    )
    session.commit()
    await socket_manager.broadcast({context.calendar.id}, _broadcast_payload(user, mode=valid_mode, anchor=anchor))
    return _redirect_or_bundle(
        request,
        session,
        user,
        mode=valid_mode,
        anchor=anchor,
        toast_message="この予定以降を削除しました。",
        triggers={"calendar-close-modal": True},
    )


@router.post("/events/{event_id}/move")
async def move_event(
    request: Request,
    event_id: str,
    start_at: str = Form(...),
    end_at: str = Form(...),
    original_start_at: str = Form(""),
    session: Session = Depends(get_session),
):
    user = _require_calendar_user(session, request)
    event_uuid = _coerce_uuid(event_id)
    if event_uuid is None:
        raise HTTPException(status_code=404, detail="予定が見つかりません。")
    event = _load_event(session, event_uuid)
    context = get_calendar_context(session, user.id, event.calendar_id, include_archived=True)
    if context is None:
        raise HTTPException(status_code=404, detail="予定が見つかりません。")
    target_original_start = parse_iso_datetime(original_start_at)
    occurrence = find_occurrence(session, context, user, event, target_original_start)
    if occurrence is None or not occurrence.can_edit:
        raise HTTPException(status_code=403, detail="この予定は編集できません。")
    new_start = parse_iso_datetime(start_at)
    new_end = parse_iso_datetime(end_at)
    if new_start is None or new_end is None or new_end <= new_start:
        raise HTTPException(status_code=400, detail="開始日時または終了日時が不正です。")
    if event.kind == EventKind.series_master and target_original_start:
        override = session.exec(
            select(EventOverride).where(
                EventOverride.series_event_id == event.id,
                EventOverride.original_start_at == target_original_start,
            )
        ).first()
        if override is None:
            override = EventOverride(
                series_event_id=event.id,
                original_start_at=target_original_start,
                created_by_user_id=user.id,
            )
        override.start_at = new_start
        override.end_at = new_end
        override.updated_at = utc_now()
        session.add(override)
    else:
        event.start_at = new_start
        event.end_at = new_end
        event.updated_at = utc_now()
        session.add(event)
    rebuild_notification_jobs_for_event(session, event, calendar=context.calendar)
    _record_shared_calendar_log(
        session,
        calendar=context.calendar,
        actor=user,
        action=CalendarActivityKind.event_moved,
        summary=f"{_loggable_event_label(occurrence.title, occurrence.visibility)}の時間を変更しました。",
    )
    session.commit()
    await socket_manager.broadcast({context.calendar.id}, {"type": "calendar-updated"})
    return JSONResponse({"ok": True})


@router.post("/events/{event_id}/resize")
async def resize_event(
    request: Request,
    event_id: str,
    end_at: str = Form(...),
    original_start_at: str = Form(""),
    session: Session = Depends(get_session),
):
    user = _require_calendar_user(session, request)
    event_uuid = _coerce_uuid(event_id)
    if event_uuid is None:
        raise HTTPException(status_code=404, detail="予定が見つかりません。")
    event = _load_event(session, event_uuid)
    context = get_calendar_context(session, user.id, event.calendar_id, include_archived=True)
    if context is None:
        raise HTTPException(status_code=404, detail="予定が見つかりません。")
    target_original_start = parse_iso_datetime(original_start_at)
    occurrence = find_occurrence(session, context, user, event, target_original_start)
    if occurrence is None or not occurrence.can_edit:
        raise HTTPException(status_code=403, detail="この予定は編集できません。")
    new_end = parse_iso_datetime(end_at)
    if new_end is None or new_end <= occurrence.start_at:
        raise HTTPException(status_code=400, detail="終了日時が不正です。")
    if event.kind == EventKind.series_master and target_original_start:
        override = session.exec(
            select(EventOverride).where(
                EventOverride.series_event_id == event.id,
                EventOverride.original_start_at == target_original_start,
            )
        ).first()
        if override is None:
            override = EventOverride(
                series_event_id=event.id,
                original_start_at=target_original_start,
                created_by_user_id=user.id,
            )
        override.end_at = new_end
        override.updated_at = utc_now()
        session.add(override)
    else:
        event.end_at = new_end
        event.updated_at = utc_now()
        session.add(event)
    rebuild_notification_jobs_for_event(session, event, calendar=context.calendar)
    _record_shared_calendar_log(
        session,
        calendar=context.calendar,
        actor=user,
        action=CalendarActivityKind.event_resized,
        summary=f"{_loggable_event_label(occurrence.title, occurrence.visibility)}の終了時刻を変更しました。",
    )
    session.commit()
    await socket_manager.broadcast({context.calendar.id}, {"type": "calendar-updated"})
    return JSONResponse({"ok": True})


@router.get("/search/events", response_class=HTMLResponse)
def search_events(
    request: Request,
    q: str = Query(default=""),
    mode: str = Query(default="month"),
    date_value: str | None = Query(default=None, alias="date"),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    session: Session = Depends(get_session),
):
    user = _current_calendar_user(session, request)
    if user is None:
        return _mock_login_redirect(request)
    contexts = list_calendar_contexts(session, user.id, include_archived=False)
    from_date = parse_iso_date(date_from) or (local_today(user.timezone) - timedelta(days=30))
    to_date = parse_iso_date(date_to) or (local_today(user.timezone) + timedelta(days=60))
    normalized_query = q.strip()
    results = []
    if normalized_query:
        results = search_occurrences(
            session,
            contexts,
            user,
            query=normalized_query,
            range_start=combine_local_date(from_date, user.timezone),
            range_end=combine_local_date(to_date + timedelta(days=1), user.timezone),
            calendar_ids=_visible_calendar_ids(contexts),
        )
    return templates.TemplateResponse(
        request,
        "calendar/_search_modal.html",
        {
            "request": request,
            "current_user": user,
            "query": normalized_query,
            "results": results,
            "result_count": len(results),
            "user_timezone": user.timezone,
            "view_mode": _valid_mode(mode),
            "anchor_date": _anchor_date(date_value, user).isoformat(),
            "format_datetime_local": format_datetime_local,
        },
    )


@router.websocket("/ws/calendars/{calendar_id}")
async def calendar_updates(websocket: WebSocket, calendar_id: str, session: Session = Depends(get_session)):
    calendar_uuid = _coerce_uuid(calendar_id)
    principal = resolve_staff_principal(websocket)
    if principal is None or principal.user_id is None or calendar_uuid is None:
        await websocket.close(code=1008)
        return
    if not websocket_origin_allowed(websocket):
        await websocket.close(code=1008)
        return

    try:
        user = session.get(User, principal.user_id)
        context = (
            get_calendar_context(session, user.id, calendar_uuid, include_archived=True)
            if user is not None and user.is_active
            else None
        )
    finally:
        # A WebSocket may live for hours. Release its checked-out connection before accept.
        session.close()

    if user is None or not user.is_active or context is None:
        await websocket.close(code=1008)
        return
    await socket_manager.connect(calendar_uuid, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await socket_manager.disconnect(calendar_uuid, websocket)
