from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from io import StringIO
from math import ceil
from typing import Optional

from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from models import (
    AttendanceRecord,
    Child,
    ExtendedCareCharge,
    ExtendedCareChargeStatus,
    ExtendedCareFeeRule,
)
from time_utils import local_today, utc_now


LOCKED_STATUSES = {
    ExtendedCareChargeStatus.confirmed,
    ExtendedCareChargeStatus.manual_adjusted,
    ExtendedCareChargeStatus.excluded,
}


@dataclass(slots=True)
class ChargeComputation:
    charge_start_at: datetime
    actual_check_out_at: datetime
    extended_minutes: int
    billable_units: int
    auto_amount: int


@dataclass(slots=True)
class ExtendedCareChargeDetail:
    charge_id: int
    attendance_record_id: int
    target_date: date
    check_in_at: Optional[datetime]
    check_out_at: Optional[datetime]
    planned_pickup_time: str
    charge_start_at: datetime
    extended_minutes: int
    billable_units: int
    auto_amount: int
    adjustment_amount: int
    final_amount: int
    status: ExtendedCareChargeStatus
    status_label: str
    adjustment_reason: str
    warning: str = ""

    @property
    def needs_confirmation(self) -> bool:
        return self.status == ExtendedCareChargeStatus.draft and self.final_amount > 0


@dataclass(slots=True)
class ExtendedCareMonthlySummary:
    child_id: int
    child_name: str
    child_name_kana: str
    classroom_name: str
    classroom_sort_order: int
    extended_days: int = 0
    extended_minutes_total: int = 0
    auto_amount_total: int = 0
    adjustment_amount_total: int = 0
    final_amount_total: int = 0
    unconfirmed_count: int = 0
    details: list[ExtendedCareChargeDetail] = field(default_factory=list)


@dataclass(slots=True)
class ExtendedCareMonthlyOverview:
    month: str
    start_date: date
    end_date: date
    summaries: list[ExtendedCareMonthlySummary]
    warnings: list[str]
    total_extended_days: int
    total_extended_minutes: int
    total_auto_amount: int
    total_adjustment_amount: int
    total_final_amount: int
    total_unconfirmed_count: int


def parse_month(raw: Optional[str]) -> tuple[str, date, date]:
    today = local_today()
    normalized = today.strftime("%Y-%m")
    if raw:
        try:
            parsed = date.fromisoformat(f"{raw}-01")
            normalized = parsed.strftime("%Y-%m")
        except ValueError:
            parsed = today.replace(day=1)
    else:
        parsed = today.replace(day=1)

    if parsed.month == 12:
        next_month = date(parsed.year + 1, 1, 1)
    else:
        next_month = date(parsed.year, parsed.month + 1, 1)
    return normalized, parsed, next_month - timedelta(days=1)


def charge_status_label(charge: ExtendedCareCharge) -> str:
    if charge.status == ExtendedCareChargeStatus.draft and charge.final_amount == 0:
        return "0円"
    return charge.status.label


def validate_fee_rule(
    session: Session,
    *,
    name: str,
    effective_from: date,
    effective_to: Optional[date],
    start_time: str,
    grace_minutes: int,
    rounding_minutes: int,
    unit_price: int,
    daily_cap_amount: Optional[int],
    is_active: bool,
    rule_id: Optional[int] = None,
) -> list[str]:
    errors: list[str] = []
    if not name.strip():
        errors.append("ルール名を入力してください。")
    try:
        _parse_rule_time(start_time)
    except ValueError:
        errors.append("延長開始時刻は HH:MM 形式で入力してください。")
    if not 0 <= grace_minutes <= 120:
        errors.append("猶予時間は 0 以上 120 以下で入力してください。")
    if not 1 <= rounding_minutes <= 120:
        errors.append("丸め単位は 1 以上 120 以下で入力してください。")
    if unit_price < 0:
        errors.append("単価は 0 以上で入力してください。")
    if daily_cap_amount is not None and daily_cap_amount < 0:
        errors.append("日別上限額は空欄または 0 以上で入力してください。")
    if effective_to is not None and effective_to < effective_from:
        errors.append("適用終了日は適用開始日以降にしてください。")

    if is_active and not errors:
        existing_rules = session.exec(
            select(ExtendedCareFeeRule).where(ExtendedCareFeeRule.is_active == True)  # noqa: E712
        ).all()
        for existing in existing_rules:
            if rule_id is not None and existing.id == rule_id:
                continue
            if _periods_overlap(effective_from, effective_to, existing.effective_from, existing.effective_to):
                errors.append("既存の有効ルールと適用期間が重複しています。")
                break

    return errors


def get_active_rule_for_date(session: Session, target_date: date) -> Optional[ExtendedCareFeeRule]:
    rules = session.exec(
        select(ExtendedCareFeeRule).where(
            ExtendedCareFeeRule.is_active == True,  # noqa: E712
            ExtendedCareFeeRule.effective_from <= target_date,
        )
    ).all()
    candidates = [
        rule
        for rule in rules
        if rule.effective_to is None or rule.effective_to >= target_date
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda rule: (rule.effective_from, rule.id or 0), reverse=True)[0]


def calculate_charge(record: AttendanceRecord, rule: ExtendedCareFeeRule) -> ChargeComputation:
    if record.check_out_at is None:
        raise ValueError("降園打刻がないため計算できません。")

    charge_start_at = datetime.combine(record.attendance_date, _parse_rule_time(rule.start_time)) + timedelta(
        minutes=rule.grace_minutes
    )
    actual_check_out_at = _floor_to_minute(record.check_out_at)

    if actual_check_out_at <= charge_start_at:
        return ChargeComputation(
            charge_start_at=charge_start_at,
            actual_check_out_at=actual_check_out_at,
            extended_minutes=0,
            billable_units=0,
            auto_amount=0,
        )

    elapsed_seconds = (actual_check_out_at - charge_start_at).total_seconds()
    raw_minutes = max(0, int(elapsed_seconds // 60))
    billable_units = ceil(raw_minutes / rule.rounding_minutes)
    extended_minutes = billable_units * rule.rounding_minutes
    auto_amount = billable_units * rule.unit_price
    if rule.daily_cap_amount is not None:
        auto_amount = min(auto_amount, rule.daily_cap_amount)

    return ChargeComputation(
        charge_start_at=charge_start_at,
        actual_check_out_at=actual_check_out_at,
        extended_minutes=extended_minutes,
        billable_units=billable_units,
        auto_amount=auto_amount,
    )


def recalculate_attendance_charge(
    session: Session,
    record: AttendanceRecord,
    *,
    include_locked: bool = False,
) -> Optional[ExtendedCareCharge]:
    if record.id is None or record.check_out_at is None:
        return None

    existing = session.exec(
        select(ExtendedCareCharge).where(ExtendedCareCharge.attendance_record_id == record.id)
    ).first()
    if existing and existing.status in LOCKED_STATUSES and not include_locked:
        return existing

    rule = get_active_rule_for_date(session, record.attendance_date)
    if rule is None or rule.id is None:
        return existing

    computed = calculate_charge(record, rule)
    charge = existing or ExtendedCareCharge(
        attendance_record_id=record.id,
        child_id=record.child_id,
        target_date=record.attendance_date,
        rule_id=rule.id,
        charge_start_at=computed.charge_start_at,
    )
    charge.child_id = record.child_id
    charge.target_date = record.attendance_date
    charge.rule_id = rule.id
    charge.charge_start_at = computed.charge_start_at
    charge.actual_check_out_at = computed.actual_check_out_at
    charge.extended_minutes = computed.extended_minutes
    charge.billable_units = computed.billable_units
    charge.auto_amount = computed.auto_amount
    charge.adjustment_amount = 0
    charge.final_amount = computed.auto_amount
    charge.status = ExtendedCareChargeStatus.draft
    charge.adjustment_reason = None
    charge.confirmed_by = None
    charge.confirmed_at = None
    charge.updated_at = utc_now()
    session.add(charge)
    return charge


def recalculate_period(
    session: Session,
    start_date: date,
    end_date: date,
    *,
    include_locked: bool = False,
) -> int:
    records = session.exec(
        select(AttendanceRecord).where(
            AttendanceRecord.attendance_date >= start_date,
            AttendanceRecord.attendance_date <= end_date,
            AttendanceRecord.check_out_at.is_not(None),
        )
    ).all()
    updated = 0
    for record in records:
        before = session.exec(
            select(ExtendedCareCharge).where(ExtendedCareCharge.attendance_record_id == record.id)
        ).first()
        before_snapshot = _charge_recalculation_snapshot(before)
        charge = recalculate_attendance_charge(session, record, include_locked=include_locked)
        if charge is not None and _charge_recalculation_snapshot(charge) != before_snapshot:
            updated += 1
    return updated


def _charge_recalculation_snapshot(charge: Optional[ExtendedCareCharge]) -> tuple | None:
    if charge is None:
        return None
    return (
        charge.rule_id,
        charge.charge_start_at,
        charge.actual_check_out_at,
        charge.extended_minutes,
        charge.billable_units,
        charge.auto_amount,
        charge.adjustment_amount,
        charge.final_amount,
        charge.status,
        charge.adjustment_reason,
        charge.confirmed_by,
        charge.confirmed_at,
    )


def confirm_charge(charge: ExtendedCareCharge, staff_name: str) -> None:
    charge.status = ExtendedCareChargeStatus.confirmed
    charge.confirmed_by = staff_name
    charge.confirmed_at = utc_now()
    charge.final_amount = max(0, charge.auto_amount + charge.adjustment_amount)
    charge.updated_at = utc_now()


def adjust_charge(charge: ExtendedCareCharge, adjustment_amount: int, reason: str, staff_name: str) -> None:
    cleaned_reason = reason.strip()
    if adjustment_amount != 0 and not cleaned_reason:
        raise ValueError("調整額を入力する場合は理由を入力してください。")
    charge.adjustment_amount = adjustment_amount
    charge.adjustment_reason = cleaned_reason or None
    charge.final_amount = max(0, charge.auto_amount + adjustment_amount)
    charge.status = ExtendedCareChargeStatus.manual_adjusted
    charge.confirmed_by = staff_name
    charge.confirmed_at = utc_now()
    charge.updated_at = utc_now()


def exclude_charge(charge: ExtendedCareCharge, reason: str, staff_name: str) -> None:
    charge.adjustment_amount = -charge.auto_amount
    charge.adjustment_reason = reason.strip() or "請求対象外"
    charge.final_amount = 0
    charge.status = ExtendedCareChargeStatus.excluded
    charge.confirmed_by = staff_name
    charge.confirmed_at = utc_now()
    charge.updated_at = utc_now()


def build_monthly_overview(
    session: Session,
    *,
    month: str,
    classroom_id: Optional[int] = None,
    child_name: str = "",
    unconfirmed_only: bool = False,
) -> ExtendedCareMonthlyOverview:
    normalized_month, start_date, end_date = parse_month(month)
    normalized_child_name = _normalize_text(child_name)
    children = session.exec(select(Child).options(selectinload(Child.classroom))).all()
    children_by_id = {child.id: child for child in children if child.id is not None}
    summaries: dict[int, ExtendedCareMonthlySummary] = {}
    warnings: list[str] = []

    charges = session.exec(
        select(ExtendedCareCharge).where(
            ExtendedCareCharge.target_date >= start_date,
            ExtendedCareCharge.target_date <= end_date,
        )
    ).all()
    charge_record_ids = {charge.attendance_record_id for charge in charges}
    record_ids = list(charge_record_ids)
    records_by_id: dict[int, AttendanceRecord] = {}
    if record_ids:
        records = session.exec(
            select(AttendanceRecord).where(AttendanceRecord.id.in_(record_ids))
        ).all()
        records_by_id = {record.id: record for record in records if record.id is not None}

    for charge in sorted(charges, key=lambda item: (item.target_date, item.child_id, item.id or 0)):
        child = children_by_id.get(charge.child_id)
        if child is None or not _matches_child_filter(child, classroom_id, normalized_child_name):
            continue
        if unconfirmed_only and not _is_unconfirmed(charge):
            continue

        summary = summaries.setdefault(charge.child_id, _make_summary(child))
        record = records_by_id.get(charge.attendance_record_id)
        detail = _make_detail(charge, record)
        summary.details.append(detail)
        if charge.final_amount > 0:
            summary.extended_days += 1
        summary.extended_minutes_total += charge.extended_minutes
        summary.auto_amount_total += charge.auto_amount
        summary.adjustment_amount_total += charge.adjustment_amount
        summary.final_amount_total += charge.final_amount
        if _is_unconfirmed(charge):
            summary.unconfirmed_count += 1

    unchecked_records = session.exec(
        select(AttendanceRecord).where(
            AttendanceRecord.attendance_date >= start_date,
            AttendanceRecord.attendance_date <= end_date,
            AttendanceRecord.check_out_at.is_not(None),
        )
    ).all()
    for record in unchecked_records:
        if record.id in charge_record_ids:
            continue
        child = children_by_id.get(record.child_id)
        if child is None or not _matches_child_filter(child, classroom_id, normalized_child_name):
            continue
        rule = get_active_rule_for_date(session, record.attendance_date)
        reason = "有効な料金ルールがありません" if rule is None else "未計算です"
        warnings.append(f"{record.attendance_date.isoformat()} {child.full_name}: {reason}")

    summary_list = sorted(
        summaries.values(),
        key=lambda item: (item.classroom_sort_order, _normalize_text(item.child_name_kana), item.child_id),
    )
    return ExtendedCareMonthlyOverview(
        month=normalized_month,
        start_date=start_date,
        end_date=end_date,
        summaries=summary_list,
        warnings=warnings,
        total_extended_days=sum(item.extended_days for item in summary_list),
        total_extended_minutes=sum(item.extended_minutes_total for item in summary_list),
        total_auto_amount=sum(item.auto_amount_total for item in summary_list),
        total_adjustment_amount=sum(item.adjustment_amount_total for item in summary_list),
        total_final_amount=sum(item.final_amount_total for item in summary_list),
        total_unconfirmed_count=sum(item.unconfirmed_count for item in summary_list),
    )


def build_monthly_csv(overview: ExtendedCareMonthlyOverview) -> bytes:
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(
        [
            "対象月",
            "園児ID",
            "園児名",
            "園児名カナ",
            "クラス",
            "延長回数",
            "延長分数合計",
            "自動計算額合計",
            "調整額合計",
            "確定額合計",
            "未確認件数",
        ]
    )
    for summary in overview.summaries:
        writer.writerow(
            [
                overview.month,
                summary.child_id,
                summary.child_name,
                summary.child_name_kana,
                summary.classroom_name,
                summary.extended_days,
                summary.extended_minutes_total,
                summary.auto_amount_total,
                summary.adjustment_amount_total,
                summary.final_amount_total,
                summary.unconfirmed_count,
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


def _parse_rule_time(value: str) -> time:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.time()


def _floor_to_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def _periods_overlap(
    start_a: date,
    end_a: Optional[date],
    start_b: date,
    end_b: Optional[date],
) -> bool:
    normalized_end_a = end_a or date.max
    normalized_end_b = end_b or date.max
    return start_a <= normalized_end_b and start_b <= normalized_end_a


def _normalize_text(value: str) -> str:
    return "".join((value or "").lower().split())


def _matches_child_filter(child: Child, classroom_id: Optional[int], child_name: str) -> bool:
    if classroom_id is not None and child.classroom_id != classroom_id:
        return False
    if not child_name:
        return True
    haystacks = [
        child.full_name,
        child.full_name.replace(" ", ""),
        child.full_name_kana,
        child.full_name_kana.replace(" ", ""),
    ]
    return any(child_name in _normalize_text(value) for value in haystacks)


def _is_unconfirmed(charge: ExtendedCareCharge) -> bool:
    return charge.status == ExtendedCareChargeStatus.draft and charge.final_amount > 0


def _make_summary(child: Child) -> ExtendedCareMonthlySummary:
    return ExtendedCareMonthlySummary(
        child_id=child.id or 0,
        child_name=child.full_name,
        child_name_kana=child.full_name_kana,
        classroom_name=child.classroom.name if child.classroom else "",
        classroom_sort_order=child.classroom.display_order if child.classroom else 999,
    )


def _make_detail(charge: ExtendedCareCharge, record: Optional[AttendanceRecord]) -> ExtendedCareChargeDetail:
    warning = ""
    if record and record.check_out_at:
        cutoff = datetime.combine(record.attendance_date + timedelta(days=1), time(3, 0))
        if record.check_out_at > cutoff:
            warning = "翌日03:00を超える降園打刻です"
    return ExtendedCareChargeDetail(
        charge_id=charge.id or 0,
        attendance_record_id=charge.attendance_record_id,
        target_date=charge.target_date,
        check_in_at=record.check_in_at if record else None,
        check_out_at=record.check_out_at if record else charge.actual_check_out_at,
        planned_pickup_time=record.planned_pickup_time if record and record.planned_pickup_time else "",
        charge_start_at=charge.charge_start_at,
        extended_minutes=charge.extended_minutes,
        billable_units=charge.billable_units,
        auto_amount=charge.auto_amount,
        adjustment_amount=charge.adjustment_amount,
        final_amount=charge.final_amount,
        status=charge.status,
        status_label=charge_status_label(charge),
        adjustment_reason=charge.adjustment_reason or "",
        warning=warning,
    )
