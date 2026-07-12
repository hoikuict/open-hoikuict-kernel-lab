from __future__ import annotations

import calendar
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from auth import get_current_staff_user, require_admin, require_can_edit
from billing_calculation_service import BillingCalculationError, recalculate_claim_total, validate_charge_amount
from database import get_session
from models import (
    BillingChargeLine,
    BillingChargeSourceType,
    BillingClaim,
    BillingClaimStatus,
    BillingCycle,
    BillingCycleStatus,
    BillingPaymentMethod,
    BillingSetting,
    Child,
    ChildStatus,
    DirectDebitStatus,
    FamilyBillingProfile,
    FeeItem,
    ZenginExport,
    ZenginExportLine,
)
from time_utils import utc_now
from zengin_service import (
    ParsedResultRecord,
    ZenginError,
    build_data_record,
    build_end_record,
    build_file_bytes,
    build_header_record,
    build_trailer_record,
    create_customer_number,
    create_zengin_export,
    import_result_file,
    mark_zengin_export_submitted,
    supersede_zengin_export,
)

router = APIRouter(prefix="/billing", tags=["billing"])
templates = Jinja2Templates(directory="templates")

TABLE_CHARGE_COLUMNS = [
    {
        "code": "monthly_meal",
        "label": "給食費",
        "description": "給食費（月額）",
        "category": "monthly",
        "category_label": "毎月請求",
        "source_type": BillingChargeSourceType.manual,
        "display_order": 110,
    },
    {
        "code": "monthly_childcare",
        "label": "延長保育料",
        "description": "延長保育料（月額）",
        "category": "monthly",
        "category_label": "毎月請求",
        "source_type": BillingChargeSourceType.manual,
        "display_order": 120,
    },
    {
        "code": "temp_photo",
        "label": "写真代",
        "description": "写真代",
        "category": "temporary",
        "category_label": "単発請求",
        "source_type": BillingChargeSourceType.manual,
        "display_order": 210,
    },
    {
        "code": "temp_material",
        "label": "教材費",
        "description": "教材費",
        "category": "temporary",
        "category_label": "単発請求",
        "source_type": BillingChargeSourceType.manual,
        "display_order": 220,
    },
    {
        "code": "temp_event",
        "label": "行事費",
        "description": "行事費",
        "category": "temporary",
        "category_label": "単発請求",
        "source_type": BillingChargeSourceType.manual,
        "display_order": 230,
    },
    {
        "code": "adjustment",
        "label": "調整額",
        "description": "調整額",
        "category": "adjustment",
        "category_label": "調整",
        "source_type": BillingChargeSourceType.adjustment,
        "display_order": 310,
    },
]

TABLE_CHARGE_GROUPS = [
    {
        "key": "monthly",
        "label": "毎月請求",
        "columns": [column for column in TABLE_CHARGE_COLUMNS if column["category"] == "monthly"],
    },
    {
        "key": "temporary",
        "label": "単発請求",
        "columns": [column for column in TABLE_CHARGE_COLUMNS if column["category"] == "temporary"],
    },
    {
        "key": "adjustment",
        "label": "調整",
        "columns": [column for column in TABLE_CHARGE_COLUMNS if column["category"] == "adjustment"],
    },
]


def _dashboard_redirect(**params: str) -> RedirectResponse:
    query = urlencode({key: value for key, value in params.items() if value})
    suffix = f"?{query}" if query else ""
    return RedirectResponse(url=f"/billing/{suffix}", status_code=303)


def _cycle_redirect(cycle_id: int, *, message: str = "", error: str = "") -> RedirectResponse:
    query = urlencode({key: value for key, value in {"message": message, "error": error}.items() if value})
    suffix = f"?{query}" if query else ""
    return RedirectResponse(url=f"/billing/cycles/{cycle_id}/child-charges{suffix}", status_code=303)


def _child_charge_redirect(cycle_id: int, child_id: int, *, message: str = "", error: str = "") -> RedirectResponse:
    query = urlencode({key: value for key, value in {"message": message, "error": error}.items() if value})
    suffix = f"?{query}" if query else ""
    return RedirectResponse(url=f"/billing/cycles/{cycle_id}/child-charges/{child_id}{suffix}", status_code=303)


def _ensure_manual_fee_item(session: Session, *, code: str = "manual_child", name: str = "園児別手動費用") -> FeeItem:
    item = session.exec(select(FeeItem).where(FeeItem.code == code)).first()
    if item is None:
        item = FeeItem(
            code=code,
            name=name,
            category="other",
            charge_unit="child",
            taxable_type="non_taxable",
            display_order=900,
        )
        session.add(item)
        session.flush()
    return item


def _ensure_table_fee_item(session: Session, column: dict) -> FeeItem:
    item = session.exec(select(FeeItem).where(FeeItem.code == column["code"])).first()
    if item is None:
        item = FeeItem(
            code=column["code"],
            name=column["description"],
            category=column["category"],
            charge_unit="child",
            taxable_type="non_taxable",
            display_order=column["display_order"],
        )
        session.add(item)
        session.flush()
    return item


def _ensure_test_billing_setting(session: Session) -> BillingSetting:
    setting = session.exec(select(BillingSetting).order_by(BillingSetting.id)).first()
    if setting is not None:
        return setting
    setting = BillingSetting(
        facility_name="open-hoikuict テスト園",
        collector_code="1234567890",
        collector_name_kana="HOIKU",
        customer_number_facility_code="001",
        withdrawal_bank_code="0001",
        withdrawal_bank_name_kana="BANK",
        withdrawal_branch_code="001",
        withdrawal_branch_name_kana="BRANCH",
        collector_account_type="1",
        collector_account_number="1234567",
    )
    session.add(setting)
    session.flush()
    return setting


def _ensure_test_billing_profile(session: Session, *, child: Child, setting: BillingSetting) -> FamilyBillingProfile:
    if child.family_id is None:
        raise BillingCalculationError("園児に家族が紐づいていません")
    profile = session.exec(
        select(FamilyBillingProfile).where(FamilyBillingProfile.family_id == child.family_id)
    ).first()
    customer_number = create_customer_number(setting.customer_number_facility_code, child.family_id)
    if profile is None:
        profile = FamilyBillingProfile(
            family_id=child.family_id,
            customer_number=customer_number,
        )
    profile.payment_method = BillingPaymentMethod.direct_debit
    profile.direct_debit_status = DirectDebitStatus.active
    profile.bank_code = profile.bank_code or "0005"
    profile.bank_name_kana = profile.bank_name_kana or "BANK"
    profile.branch_code = profile.branch_code or "123"
    profile.branch_name_kana = profile.branch_name_kana or "BRANCH"
    profile.account_type = profile.account_type or "1"
    profile.account_number = profile.account_number or "7654321"
    profile.account_holder_kana = profile.account_holder_kana or "TANAKA TARO"
    profile.new_code = profile.new_code or "1"
    profile.updated_at = utc_now()
    session.add(profile)
    session.flush()
    return profile


def _next_available_year_month(session: Session) -> str:
    existing = {cycle.year_month for cycle in session.exec(select(BillingCycle)).all()}
    year = 2026
    month = 5
    while True:
        value = f"{year:04d}-{month:02d}"
        if value not in existing:
            return value
        month += 1
        if month > 12:
            year += 1
            month = 1


def _month_bounds(year_month: str) -> tuple[date, date, date]:
    year, month = [int(part) for part in year_month.split("-")]
    last_day = calendar.monthrange(year, month)[1]
    withdrawal_month = month + 1
    withdrawal_year = year
    if withdrawal_month > 12:
        withdrawal_year += 1
        withdrawal_month = 1
    withdrawal_day = min(27, calendar.monthrange(withdrawal_year, withdrawal_month)[1])
    return (
        date(year, month, 1),
        date(year, month, last_day),
        date(withdrawal_year, withdrawal_month, withdrawal_day),
    )


def _create_blank_input_cycle(session: Session, *, created_by: str) -> BillingCycle:
    year_month = _next_available_year_month(session)
    period_start, period_end, withdrawal_date = _month_bounds(year_month)
    cycle = BillingCycle(
        year_month=year_month,
        period_start=period_start,
        period_end=period_end,
        withdrawal_date=withdrawal_date,
        status=BillingCycleStatus.confirmed,
        confirmed_at=utc_now(),
        confirmed_by=created_by,
    )
    session.add(cycle)
    session.flush()
    return cycle


def _ensure_claim_for_child(session: Session, *, cycle: BillingCycle, child: Child) -> BillingClaim:
    if child.family_id is None:
        raise HTTPException(status_code=400, detail="園児に家族が紐づいていません")
    claim = session.exec(
        select(BillingClaim)
        .where(BillingClaim.billing_cycle_id == cycle.id)
        .where(BillingClaim.family_id == child.family_id)
    ).first()
    if claim is None:
        profile = session.exec(
            select(FamilyBillingProfile).where(FamilyBillingProfile.family_id == child.family_id)
        ).first()
        payment_method = profile.payment_method if profile else BillingPaymentMethod.direct_debit
        claim = BillingClaim(
            billing_cycle_id=cycle.id,
            family_id=child.family_id,
            payment_method=payment_method,
            total_amount=0,
            status=BillingClaimStatus.confirmed if cycle.status == BillingCycleStatus.confirmed else BillingClaimStatus.draft,
        )
        session.add(claim)
        session.flush()
    return claim


def _add_child_charge(
    session: Session,
    *,
    cycle: BillingCycle,
    child: Child,
    description: str,
    amount: int,
    quantity: int,
    unit_label: str,
    source_date: date | None,
) -> BillingClaim:
    if cycle.status not in {BillingCycleStatus.draft, BillingCycleStatus.generated, BillingCycleStatus.confirmed}:
        raise BillingCalculationError("Zengin出力後の請求月には明細を追加できません")
    if not description.strip():
        raise BillingCalculationError("内容を入力してください")
    validate_charge_amount(BillingChargeSourceType.manual, amount, allow_zero_note=True)

    fee_item = _ensure_manual_fee_item(session)
    claim = _ensure_claim_for_child(session, cycle=cycle, child=child)
    if claim.status in {BillingClaimStatus.exported, BillingClaimStatus.paid}:
        raise BillingCalculationError("出力済みまたは入金済みの請求には明細を追加できません")

    line = BillingChargeLine(
        billing_claim_id=claim.id,
        fee_item_id=fee_item.id,
        child_id=child.id,
        source_type=BillingChargeSourceType.manual,
        source_date=source_date,
        description=description.strip(),
        quantity=quantity,
        unit_label=unit_label.strip() or "式",
        unit_price=amount,
        amount=amount,
        is_locked=False,
    )
    session.add(line)
    session.flush()
    lines = session.exec(select(BillingChargeLine).where(BillingChargeLine.billing_claim_id == claim.id)).all()
    recalculate_claim_total(claim, lines)
    if cycle.status == BillingCycleStatus.confirmed:
        claim.status = BillingClaimStatus.confirmed
    elif claim.status != BillingClaimStatus.confirmed:
        claim.status = BillingClaimStatus.draft
    claim.updated_at = utc_now()
    session.add(claim)
    return claim


def _parse_optional_date(raw: str | None) -> date | None:
    if not raw:
        return None
    return date.fromisoformat(raw)


def _validate_account_digits(value: str, length: int, label: str) -> str:
    cleaned = value.strip()
    if not cleaned.isdigit() or len(cleaned) != length:
        raise BillingCalculationError(f"{label}は{length}桁の数字で入力してください")
    return cleaned


def _validate_account_number(value: str) -> str:
    cleaned = value.strip()
    if not cleaned.isdigit() or not (1 <= len(cleaned) <= 7):
        raise BillingCalculationError("口座番号は1〜7桁の数字で入力してください")
    return cleaned


def _update_profile_new_code(profile: FamilyBillingProfile, *, previous: dict[str, str | None]) -> None:
    watched_fields = ["bank_code", "branch_code", "account_type", "account_number"]
    if any(previous.get(field) != getattr(profile, field) for field in watched_fields):
        profile.new_code = "2" if profile.direct_debit_status == DirectDebitStatus.active else profile.new_code
        return
    if previous.get("account_holder_kana") != profile.account_holder_kana:
        profile.new_code = "2" if profile.direct_debit_status == DirectDebitStatus.active else profile.new_code


def _parse_table_amount(raw_value: str, *, child: Child, column: dict) -> int:
    cleaned = raw_value.strip().replace(",", "")
    if cleaned == "":
        return 0
    try:
        amount = int(cleaned)
    except ValueError as exc:
        raise BillingCalculationError(f"{child.full_name} の {column['label']} は整数で入力してください") from exc
    if amount < 0 and column["category"] != "adjustment":
        raise BillingCalculationError(f"{child.full_name} の {column['label']} は0円以上で入力してください")
    validate_charge_amount(column["source_type"], amount, allow_zero_note=True)
    return amount


def _upsert_table_charge(
    session: Session,
    *,
    cycle: BillingCycle,
    child: Child,
    column: dict,
    amount: int,
) -> BillingClaim | None:
    if child.family_id is None:
        raise BillingCalculationError(f"{child.full_name} に家族が紐づいていません")

    fee_item = session.exec(select(FeeItem).where(FeeItem.code == column["code"])).first()
    claim = session.exec(
        select(BillingClaim)
        .where(BillingClaim.billing_cycle_id == cycle.id)
        .where(BillingClaim.family_id == child.family_id)
    ).first()

    if amount == 0 and (fee_item is None or claim is None):
        return None

    if claim is None:
        claim = _ensure_claim_for_child(session, cycle=cycle, child=child)
    if claim.status in {BillingClaimStatus.exported, BillingClaimStatus.paid}:
        raise BillingCalculationError(f"{child.full_name} の請求は出力済みまたは入金済みです")

    if fee_item is None:
        fee_item = _ensure_table_fee_item(session, column)

    existing_lines = session.exec(
        select(BillingChargeLine)
        .where(BillingChargeLine.billing_claim_id == claim.id)
        .where(BillingChargeLine.child_id == child.id)
        .where(BillingChargeLine.fee_item_id == fee_item.id)
        .order_by(BillingChargeLine.id)
    ).all()
    if any(line.is_locked for line in existing_lines):
        raise BillingCalculationError(f"{child.full_name} の {column['label']} はロック済みです")

    if amount == 0:
        for line in existing_lines:
            session.delete(line)
        session.flush()
        return claim

    line = existing_lines[0] if existing_lines else None
    if line is None:
        line = BillingChargeLine(
            billing_claim_id=claim.id,
            fee_item_id=fee_item.id,
            child_id=child.id,
            source_type=column["source_type"],
            description=column["description"],
        )
    line.source_type = column["source_type"]
    line.source_date = cycle.period_start
    line.description = column["description"]
    line.quantity = 1
    line.unit_label = "式"
    line.unit_price = amount
    line.amount = amount
    line.is_locked = False
    line.updated_at = utc_now()
    session.add(line)

    for extra_line in existing_lines[1:]:
        session.delete(extra_line)
    session.flush()
    return claim


def _recalculate_touched_claims(session: Session, *, cycle: BillingCycle, claims: list[BillingClaim]) -> None:
    seen_ids: set[int] = set()
    for claim in claims:
        if claim.id is None or claim.id in seen_ids:
            continue
        seen_ids.add(claim.id)
        lines = session.exec(select(BillingChargeLine).where(BillingChargeLine.billing_claim_id == claim.id)).all()
        recalculate_claim_total(claim, lines)
        if cycle.status == BillingCycleStatus.confirmed:
            claim.status = BillingClaimStatus.confirmed
        elif claim.status != BillingClaimStatus.confirmed:
            claim.status = BillingClaimStatus.draft
        claim.updated_at = utc_now()
        session.add(claim)


@router.get("/")
def billing_dashboard(
    request: Request,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    cycles = session.exec(select(BillingCycle).order_by(BillingCycle.year_month.desc())).all()
    claims = session.exec(select(BillingClaim)).all()
    exports = session.exec(select(ZenginExport).order_by(ZenginExport.created_at.desc())).all()

    claims_by_cycle: dict[int, list[BillingClaim]] = {}
    for claim in claims:
        claims_by_cycle.setdefault(claim.billing_cycle_id, []).append(claim)

    exports_by_cycle: dict[int, list[ZenginExport]] = {}
    for export in exports:
        exports_by_cycle.setdefault(export.billing_cycle_id, []).append(export)

    cycle_rows = []
    for cycle in cycles:
        cycle_claims = claims_by_cycle.get(cycle.id or 0, [])
        cycle_exports = exports_by_cycle.get(cycle.id or 0, [])
        cycle_rows.append(
            {
                "cycle": cycle,
                "claims": cycle_claims,
                "claim_count": len(cycle_claims),
                "total_amount": sum(claim.total_amount for claim in cycle_claims),
                "exports": cycle_exports,
            }
        )

    export_rows = []
    for export in exports:
        lines = session.exec(
            select(ZenginExportLine).where(ZenginExportLine.zengin_export_id == export.id)
        ).all()
        export_rows.append({"export": export, "line_count": len(lines)})

    setting = session.exec(select(BillingSetting).order_by(BillingSetting.id)).first()
    profiles = session.exec(select(FamilyBillingProfile).order_by(FamilyBillingProfile.family_id)).all()

    return templates.TemplateResponse(
        request,
        "billing/index.html",
        {
            "request": request,
            "current_user": current_user,
            "cycle_rows": cycle_rows,
            "export_rows": export_rows,
            "setting": setting,
            "profiles": profiles,
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.get("/cycles/{cycle_id}/child-charges")
def child_charge_form(
    cycle_id: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    cycle = session.get(BillingCycle, cycle_id)
    if cycle is None:
        raise HTTPException(status_code=404, detail="請求月が見つかりません")

    children = session.exec(
        select(Child)
        .where(Child.status == ChildStatus.enrolled)
        .order_by(Child.last_name_kana, Child.first_name_kana, Child.id)
    ).all()
    claims = session.exec(select(BillingClaim).where(BillingClaim.billing_cycle_id == cycle_id)).all()
    claims_by_family = {claim.family_id: claim for claim in claims}
    claim_ids = [claim.id for claim in claims if claim.id is not None]
    charge_lines = []
    if claim_ids:
        charge_lines = session.exec(
            select(BillingChargeLine)
            .where(BillingChargeLine.billing_claim_id.in_(claim_ids))
            .order_by(BillingChargeLine.created_at.desc(), BillingChargeLine.id.desc())
        ).all()
    table_codes = [column["code"] for column in TABLE_CHARGE_COLUMNS]
    table_fee_items = session.exec(select(FeeItem).where(FeeItem.code.in_(table_codes))).all()
    table_item_codes_by_id = {item.id: item.code for item in table_fee_items if item.id is not None}
    table_amounts: dict[tuple[int, str], int] = {}
    for line in charge_lines:
        if line.child_id is None:
            continue
        code = table_item_codes_by_id.get(line.fee_item_id)
        if code is None:
            continue
        key = (line.child_id, code)
        table_amounts[key] = table_amounts.get(key, 0) + line.amount

    profiles = session.exec(select(FamilyBillingProfile).order_by(FamilyBillingProfile.family_id)).all()
    profiles_by_family = {profile.family_id: profile for profile in profiles}
    children_by_id = {child.id: child for child in children}
    line_rows = []
    for line in charge_lines:
        child = children_by_id.get(line.child_id)
        claim = next((item for item in claims if item.id == line.billing_claim_id), None)
        line_rows.append({"line": line, "child": child, "claim": claim})

    table_rows = []
    for child in children:
        amounts = {column["code"]: table_amounts.get((child.id, column["code"]), 0) for column in TABLE_CHARGE_COLUMNS}
        claim = claims_by_family.get(child.family_id) if child.family_id is not None else None
        profile = profiles_by_family.get(child.family_id) if child.family_id is not None else None
        table_rows.append(
            {
                "child": child,
                "claim": claim,
                "profile": profile,
                "amounts": amounts,
                "row_total": sum(amounts.values()),
            }
        )

    editable = cycle.status in {
        BillingCycleStatus.draft,
        BillingCycleStatus.generated,
        BillingCycleStatus.confirmed,
    }
    return templates.TemplateResponse(
        request,
        "billing/child_charges.html",
        {
            "request": request,
            "current_user": current_user,
            "cycle": cycle,
            "children": children,
            "claims_by_family": claims_by_family,
            "charge_columns": TABLE_CHARGE_COLUMNS,
            "charge_column_groups": TABLE_CHARGE_GROUPS,
            "table_rows": table_rows,
            "line_rows": line_rows,
            "editable": editable,
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/cycles/{cycle_id}/child-charges/table")
async def update_child_charge_table(
    cycle_id: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    cycle = session.get(BillingCycle, cycle_id)
    if cycle is None:
        raise HTTPException(status_code=404, detail="請求月が見つかりません")
    if cycle.status not in {BillingCycleStatus.draft, BillingCycleStatus.generated, BillingCycleStatus.confirmed}:
        return _cycle_redirect(cycle_id, error="Zengin出力後の請求月は表入力できません")

    children = session.exec(
        select(Child)
        .where(Child.status == ChildStatus.enrolled)
        .order_by(Child.last_name_kana, Child.first_name_kana, Child.id)
    ).all()
    children_by_id = {child.id: child for child in children if child.id is not None}
    form = await request.form()
    touched_claims: list[BillingClaim] = []

    try:
        for child_id, child in children_by_id.items():
            for column in TABLE_CHARGE_COLUMNS:
                field_name = f"amount_{column['code']}_{child_id}"
                if field_name not in form:
                    continue
                raw_value = str(form.get(field_name, ""))
                amount = _parse_table_amount(raw_value, child=child, column=column)
                touched_claim = _upsert_table_charge(
                    session,
                    cycle=cycle,
                    child=child,
                    column=column,
                    amount=amount,
                )
                if touched_claim is not None:
                    touched_claims.append(touched_claim)
        _recalculate_touched_claims(session, cycle=cycle, claims=touched_claims)
        cycle.updated_at = utc_now()
        session.add(cycle)
        session.commit()
    except BillingCalculationError as exc:
        session.rollback()
        return _cycle_redirect(cycle_id, error=str(exc))

    return _cycle_redirect(cycle_id, message="表入力を保存しました")


@router.get("/cycles/{cycle_id}/child-charges/{child_id}")
def child_charge_input(
    cycle_id: int,
    child_id: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    cycle = session.get(BillingCycle, cycle_id)
    if cycle is None:
        raise HTTPException(status_code=404, detail="請求月が見つかりません")
    child = session.get(Child, child_id)
    if child is None:
        raise HTTPException(status_code=404, detail="園児が見つかりません")

    claim = None
    profile = None
    line_rows = []
    if child.family_id is not None:
        profile = session.exec(
            select(FamilyBillingProfile).where(FamilyBillingProfile.family_id == child.family_id)
        ).first()
        claim = session.exec(
            select(BillingClaim)
            .where(BillingClaim.billing_cycle_id == cycle_id)
            .where(BillingClaim.family_id == child.family_id)
        ).first()
        if claim is not None:
            lines = session.exec(
                select(BillingChargeLine)
                .where(BillingChargeLine.billing_claim_id == claim.id)
                .where(BillingChargeLine.child_id == child.id)
                .order_by(BillingChargeLine.created_at.desc(), BillingChargeLine.id.desc())
            ).all()
            line_rows = [{"line": line, "claim": claim} for line in lines]

    editable = cycle.status in {
        BillingCycleStatus.draft,
        BillingCycleStatus.generated,
        BillingCycleStatus.confirmed,
    }
    return templates.TemplateResponse(
        request,
        "billing/child_charge_input.html",
        {
            "request": request,
            "current_user": current_user,
            "cycle": cycle,
            "child": child,
            "profile": profile,
            "claim": claim,
            "line_rows": line_rows,
            "editable": editable,
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/cycles/{cycle_id}/child-charges/{child_id}/profile")
def update_child_billing_profile(
    cycle_id: int,
    child_id: int,
    payment_method: str = Form(...),
    direct_debit_status: str = Form(...),
    bank_code: str = Form(""),
    bank_name_kana: str = Form(""),
    branch_code: str = Form(""),
    branch_name_kana: str = Form(""),
    account_type: str = Form(""),
    account_number: str = Form(""),
    account_holder_kana: str = Form(""),
    new_code: str = Form("0"),
    mandate_received_on: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    cycle = session.get(BillingCycle, cycle_id)
    if cycle is None:
        raise HTTPException(status_code=404, detail="請求月が見つかりません")
    child = session.get(Child, child_id)
    if child is None:
        raise HTTPException(status_code=404, detail="園児が見つかりません")
    if child.family_id is None:
        return _child_charge_redirect(cycle_id, child_id, error="園児に家族が紐づいていません")

    try:
        method = BillingPaymentMethod(payment_method)
        status = DirectDebitStatus(direct_debit_status)
        if new_code not in {"0", "1", "2"}:
            raise BillingCalculationError("新規コードは0, 1, 2のいずれかで入力してください")

        setting = _ensure_test_billing_setting(session)
        profile = session.exec(
            select(FamilyBillingProfile).where(FamilyBillingProfile.family_id == child.family_id)
        ).first()
        if profile is None:
            profile = FamilyBillingProfile(
                family_id=child.family_id,
                customer_number=create_customer_number(setting.customer_number_facility_code, child.family_id),
            )
            previous = {}
        else:
            previous = {
                "bank_code": profile.bank_code,
                "branch_code": profile.branch_code,
                "account_type": profile.account_type,
                "account_number": profile.account_number,
                "account_holder_kana": profile.account_holder_kana,
            }

        profile.payment_method = method
        profile.direct_debit_status = status
        profile.bank_code = _validate_account_digits(bank_code, 4, "銀行コード") if bank_code.strip() else None
        profile.bank_name_kana = bank_name_kana.strip() or None
        profile.branch_code = _validate_account_digits(branch_code, 3, "支店コード") if branch_code.strip() else None
        profile.branch_name_kana = branch_name_kana.strip() or None
        if account_type.strip() and account_type.strip() not in {"1", "2", "3", "9"}:
            raise BillingCalculationError("預金種目は1, 2, 3, 9のいずれかで入力してください")
        profile.account_type = account_type.strip() or None
        profile.account_number = _validate_account_number(account_number) if account_number.strip() else None
        profile.account_holder_kana = account_holder_kana.strip() or None
        profile.new_code = new_code
        profile.mandate_received_on = _parse_optional_date(mandate_received_on)
        profile.note = note.strip() or None
        if not previous and status == DirectDebitStatus.active and profile.new_code == "0":
            profile.new_code = "1"
        elif previous:
            _update_profile_new_code(profile, previous=previous)
        profile.updated_at = utc_now()
        session.add(profile)
        session.commit()
    except (BillingCalculationError, ValueError) as exc:
        return _child_charge_redirect(cycle_id, child_id, error=str(exc))

    return _child_charge_redirect(cycle_id, child_id, message="口座情報を保存しました")


@router.post("/cycles/{cycle_id}/child-charges/{child_id}")
def add_child_charge_for_child(
    cycle_id: int,
    child_id: int,
    description: str = Form(...),
    amount: int = Form(...),
    quantity: int = Form(default=1),
    unit_label: str = Form(default="式"),
    source_date: date | None = Form(default=None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    cycle = session.get(BillingCycle, cycle_id)
    if cycle is None:
        raise HTTPException(status_code=404, detail="請求月が見つかりません")
    child = session.get(Child, child_id)
    if child is None:
        raise HTTPException(status_code=404, detail="園児が見つかりません")
    try:
        _add_child_charge(
            session,
            cycle=cycle,
            child=child,
            description=description,
            amount=amount,
            quantity=quantity,
            unit_label=unit_label,
            source_date=source_date,
        )
    except BillingCalculationError as exc:
        return _child_charge_redirect(cycle_id, child_id, error=str(exc))
    session.commit()
    return _child_charge_redirect(cycle_id, child_id, message="請求明細を追加しました")


@router.post("/child-charges/new-editable-cycle")
def create_editable_cycle_for_table(
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    cycle = _create_blank_input_cycle(session, created_by=current_user.name)
    session.commit()
    return _cycle_redirect(cycle.id, message=f"{cycle.year_month} の新規請求月を作成しました")


@router.post("/child-charges/{child_id}/new-editable-cycle")
def create_editable_cycle_for_child(
    child_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    child = session.get(Child, child_id)
    if child is None:
        raise HTTPException(status_code=404, detail="園児が見つかりません")
    cycle = _create_blank_input_cycle(session, created_by=current_user.name)
    session.commit()
    return _child_charge_redirect(
        cycle.id,
        child.id,
        message=f"{cycle.year_month} の新規請求月を作成しました",
    )


@router.post("/cycles/{cycle_id}/child-charges")
def add_child_charge(
    cycle_id: int,
    child_id: int = Form(...),
    description: str = Form(...),
    amount: int = Form(...),
    quantity: int = Form(default=1),
    unit_label: str = Form(default="式"),
    source_date: date | None = Form(default=None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    cycle = session.get(BillingCycle, cycle_id)
    if cycle is None:
        raise HTTPException(status_code=404, detail="請求月が見つかりません")

    child = session.get(Child, child_id)
    if child is None:
        return _cycle_redirect(cycle_id, error="園児が見つかりません")
    try:
        _add_child_charge(
            session,
            cycle=cycle,
            child=child,
            description=description,
            amount=amount,
            quantity=quantity,
            unit_label=unit_label,
            source_date=source_date,
        )
    except BillingCalculationError as exc:
        return _cycle_redirect(cycle_id, error=str(exc))

    session.commit()
    return _cycle_redirect(cycle_id, message="園児別の請求明細を追加しました")


@router.post("/cycles/new-input")
def create_new_billing_cycle_from_dashboard(
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    cycle = _create_blank_input_cycle(session, created_by=current_user.name)
    session.commit()
    return _cycle_redirect(cycle.id, message=f"{cycle.year_month} の新規請求月を作成しました")


@router.post("/dev/seed")
def seed_demo_billing_data(
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    cycle = _create_blank_input_cycle(session, created_by=current_user.name)
    session.commit()
    return _cycle_redirect(cycle.id, message=f"{cycle.year_month} の新規請求月を作成しました")


@router.post("/zengin/{cycle_id}/create-ui")
def create_zengin_export_from_ui(
    cycle_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    try:
        export = create_zengin_export(session, cycle_id, created_by=current_user.name)
    except ZenginError as exc:
        return _dashboard_redirect(error=str(exc))
    return _dashboard_redirect(message=f"Zengin出力を作成しました: export_id={export.id}")


@router.post("/zengin/exports/{export_id}/mark-submitted-ui")
def mark_zengin_export_submitted_from_ui(
    export_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_admin(current_user)
    try:
        mark_zengin_export_submitted(session, export_id)
    except ZenginError as exc:
        return _dashboard_redirect(error=str(exc))
    return _dashboard_redirect(message=f"銀行提出済みにしました: export_id={export_id}")


@router.post("/zengin/exports/{export_id}/supersede-ui")
def supersede_zengin_export_from_ui(
    export_id: int,
    reason: str = Form(...),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_admin(current_user)
    try:
        replacement = supersede_zengin_export(
            session,
            export_id,
            reason=reason,
            created_by=current_user.name,
        )
    except ZenginError as exc:
        return _dashboard_redirect(error=str(exc))
    return _dashboard_redirect(
        message=f"Zengin出力を差し替えました: export_id={replacement.id}"
    )


@router.post("/zengin/exports/{export_id}/import-paid-demo")
def import_paid_demo_result(
    export_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    export = session.get(ZenginExport, export_id)
    if export is None:
        raise HTTPException(status_code=404, detail="Zengin出力履歴が見つかりません")
    lines = session.exec(
        select(ZenginExportLine)
        .where(ZenginExportLine.zengin_export_id == export_id)
        .order_by(ZenginExportLine.id)
    ).all()
    result_records = [
        ParsedResultRecord(customer_number=line.customer_number, amount=line.amount, result_code="0")
        for line in lines
    ]
    encoding = export.settings_snapshot.get("file_encoding", "cp932")
    records = [build_header_record(export.settings_snapshot, export.withdrawal_date)]
    records.extend(build_data_record(line, encoding) for line in lines)
    records.append(build_trailer_record(lines, encoding, result_records=result_records))
    records.append(build_end_record(encoding))
    file_bytes = build_file_bytes(records, encoding, export.settings_snapshot.get("line_separator", "CRLF"))
    try:
        parsed = import_result_file(session, file_bytes, export_id)
    except ZenginError as exc:
        return _dashboard_redirect(error=str(exc))
    if parsed.errors:
        return _dashboard_redirect(error=" / ".join(parsed.errors))
    return _dashboard_redirect(message=f"疑似結果ファイルを取り込みました: {len(parsed.records)}件")


@router.post("/cycles")
def create_cycle(
    year_month: str = Form(...),
    period_start: date = Form(...),
    period_end: date = Form(...),
    withdrawal_date: date = Form(...),
    due_date: date | None = Form(default=None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    if period_end < period_start:
        raise HTTPException(status_code=400, detail="対象期間が不正です")
    existing = session.exec(select(BillingCycle).where(BillingCycle.year_month == year_month)).first()
    if existing:
        raise HTTPException(status_code=409, detail="同一請求月は既に存在します")
    cycle = BillingCycle(
        year_month=year_month,
        period_start=period_start,
        period_end=period_end,
        withdrawal_date=withdrawal_date,
        due_date=due_date,
    )
    session.add(cycle)
    session.commit()
    session.refresh(cycle)
    return {"id": cycle.id, "year_month": cycle.year_month, "status": cycle.status.value}


@router.post("/cycles/{cycle_id}/confirm")
def confirm_cycle(
    cycle_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    cycle = session.get(BillingCycle, cycle_id)
    if not cycle:
        raise HTTPException(status_code=404, detail="請求月が見つかりません")
    if cycle.status != BillingCycleStatus.generated:
        raise HTTPException(status_code=400, detail="generated状態の請求月のみ確定できます")
    cycle.status = BillingCycleStatus.confirmed
    cycle.confirmed_at = utc_now()
    cycle.confirmed_by = current_user.name
    cycle.updated_at = utc_now()
    session.add(cycle)
    session.commit()
    return {"id": cycle.id, "status": cycle.status.value}


@router.post("/cycles/{cycle_id}/unconfirm")
def unconfirm_cycle(
    cycle_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_admin(current_user)
    cycle = session.get(BillingCycle, cycle_id)
    if not cycle:
        raise HTTPException(status_code=404, detail="請求月が見つかりません")
    if cycle.status != BillingCycleStatus.confirmed:
        raise HTTPException(status_code=400, detail="confirmed状態の請求月のみ確定取消できます")
    cycle.status = BillingCycleStatus.generated
    cycle.confirmed_at = None
    cycle.confirmed_by = None
    cycle.updated_at = utc_now()
    session.add(cycle)
    session.commit()
    return {"id": cycle.id, "status": cycle.status.value}
