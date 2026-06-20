import csv
from dataclasses import dataclass
from datetime import date, datetime, time
from io import BytesIO, StringIO
from typing import Optional
from urllib.parse import urlencode
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from auth import get_current_staff_user, require_can_edit
from attendance_checks_service import sync_attendance_alarm
from database import get_session
from extended_care_fee_service import charge_status_label, recalculate_attendance_charge
from models import AttendanceRecord, Child, ChildStatus, Classroom, ExtendedCareCharge, ExtendedCareChargeStatus

router = APIRouter(prefix="/attendance", tags=["attendance"])
templates = Jinja2Templates(directory="templates")

VALID_TIME_FIELDS = {"either", "check_in", "check_out"}
VALID_SORT_FIELDS = {
    "attendance_date",
    "child_name",
    "classroom",
    "check_in_at",
    "check_out_at",
    "status",
    "planned_pickup_time",
}
VALID_SORT_ORDERS = {"asc", "desc"}
NOTICE_MESSAGES = {
    "export_admin_required": "CSV/Excel出力は管理者のみ利用できます。",
}


@dataclass
class AttendanceFilterParams:
    start_date: date
    end_date: date
    child_name: str
    classroom_id: Optional[int]
    classroom_id_value: str
    time_field: str
    time_from: Optional[time]
    time_to: Optional[time]
    time_from_value: str
    time_to_value: str
    sort_by: str
    sort_order: str

    @property
    def is_single_day(self) -> bool:
        return self.start_date == self.end_date

    @property
    def display_date_value(self) -> str:
        return self.start_date.isoformat()

    @property
    def has_search_filters(self) -> bool:
        today = date.today()
        return any(
            [
                self.start_date != today,
                self.end_date != today,
                bool(self.child_name),
                self.classroom_id is not None,
                self.time_field != "either",
                self.time_from is not None,
                self.time_to is not None,
            ]
        )

    def query_params(self) -> dict[str, str]:
        params = {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "sort_by": self.sort_by,
            "sort_order": self.sort_order,
        }
        if self.child_name:
            params["child_name"] = self.child_name
        if self.classroom_id_value:
            params["classroom_id"] = self.classroom_id_value
        if self.time_field != "either":
            params["time_field"] = self.time_field
        if self.time_from_value:
            params["time_from"] = self.time_from_value
        if self.time_to_value:
            params["time_to"] = self.time_to_value
        return params


@dataclass
class AttendanceReportRow:
    attendance_date: date
    attendance_record_id: Optional[int]
    child_id: int
    child_name: str
    child_name_kana: str
    classroom_id: Optional[int]
    classroom_name: str
    classroom_sort_order: int
    age: int
    is_enrolled: bool
    check_in_at: Optional[datetime]
    check_out_at: Optional[datetime]
    planned_pickup_time: str
    pickup_person: str
    note: str
    status: str
    extended_care_charge_id: Optional[int]
    extended_care_minutes: Optional[int]
    extended_care_amount: Optional[int]
    extended_care_status_label: str
    extended_care_requires_attention: bool
def _parse_target_date(raw: Optional[str]) -> date:
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="日付は YYYY-MM-DD 形式で指定してください") from exc


def _parse_optional_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="日付は YYYY-MM-DD 形式で指定してください") from exc


def _parse_optional_time(raw: Optional[str]) -> Optional[time]:
    if not raw:
        return None
    try:
        return time.fromisoformat(raw)
    except ValueError:
        return None


def _parse_optional_int(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _build_filters(
    target_date: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    child_name: Optional[str],
    classroom_id: Optional[str],
    time_field: Optional[str],
    time_from: Optional[str],
    time_to: Optional[str],
    sort_by: Optional[str],
    sort_order: Optional[str],
) -> AttendanceFilterParams:
    parsed_start = _parse_optional_date(start_date)
    parsed_end = _parse_optional_date(end_date)

    if parsed_start or parsed_end:
        start = parsed_start or parsed_end or date.today()
        end = parsed_end or parsed_start or start
    else:
        day = _parse_target_date(target_date)
        start = day
        end = day

    if start > end:
        start, end = end, start

    parsed_time_from = _parse_optional_time(time_from)
    parsed_time_to = _parse_optional_time(time_to)
    if parsed_time_from and parsed_time_to and parsed_time_from > parsed_time_to:
        parsed_time_from, parsed_time_to = parsed_time_to, parsed_time_from
        time_from, time_to = time_to, time_from

    normalized_time_field = time_field if time_field in VALID_TIME_FIELDS else "either"
    normalized_sort_by = sort_by if sort_by in VALID_SORT_FIELDS else "attendance_date"
    normalized_sort_order = sort_order if sort_order in VALID_SORT_ORDERS else "asc"
    normalized_classroom_id = _parse_optional_int(classroom_id)

    return AttendanceFilterParams(
        start_date=start,
        end_date=end,
        child_name=(child_name or "").strip(),
        classroom_id=normalized_classroom_id,
        classroom_id_value=str(normalized_classroom_id) if normalized_classroom_id is not None else "",
        time_field=normalized_time_field,
        time_from=parsed_time_from,
        time_to=parsed_time_to,
        time_from_value=time_from or "",
        time_to_value=time_to or "",
        sort_by=normalized_sort_by,
        sort_order=normalized_sort_order,
    )


def _attendance_status(record: Optional[AttendanceRecord]) -> str:
    if not record or not record.check_in_at:
        return "未登園"
    if not record.check_out_at:
        return "在園中"
    return "降園済み"


def _normalize_text(value: str) -> str:
    return "".join(value.lower().split())


def _build_row(
    target_day: date,
    child: Child,
    record: Optional[AttendanceRecord],
    charge: Optional[ExtendedCareCharge] = None,
) -> AttendanceReportRow:
    extended_care_status_label = "—"
    extended_care_minutes = None
    extended_care_amount = None
    extended_care_charge_id = None
    extended_care_requires_attention = False
    if charge:
        extended_care_status_label = charge_status_label(charge)
        extended_care_minutes = charge.extended_minutes
        extended_care_amount = charge.final_amount
        extended_care_charge_id = charge.id
        extended_care_requires_attention = (
            charge.status == ExtendedCareChargeStatus.draft and charge.final_amount > 0
        )
    elif record and record.check_out_at:
        extended_care_status_label = "未計算"

    return AttendanceReportRow(
        attendance_date=target_day,
        attendance_record_id=record.id if record else None,
        child_id=child.id or 0,
        child_name=child.full_name,
        child_name_kana=child.full_name_kana,
        classroom_id=child.classroom_id,
        classroom_name=child.classroom.name if child.classroom else "",
        classroom_sort_order=child.classroom.display_order if child.classroom else 999,
        age=child.age,
        is_enrolled=child.status == ChildStatus.enrolled,
        check_in_at=record.check_in_at if record else None,
        check_out_at=record.check_out_at if record else None,
        planned_pickup_time=record.planned_pickup_time if record and record.planned_pickup_time else "",
        pickup_person=record.pickup_person if record and record.pickup_person else "",
        note=record.note if record and record.note else "",
        status=_attendance_status(record),
        extended_care_charge_id=extended_care_charge_id,
        extended_care_minutes=extended_care_minutes,
        extended_care_amount=extended_care_amount,
        extended_care_status_label=extended_care_status_label,
        extended_care_requires_attention=extended_care_requires_attention,
    )


def _matches_child_name(row: AttendanceReportRow, child_name: str) -> bool:
    if not child_name:
        return True

    needle = _normalize_text(child_name)
    haystacks = [
        row.child_name,
        row.child_name.replace(" ", ""),
        row.child_name_kana,
        row.child_name_kana.replace(" ", ""),
    ]
    return any(needle in _normalize_text(value) for value in haystacks)


def _matches_classroom(row: AttendanceReportRow, classroom_id: Optional[int]) -> bool:
    if classroom_id is None:
        return True
    return row.classroom_id == classroom_id


def _time_in_range(value: Optional[datetime], time_from: Optional[time], time_to: Optional[time]) -> bool:
    if value is None:
        return False

    time_value = value.time().replace(second=0, microsecond=0)
    if time_from and time_value < time_from:
        return False
    if time_to and time_value > time_to:
        return False
    return True


def _matches_time_range(row: AttendanceReportRow, filters: AttendanceFilterParams) -> bool:
    if not filters.time_from and not filters.time_to:
        return True

    candidates: list[Optional[datetime]] = []
    if filters.time_field in {"either", "check_in"}:
        candidates.append(row.check_in_at)
    if filters.time_field in {"either", "check_out"}:
        candidates.append(row.check_out_at)

    return any(_time_in_range(value, filters.time_from, filters.time_to) for value in candidates)


def _load_charges_by_record_id(
    session: Session,
    records: list[AttendanceRecord],
) -> dict[int, ExtendedCareCharge]:
    record_ids = [record.id for record in records if record.id is not None]
    if not record_ids:
        return {}
    charges = session.exec(
        select(ExtendedCareCharge).where(ExtendedCareCharge.attendance_record_id.in_(record_ids))
    ).all()
    return {charge.attendance_record_id: charge for charge in charges}


def _sort_rows(rows: list[AttendanceReportRow], sort_by: str, sort_order: str) -> list[AttendanceReportRow]:
    reverse = sort_order == "desc"

    if sort_by == "child_name":
        return sorted(
            rows,
            key=lambda row: (_normalize_text(row.child_name_kana), _normalize_text(row.child_name), row.attendance_date),
            reverse=reverse,
        )

    if sort_by == "classroom":
        present = [row for row in rows if row.classroom_id is not None]
        missing = [row for row in rows if row.classroom_id is None]
        present.sort(
            key=lambda row: (
                row.classroom_sort_order,
                row.age,
                _normalize_text(row.child_name_kana),
                row.attendance_date,
            ),
            reverse=reverse,
        )
        return present + missing

    if sort_by == "status":
        status_order = {"未登園": 0, "在園中": 1, "降園済み": 2}
        return sorted(
            rows,
            key=lambda row: (
                status_order.get(row.status, 99),
                row.attendance_date,
                _normalize_text(row.child_name_kana),
            ),
            reverse=reverse,
        )

    if sort_by == "planned_pickup_time":
        present = [row for row in rows if row.planned_pickup_time]
        missing = [row for row in rows if not row.planned_pickup_time]
        present.sort(
            key=lambda row: (row.planned_pickup_time, row.attendance_date, _normalize_text(row.child_name_kana)),
            reverse=reverse,
        )
        return present + missing

    if sort_by in {"check_in_at", "check_out_at"}:
        present = [row for row in rows if getattr(row, sort_by) is not None]
        missing = [row for row in rows if getattr(row, sort_by) is None]
        present.sort(
            key=lambda row: (
                getattr(row, sort_by),
                row.attendance_date,
                _normalize_text(row.child_name_kana),
            ),
            reverse=reverse,
        )
        return present + missing

    return sorted(
        rows,
        key=lambda row: (row.attendance_date, _normalize_text(row.child_name_kana)),
        reverse=reverse,
    )


def _build_report_rows(session: Session, filters: AttendanceFilterParams) -> list[AttendanceReportRow]:
    children = session.exec(
        select(Child)
        .options(selectinload(Child.classroom))
        .order_by(Child.last_name_kana, Child.first_name_kana)
    ).all()
    children_by_id = {child.id: child for child in children}

    records = session.exec(
        select(AttendanceRecord).where(
            AttendanceRecord.attendance_date >= filters.start_date,
            AttendanceRecord.attendance_date <= filters.end_date,
        )
    ).all()
    charges_by_record_id = _load_charges_by_record_id(session, records)

    if filters.is_single_day:
        enrolled_ids = {
            child.id
            for child in children
            if child.id is not None and child.status == ChildStatus.enrolled
        }
        record_ids = {record.child_id for record in records}
        candidate_ids = enrolled_ids | record_ids

        rows = []
        records_by_child = {record.child_id: record for record in records}
        for child in children:
            if child.id not in candidate_ids:
                continue
            record = records_by_child.get(child.id)
            charge = charges_by_record_id.get(record.id) if record and record.id is not None else None
            rows.append(_build_row(filters.start_date, child, record, charge))
    else:
        rows = []
        for record in records:
            child = children_by_id.get(record.child_id)
            if child is None:
                continue
            rows.append(_build_row(record.attendance_date, child, record, charges_by_record_id.get(record.id)))

    filtered_rows = [
        row
        for row in rows
        if _matches_child_name(row, filters.child_name)
        and _matches_classroom(row, filters.classroom_id)
        and _matches_time_range(row, filters)
    ]
    return _sort_rows(filtered_rows, filters.sort_by, filters.sort_order)


def _build_summary(rows: list[AttendanceReportRow]) -> dict[str, int]:
    return {
        "result_count": len(rows),
        "unique_children_count": len({row.child_id for row in rows}),
        "checked_in_count": sum(1 for row in rows if row.check_in_at is not None),
        "checked_out_count": sum(1 for row in rows if row.check_out_at is not None),
        "not_checked_in_count": sum(1 for row in rows if row.check_in_at is None),
    }


def _format_time(value: Optional[datetime]) -> str:
    return value.strftime("%H:%M") if value else ""


def _export_headers() -> list[str]:
    return [
        "日付",
        "園児名",
        "園児名（カナ）",
        "クラス",
        "年齢",
        "登園時刻",
        "降園時刻",
        "状態",
        "お迎え予定時刻",
        "お迎え予定者",
        "備考",
        "延長分数",
        "延長料金",
        "延長状態",
    ]


def _export_rows(rows: list[AttendanceReportRow]) -> list[list[str]]:
    return [
        [
            row.attendance_date.isoformat(),
            row.child_name,
            row.child_name_kana,
            row.classroom_name,
            f"{row.age}歳",
            _format_time(row.check_in_at),
            _format_time(row.check_out_at),
            row.status,
            row.planned_pickup_time,
            row.pickup_person,
            row.note,
            str(row.extended_care_minutes) if row.extended_care_minutes is not None else "",
            str(row.extended_care_amount) if row.extended_care_amount is not None else "",
            row.extended_care_status_label if row.extended_care_status_label != "—" else "",
        ]
        for row in rows
    ]


def _build_query_string(filters: AttendanceFilterParams) -> str:
    return urlencode(filters.query_params())


def _notice_message(notice: Optional[str]) -> str:
    return NOTICE_MESSAGES.get(notice or "", "")


def _redirect_with_notice(request: Request, notice: str) -> RedirectResponse:
    params = dict(request.query_params)
    params["notice"] = notice
    return RedirectResponse(url=f"/attendance?{urlencode(params)}", status_code=303)


def _build_redirect_url(day: date, return_query: Optional[str]) -> str:
    query = return_query or urlencode({"start_date": day.isoformat(), "end_date": day.isoformat()})
    return f"/attendance?{query}" if query else "/attendance"


def _build_csv_content(rows: list[AttendanceReportRow]) -> bytes:
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_export_headers())
    writer.writerows(_export_rows(rows))
    return buffer.getvalue().encode("utf-8-sig")


def _xlsx_column_name(index: int) -> str:
    name = ""
    current = index + 1
    while current:
        current, remainder = divmod(current - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _build_xlsx_content(rows: list[AttendanceReportRow]) -> bytes:
    workbook_rows = [_export_headers(), *_export_rows(rows)]
    row_xml_parts = []

    for row_index, row in enumerate(workbook_rows, start=1):
        cell_xml_parts = []
        for column_index, value in enumerate(row):
            cell_ref = f"{_xlsx_column_name(column_index)}{row_index}"
            cell_xml_parts.append(
                '<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{value}</t></is></c>'.format(
                    ref=cell_ref,
                    value=escape(value),
                )
            )
        row_xml_parts.append(f'<row r="{row_index}">{"".join(cell_xml_parts)}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        f'{"".join(row_xml_parts)}'
        "</sheetData>"
        "</worksheet>"
    )

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Attendance" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )

    buffer = BytesIO()
    with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", rels_xml)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)

    return buffer.getvalue()


def _export_filename(filters: AttendanceFilterParams, extension: str) -> str:
    if filters.is_single_day:
        return f"attendance_{filters.start_date.isoformat()}.{extension}"
    return f"attendance_{filters.start_date.isoformat()}_{filters.end_date.isoformat()}.{extension}"


@router.get("/", response_class=HTMLResponse)
def attendance_list(
    request: Request,
    target_date: Optional[str] = Query(default=None, alias="date"),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    child_name: Optional[str] = Query(default=None),
    classroom_id: Optional[str] = Query(default=None),
    time_field: Optional[str] = Query(default="either"),
    time_from: Optional[str] = Query(default=None),
    time_to: Optional[str] = Query(default=None),
    sort_by: Optional[str] = Query(default="attendance_date"),
    sort_order: Optional[str] = Query(default="asc"),
    notice: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    filters = _build_filters(
        target_date=target_date,
        start_date=start_date,
        end_date=end_date,
        child_name=child_name,
        classroom_id=classroom_id,
        time_field=time_field,
        time_from=time_from,
        time_to=time_to,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    rows = _build_report_rows(session, filters)
    summary = _build_summary(rows)
    classrooms = session.exec(select(Classroom).order_by(Classroom.display_order, Classroom.id)).all()

    return templates.TemplateResponse(
        request,
        "attendance_list.html",
        {
            "request": request,
            "rows": rows,
            "filters": filters,
            "current_query_string": _build_query_string(filters),
            "notice_message": _notice_message(notice),
            "current_user": current_user,
            "result_count": summary["result_count"],
            "unique_children_count": summary["unique_children_count"],
            "checked_in_count": summary["checked_in_count"],
            "checked_out_count": summary["checked_out_count"],
            "not_checked_in_count": summary["not_checked_in_count"],
            "classroom_options": classrooms,
            "time_field_options": [
                {"value": "either", "label": "登園・降園どちらか"},
                {"value": "check_in", "label": "登園のみ"},
                {"value": "check_out", "label": "降園のみ"},
            ],
            "sort_options": [
                {"value": "attendance_date", "label": "日付"},
                {"value": "child_name", "label": "園児名"},
                {"value": "classroom", "label": "クラス"},
                {"value": "check_in_at", "label": "登園時刻"},
                {"value": "check_out_at", "label": "降園時刻"},
                {"value": "status", "label": "状態"},
                {"value": "planned_pickup_time", "label": "お迎え予定時刻"},
            ],
            "sort_order_options": [
                {"value": "asc", "label": "昇順"},
                {"value": "desc", "label": "降順"},
            ],
        },
    )


@router.get("/export.csv")
def export_attendance_csv(
    request: Request,
    target_date: Optional[str] = Query(default=None, alias="date"),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    child_name: Optional[str] = Query(default=None),
    classroom_id: Optional[str] = Query(default=None),
    time_field: Optional[str] = Query(default="either"),
    time_from: Optional[str] = Query(default=None),
    time_to: Optional[str] = Query(default=None),
    sort_by: Optional[str] = Query(default="attendance_date"),
    sort_order: Optional[str] = Query(default="asc"),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    if not current_user.is_admin:
        return _redirect_with_notice(request, "export_admin_required")

    filters = _build_filters(
        target_date=target_date,
        start_date=start_date,
        end_date=end_date,
        child_name=child_name,
        classroom_id=classroom_id,
        time_field=time_field,
        time_from=time_from,
        time_to=time_to,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    rows = _build_report_rows(session, filters)
    filename = _export_filename(filters, "csv")

    return Response(
        content=_build_csv_content(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export.xlsx")
def export_attendance_xlsx(
    request: Request,
    target_date: Optional[str] = Query(default=None, alias="date"),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    child_name: Optional[str] = Query(default=None),
    classroom_id: Optional[str] = Query(default=None),
    time_field: Optional[str] = Query(default="either"),
    time_from: Optional[str] = Query(default=None),
    time_to: Optional[str] = Query(default=None),
    sort_by: Optional[str] = Query(default="attendance_date"),
    sort_order: Optional[str] = Query(default="asc"),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    if not current_user.is_admin:
        return _redirect_with_notice(request, "export_admin_required")

    filters = _build_filters(
        target_date=target_date,
        start_date=start_date,
        end_date=end_date,
        child_name=child_name,
        classroom_id=classroom_id,
        time_field=time_field,
        time_from=time_from,
        time_to=time_to,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    rows = _build_report_rows(session, filters)
    filename = _export_filename(filters, "xlsx")

    return Response(
        content=_build_xlsx_content(rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{child_id}/check-in")
def check_in(
    child_id: int,
    target_date: str = Form(..., alias="date"),
    return_query: Optional[str] = Form(default=None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)

    child = session.get(Child, child_id)
    if not child:
        raise HTTPException(status_code=404, detail="園児が見つかりません")
    if child.status != ChildStatus.enrolled:
        raise HTTPException(status_code=400, detail="在園児のみ打刻できます")

    day = _parse_target_date(target_date)
    record = session.exec(
        select(AttendanceRecord).where(
            AttendanceRecord.child_id == child_id,
            AttendanceRecord.attendance_date == day,
        )
    ).first()

    now = datetime.now()
    if not record:
        record = AttendanceRecord(child_id=child_id, attendance_date=day, check_in_at=now)
    elif record.check_in_at is None:
        record.check_in_at = now

    record.updated_at = now
    session.add(record)
    session.flush()
    sync_attendance_alarm(session, child_id=child_id, target_date=day, record=record, now=now)
    session.commit()

    return RedirectResponse(url=_build_redirect_url(day, return_query), status_code=303)


@router.post("/{child_id}/check-out")
def check_out(
    child_id: int,
    target_date: str = Form(..., alias="date"),
    return_query: Optional[str] = Form(default=None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)

    child = session.get(Child, child_id)
    if not child:
        raise HTTPException(status_code=404, detail="園児が見つかりません")
    if child.status != ChildStatus.enrolled:
        raise HTTPException(status_code=400, detail="在園児のみ打刻できます")

    day = _parse_target_date(target_date)
    record = session.exec(
        select(AttendanceRecord).where(
            AttendanceRecord.child_id == child_id,
            AttendanceRecord.attendance_date == day,
        )
    ).first()

    if not record or record.check_in_at is None:
        raise HTTPException(status_code=400, detail="先に登園打刻を行ってください")

    now = datetime.now()
    if record.check_out_at is None:
        record.check_out_at = now
    record.updated_at = now

    session.add(record)
    session.flush()
    recalculate_attendance_charge(session, record)
    sync_attendance_alarm(session, child_id=child_id, target_date=day, record=record, now=now)
    session.commit()

    return RedirectResponse(url=_build_redirect_url(day, return_query), status_code=303)
