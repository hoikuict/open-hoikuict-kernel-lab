from __future__ import annotations

import json
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from auth import get_current_staff_user, require_can_edit
from child_health_service import (
    build_health_attention_labels,
    build_health_check_chart_records,
    build_measurement_chart_payload,
    expired_allergy_count,
    get_or_create_child_health_profile,
    has_priority_management_items,
    health_check_is_stale,
    latest_health_check,
    latest_measurement_summary,
    load_child_allergies,
    load_allergies_for_children,
    load_health_checks_for_children,
    load_health_check_records,
    load_health_profiles_for_children,
    sync_child_extra_data_from_health_records,
    sync_health_records_from_legacy_extra_data,
)
from database import get_session
from models import (
    AllergenCategory,
    AllergySeverity,
    Child,
    ChildAllergy,
    ChildHealthProfile,
    Classroom,
    HealthCheckRecord,
    HealthCheckType,
)
from time_utils import local_today, utc_now

router = APIRouter(tags=["child_health"])
child_router = APIRouter(prefix="/children/{child_id}/health", tags=["child_health"])
templates = Jinja2Templates(directory="templates")


def _load_child(session: Session, child_id: int) -> Child:
    child = session.exec(
        select(Child)
        .options(selectinload(Child.classroom), selectinload(Child.family))
        .where(Child.id == child_id)
    ).first()
    if child is None:
        raise HTTPException(status_code=404, detail="園児が見つかりません")
    return child


def _ensure_health_records(session: Session, child: Child) -> None:
    if sync_health_records_from_legacy_extra_data(session, child):
        session.commit()
        session.refresh(child)


def _parse_optional_date(raw_value: Optional[str]) -> Optional[date]:
    value = str(raw_value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_optional_float(raw_value: Optional[str]) -> Optional[float]:
    value = str(raw_value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_optional_int(raw_value: Optional[str]) -> Optional[int]:
    value = str(raw_value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _checked(raw_value: Optional[str]) -> bool:
    return str(raw_value or "").lower() in {"on", "true", "1", "yes"}


def _normalize_range_key(raw_value: Optional[str]) -> str:
    return raw_value if raw_value in {"1y", "all"} else "1y"


def _normalize_status(raw_value: Optional[str]) -> str:
    return raw_value if raw_value in {"enrolled", "all", "graduated", "withdrawn"} else "enrolled"


def _normalize_attention(raw_value: Optional[str]) -> str:
    return raw_value if raw_value in {"all", "needs_attention"} else "all"


def _matches_health_search(child: Child, search_text: str) -> bool:
    normalized = search_text.strip().lower()
    if not normalized:
        return True

    candidates = [
        child.full_name,
        child.full_name_kana,
        child.classroom.name if child.classroom else "",
        child.family.family_name if child.family else "",
    ]
    return any(normalized in str(candidate or "").lower() for candidate in candidates)


@router.get("/health", response_class=HTMLResponse)
def health_overview(
    request: Request,
    status: str = Query(default="enrolled"),
    attention: str = Query(default="all"),
    classroom_id: Optional[str] = Query(default=None),
    q: str = Query(default=""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    current_status = _normalize_status(status)
    current_attention = _normalize_attention(attention)
    current_classroom_id = _parse_optional_int(classroom_id)

    classrooms = session.exec(select(Classroom).order_by(Classroom.display_order, Classroom.id)).all()
    statement = (
        select(Child)
        .options(selectinload(Child.classroom), selectinload(Child.family))
        .order_by(Child.last_name_kana, Child.first_name_kana)
    )
    if current_status != "all":
        statement = statement.where(Child.status == current_status)
    if current_classroom_id is not None:
        statement = statement.where(Child.classroom_id == current_classroom_id)

    children = session.exec(statement).all()

    changed = False
    for child in children:
        changed = sync_health_records_from_legacy_extra_data(session, child) or changed
    if changed:
        session.commit()
        children = session.exec(statement).all()

    child_ids = [child.id for child in children if child.id is not None]
    profiles_by_child_id = load_health_profiles_for_children(session, child_ids)
    allergies_by_child_id = load_allergies_for_children(session, child_ids, include_inactive=False)
    checks_by_child_id = load_health_checks_for_children(session, child_ids)
    today_date = local_today()

    rows: list[dict[str, object]] = []
    for child in children:
        profile = profiles_by_child_id.get(child.id)
        allergies = allergies_by_child_id.get(child.id, [])
        check_records = checks_by_child_id.get(child.id, [])
        chart_records = build_health_check_chart_records(check_records, range_key="all")
        attention_labels = build_health_attention_labels(
            profile=profile,
            allergies=allergies,
            check_records=check_records,
            today=today_date,
        )
        if current_attention == "needs_attention" and not attention_labels:
            continue
        if not _matches_health_search(child, q):
            continue

        rows.append(
            {
                "child": child,
                "profile": profile,
                "allergies": allergies,
                "active_allergy_count": len(allergies),
                "expired_allergy_count": expired_allergy_count(allergies, today=today_date),
                "latest_record": latest_health_check(check_records),
                "latest_height": latest_measurement_summary(chart_records, "height_cm"),
                "latest_weight": latest_measurement_summary(chart_records, "weight_kg"),
                "health_check_stale": health_check_is_stale(check_records),
                "attention_labels": attention_labels,
            }
        )

    summary = {
        "children_count": len(rows),
        "attention_count": sum(1 for row in rows if row["attention_labels"]),
        "priority_management_count": sum(
            1 for row in rows if has_priority_management_items(row["profile"])
        ),
        "expired_allergy_children_count": sum(1 for row in rows if row["expired_allergy_count"]),
    }

    return templates.TemplateResponse(
        request,
        "health/overview.html",
        {
            "request": request,
            "current_user": current_user,
            "rows": rows,
            "summary": summary,
            "classrooms": classrooms,
            "current_status": current_status,
            "current_attention": current_attention,
            "current_classroom_id": current_classroom_id,
            "current_query": q,
        },
    )


def _profile_form_data(profile: ChildHealthProfile) -> dict[str, object]:
    return {
        "blood_type": profile.blood_type or "",
        "primary_doctor_name": profile.primary_doctor_name or "",
        "primary_doctor_phone": profile.primary_doctor_phone or "",
        "primary_doctor_address": profile.primary_doctor_address or "",
        "hospital_name": profile.hospital_name or "",
        "hospital_phone": profile.hospital_phone or "",
        "has_allergy": profile.has_allergy,
        "has_epipen": profile.has_epipen,
        "has_anaphylaxis": profile.has_anaphylaxis,
        "has_febrile_seizure": profile.has_febrile_seizure,
        "has_nursemaids_elbow": profile.has_nursemaids_elbow,
        "has_medication": profile.has_medication,
        "other_management_items": profile.other_management_items or "",
        "epipen_storage_location": profile.epipen_storage_location or "",
        "medical_history": profile.medical_history or "",
        "disability_info": profile.disability_info or "",
        "current_medications": profile.current_medications or "",
        "breastfed": profile.breastfed,
        "formula_type": profile.formula_type or "",
        "food_texture_level": profile.food_texture_level or "",
        "religious_dietary": profile.religious_dietary or "",
        "other_dietary_restrictions": profile.other_dietary_restrictions or "",
        "developmental_notes": profile.developmental_notes or "",
        "psychological_notes": profile.psychological_notes or "",
        "family_health_notes": profile.family_health_notes or "",
        "other_notes": profile.other_notes or "",
    }


def _allergy_form_data(form_data: Optional[dict[str, object]] = None) -> dict[str, object]:
    if form_data is not None:
        return form_data
    return {
        "allergy_id": "",
        "allergen_category": AllergenCategory.other_food.value,
        "allergen_name": "",
        "severity": AllergySeverity.mild.value,
        "symptoms": "",
        "diagnosis_confirmed": False,
        "diagnosis_date": "",
        "treating_doctor": "",
        "removal_required": True,
        "substitute_food": "",
        "action_plan": "",
        "source_document": "",
        "source_document_date": "",
        "valid_until": "",
        "notes": "",
    }


def _allergy_form_data_from_record(allergy: ChildAllergy) -> dict[str, object]:
    return _allergy_form_data(
        {
            "allergy_id": str(allergy.id or ""),
            "allergen_category": allergy.allergen_category.value,
            "allergen_name": allergy.allergen_name,
            "severity": allergy.severity.value,
            "symptoms": allergy.symptoms or "",
            "diagnosis_confirmed": allergy.diagnosis_confirmed,
            "diagnosis_date": allergy.diagnosis_date.isoformat() if allergy.diagnosis_date else "",
            "treating_doctor": allergy.treating_doctor or "",
            "removal_required": allergy.removal_required,
            "substitute_food": allergy.substitute_food or "",
            "action_plan": allergy.action_plan or "",
            "source_document": allergy.source_document or "",
            "source_document_date": allergy.source_document_date.isoformat() if allergy.source_document_date else "",
            "valid_until": allergy.valid_until.isoformat() if allergy.valid_until else "",
            "notes": allergy.notes or "",
        }
    )


def _check_form_data(form_data: Optional[dict[str, object]] = None) -> dict[str, object]:
    if form_data is not None:
        return form_data
    return {
        "check_type": HealthCheckType.periodic.value,
        "checked_at": local_today().isoformat(),
        "height_cm": "",
        "weight_kg": "",
        "temperature": "",
        "heart_rate": "",
        "respiratory_rate": "",
        "general_condition": "",
        "overall_result": "",
        "doctor_name": "",
        "observer_name": "",
        "requires_followup": False,
        "followup_notes": "",
    }


@child_router.get("", response_class=HTMLResponse)
def health_summary(
    request: Request,
    child_id: int,
    notice: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    child = _load_child(session, child_id)
    _ensure_health_records(session, child)
    profile = get_or_create_child_health_profile(session, child)
    allergies = load_child_allergies(session, child_id, include_inactive=False)
    check_records = load_health_check_records(session, child_id)
    chart_records = build_health_check_chart_records(check_records, range_key="all")
    latest_record = latest_health_check(check_records)
    expired_allergies = [
        allergy for allergy in allergies if allergy.valid_until is not None and allergy.valid_until < local_today()
    ]

    return templates.TemplateResponse(
        request,
        "health/summary.html",
        {
            "request": request,
            "current_user": current_user,
            "child": child,
            "profile": profile,
            "allergies": allergies,
            "expired_allergies": expired_allergies,
            "latest_record": latest_record,
            "latest_height": latest_measurement_summary(chart_records, "height_cm"),
            "latest_weight": latest_measurement_summary(chart_records, "weight_kg"),
            "health_check_stale": health_check_is_stale(check_records),
            "today_date": local_today(),
            "notice": {
                "profile_updated": "健康プロフィールを更新しました。",
                "allergy_created": "アレルギー情報を追加しました。",
                "allergy_updated": "アレルギー情報を更新しました。",
                "check_created": "健康診断記録を追加しました。",
            }.get(notice or "", ""),
        },
    )


@child_router.get("/profile", response_class=HTMLResponse)
def health_profile_form(
    request: Request,
    child_id: int,
    notice: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    child = _load_child(session, child_id)
    _ensure_health_records(session, child)
    profile = get_or_create_child_health_profile(session, child)
    return templates.TemplateResponse(
        request,
        "health/profile.html",
        {
            "request": request,
            "current_user": current_user,
            "child": child,
            "profile": profile,
            "form_data": _profile_form_data(profile),
            "notice": "健康プロフィールを更新しました。" if notice == "updated" else "",
            "form_error": "",
        },
    )


@child_router.post("/profile")
def update_health_profile(
    child_id: int,
    blood_type: str = Form(""),
    primary_doctor_name: str = Form(""),
    primary_doctor_phone: str = Form(""),
    primary_doctor_address: str = Form(""),
    hospital_name: str = Form(""),
    hospital_phone: str = Form(""),
    has_allergy: Optional[str] = Form(None),
    has_epipen: Optional[str] = Form(None),
    has_anaphylaxis: Optional[str] = Form(None),
    has_febrile_seizure: Optional[str] = Form(None),
    has_nursemaids_elbow: Optional[str] = Form(None),
    has_medication: Optional[str] = Form(None),
    other_management_items: str = Form(""),
    epipen_storage_location: str = Form(""),
    medical_history: str = Form(""),
    disability_info: str = Form(""),
    current_medications: str = Form(""),
    breastfed: str = Form(""),
    formula_type: str = Form(""),
    food_texture_level: str = Form(""),
    religious_dietary: str = Form(""),
    other_dietary_restrictions: str = Form(""),
    developmental_notes: str = Form(""),
    psychological_notes: str = Form(""),
    family_health_notes: str = Form(""),
    other_notes: str = Form(""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    child = _load_child(session, child_id)
    profile = get_or_create_child_health_profile(session, child, actor_name=current_user.name)
    now = utc_now()

    def _none_if_blank(value: str) -> Optional[str]:
        cleaned = value.strip()
        return cleaned or None

    profile.blood_type = _none_if_blank(blood_type)
    profile.primary_doctor_name = _none_if_blank(primary_doctor_name)
    profile.primary_doctor_phone = _none_if_blank(primary_doctor_phone)
    profile.primary_doctor_address = _none_if_blank(primary_doctor_address)
    profile.hospital_name = _none_if_blank(hospital_name)
    profile.hospital_phone = _none_if_blank(hospital_phone)
    profile.has_allergy = _checked(has_allergy)
    profile.has_epipen = _checked(has_epipen)
    profile.has_anaphylaxis = _checked(has_anaphylaxis)
    profile.has_febrile_seizure = _checked(has_febrile_seizure)
    profile.has_nursemaids_elbow = _checked(has_nursemaids_elbow)
    profile.has_medication = _checked(has_medication)
    profile.other_management_items = _none_if_blank(other_management_items)
    profile.epipen_storage_location = _none_if_blank(epipen_storage_location)
    profile.medical_history = _none_if_blank(medical_history)
    profile.disability_info = _none_if_blank(disability_info)
    profile.current_medications = _none_if_blank(current_medications)
    if breastfed == "yes":
        profile.breastfed = True
    elif breastfed == "no":
        profile.breastfed = False
    else:
        profile.breastfed = None
    profile.formula_type = _none_if_blank(formula_type)
    profile.food_texture_level = _none_if_blank(food_texture_level)
    profile.religious_dietary = _none_if_blank(religious_dietary)
    profile.other_dietary_restrictions = _none_if_blank(other_dietary_restrictions)
    profile.developmental_notes = _none_if_blank(developmental_notes)
    profile.psychological_notes = _none_if_blank(psychological_notes)
    profile.family_health_notes = _none_if_blank(family_health_notes)
    profile.other_notes = _none_if_blank(other_notes)
    profile.updated_by = current_user.name
    profile.updated_at = now
    session.add(profile)
    sync_child_extra_data_from_health_records(session, child, profile=profile)
    session.commit()
    return RedirectResponse(url=f"/children/{child_id}/health/profile?notice=updated", status_code=303)


@child_router.get("/allergies", response_class=HTMLResponse)
def allergy_list(
    request: Request,
    child_id: int,
    notice: Optional[str] = Query(default=None),
    edit: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    child = _load_child(session, child_id)
    _ensure_health_records(session, child)
    allergies = load_child_allergies(session, child_id, include_inactive=True)
    editing_allergy = next((allergy for allergy in allergies if allergy.id == edit), None) if edit is not None else None
    return templates.TemplateResponse(
        request,
        "health/allergies.html",
        {
            "request": request,
            "current_user": current_user,
            "child": child,
            "allergies": allergies,
            "editing_allergy": editing_allergy,
            "form_data": _allergy_form_data_from_record(editing_allergy) if editing_allergy else _allergy_form_data(),
            "notice": {
                "created": "アレルギー情報を追加しました。",
                "updated": "アレルギー情報を更新しました。",
                "deactivated": "アレルギー情報を解除しました。",
                "reactivated": "アレルギー情報を再有効化しました。",
            }.get(notice or "", ""),
            "form_error": "",
            "allergen_categories": list(AllergenCategory),
            "allergy_severities": list(AllergySeverity),
        },
    )


@child_router.post("/allergies")
def save_allergy(
    request: Request,
    child_id: int,
    allergy_id: str = Form(""),
    allergen_category: str = Form(AllergenCategory.other_food.value),
    allergen_name: str = Form(""),
    severity: str = Form(AllergySeverity.mild.value),
    symptoms: str = Form(""),
    diagnosis_confirmed: Optional[str] = Form(None),
    diagnosis_date: str = Form(""),
    treating_doctor: str = Form(""),
    removal_required: Optional[str] = Form(None),
    substitute_food: str = Form(""),
    action_plan: str = Form(""),
    source_document: str = Form(""),
    source_document_date: str = Form(""),
    valid_until: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    child = _load_child(session, child_id)
    allergy_id_value = _parse_optional_int(allergy_id)
    existing_allergy = None
    if allergy_id_value is not None:
        existing_allergy = session.get(ChildAllergy, allergy_id_value)
        if existing_allergy is None or existing_allergy.child_id != child_id:
            raise HTTPException(status_code=404, detail="アレルギー情報が見つかりません")

    try:
        category_value = AllergenCategory(allergen_category)
        severity_value = AllergySeverity(severity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="アレルギー種別が不正です") from exc

    allergen_name_value = allergen_name.strip()
    diagnosis_date_value = _parse_optional_date(diagnosis_date)
    source_document_date_value = _parse_optional_date(source_document_date)
    valid_until_value = _parse_optional_date(valid_until)

    if not allergen_name_value:
        allergies = load_child_allergies(session, child_id, include_inactive=True)
        return templates.TemplateResponse(
            request,
            "health/allergies.html",
            {
                "request": request,
                "current_user": current_user,
                "child": child,
                "allergies": allergies,
                "editing_allergy": existing_allergy,
                "form_data": _allergy_form_data(
                    {
                        "allergy_id": allergy_id,
                        "allergen_category": allergen_category,
                        "allergen_name": allergen_name,
                        "severity": severity,
                        "symptoms": symptoms,
                        "diagnosis_confirmed": _checked(diagnosis_confirmed),
                        "diagnosis_date": diagnosis_date,
                        "treating_doctor": treating_doctor,
                        "removal_required": _checked(removal_required),
                        "substitute_food": substitute_food,
                        "action_plan": action_plan,
                        "source_document": source_document,
                        "source_document_date": source_document_date,
                        "valid_until": valid_until,
                        "notes": notes,
                    }
                ),
                "notice": "",
                "form_error": "アレルゲン名は必須です。",
                "allergen_categories": list(AllergenCategory),
                "allergy_severities": list(AllergySeverity),
            },
            status_code=400,
        )

    now = utc_now()
    if existing_allergy is None:
        allergy = ChildAllergy(
            child_id=child_id,
            created_by=current_user.name,
            created_at=now,
            is_active=True,
        )
    else:
        allergy = existing_allergy

    allergy.allergen_category = category_value
    allergy.allergen_name = allergen_name_value
    allergy.severity = severity_value
    allergy.symptoms = symptoms.strip() or None
    allergy.diagnosis_confirmed = _checked(diagnosis_confirmed)
    allergy.diagnosis_date = diagnosis_date_value
    allergy.treating_doctor = treating_doctor.strip() or None
    allergy.removal_required = _checked(removal_required)
    allergy.substitute_food = substitute_food.strip() or None
    allergy.action_plan = action_plan.strip() or None
    allergy.source_document = source_document.strip() or None
    allergy.source_document_date = source_document_date_value
    allergy.valid_until = valid_until_value
    allergy.notes = notes.strip() or None
    allergy.updated_by = current_user.name
    allergy.updated_at = now
    session.add(allergy)
    session.flush()
    sync_child_extra_data_from_health_records(session, child)
    session.commit()
    notice_key = "updated" if existing_allergy is not None else "created"
    return RedirectResponse(url=f"/children/{child_id}/health/allergies?notice={notice_key}", status_code=303)


@child_router.post("/allergies/{allergy_id}/deactivate")
def deactivate_allergy(
    child_id: int,
    allergy_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    child = _load_child(session, child_id)
    allergy = session.get(ChildAllergy, allergy_id)
    if allergy is None or allergy.child_id != child_id:
        raise HTTPException(status_code=404, detail="アレルギー情報が見つかりません")

    allergy.is_active = False
    allergy.updated_by = current_user.name
    allergy.updated_at = utc_now()
    session.add(allergy)
    session.flush()
    sync_child_extra_data_from_health_records(session, child)
    session.commit()
    return RedirectResponse(url=f"/children/{child_id}/health/allergies?notice=deactivated", status_code=303)


@child_router.post("/allergies/{allergy_id}/reactivate")
def reactivate_allergy(
    child_id: int,
    allergy_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    child = _load_child(session, child_id)
    allergy = session.get(ChildAllergy, allergy_id)
    if allergy is None or allergy.child_id != child_id:
        raise HTTPException(status_code=404, detail="アレルギー情報が見つかりません")

    allergy.is_active = True
    allergy.updated_by = current_user.name
    allergy.updated_at = utc_now()
    session.add(allergy)
    session.flush()
    sync_child_extra_data_from_health_records(session, child)
    session.commit()
    return RedirectResponse(url=f"/children/{child_id}/health/allergies?notice=reactivated", status_code=303)


@child_router.get("/check-records", response_class=HTMLResponse)
def health_check_list(
    request: Request,
    child_id: int,
    range_key: str = Query(default="1y", alias="range"),
    notice: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    child = _load_child(session, child_id)
    _ensure_health_records(session, child)
    records = load_health_check_records(session, child_id)
    selected_range = _normalize_range_key(range_key)
    chart_records = build_health_check_chart_records(records, range_key=selected_range)
    chart_payload = build_measurement_chart_payload(chart_records)

    return templates.TemplateResponse(
        request,
        "health/check_records.html",
        {
            "request": request,
            "current_user": current_user,
            "child": child,
            "records": records,
            "chart_records": chart_records,
            "selected_range": selected_range,
            "height_summary": latest_measurement_summary(chart_records, "height_cm"),
            "weight_summary": latest_measurement_summary(chart_records, "weight_kg"),
            "chart_payload_json": json.dumps(chart_payload, ensure_ascii=False),
            "check_types": list(HealthCheckType),
            "form_data": _check_form_data(),
            "notice": "健康診断記録を追加しました。" if notice == "created" else "",
            "form_error": "",
        },
    )


@child_router.post("/check-records")
def create_health_check_record(
    request: Request,
    child_id: int,
    check_type: str = Form(HealthCheckType.periodic.value),
    checked_at: str = Form(...),
    height_cm: str = Form(""),
    weight_kg: str = Form(""),
    temperature: str = Form(""),
    heart_rate: str = Form(""),
    respiratory_rate: str = Form(""),
    general_condition: str = Form(""),
    overall_result: str = Form(""),
    doctor_name: str = Form(""),
    observer_name: str = Form(""),
    requires_followup: Optional[str] = Form(None),
    followup_notes: str = Form(""),
    range_key: str = Form("1y"),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    child = _load_child(session, child_id)

    try:
        check_type_value = HealthCheckType(check_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="健診種別が不正です") from exc

    checked_at_value = _parse_optional_date(checked_at)
    height_value = _parse_optional_float(height_cm)
    weight_value = _parse_optional_float(weight_kg)
    temperature_value = _parse_optional_float(temperature)
    heart_rate_value = _parse_optional_int(heart_rate)
    respiratory_rate_value = _parse_optional_int(respiratory_rate)
    selected_range = _normalize_range_key(range_key)

    if checked_at_value is None:
        records = load_health_check_records(session, child_id)
        chart_records = build_health_check_chart_records(records, range_key=selected_range)
        return templates.TemplateResponse(
            request,
            "health/check_records.html",
            {
                "request": request,
                "current_user": current_user,
                "child": child,
                "records": records,
                "chart_records": chart_records,
                "selected_range": selected_range,
                "height_summary": latest_measurement_summary(chart_records, "height_cm"),
                "weight_summary": latest_measurement_summary(chart_records, "weight_kg"),
                "chart_payload_json": json.dumps(
                    build_measurement_chart_payload(chart_records),
                    ensure_ascii=False,
                ),
                "check_types": list(HealthCheckType),
                "form_data": _check_form_data(
                    {
                        "check_type": check_type,
                        "checked_at": checked_at,
                        "height_cm": height_cm,
                        "weight_kg": weight_kg,
                        "temperature": temperature,
                        "heart_rate": heart_rate,
                        "respiratory_rate": respiratory_rate,
                        "general_condition": general_condition,
                        "overall_result": overall_result,
                        "doctor_name": doctor_name,
                        "observer_name": observer_name,
                        "requires_followup": _checked(requires_followup),
                        "followup_notes": followup_notes,
                    }
                ),
                "notice": "",
                "form_error": "測定日は YYYY-MM-DD 形式で入力してください。",
            },
            status_code=400,
        )

    if height_value is not None and not (30.0 <= height_value <= 200.0):
        raise HTTPException(status_code=400, detail="身長は 30.0cm 以上 200.0cm 以下で入力してください")
    if weight_value is not None and not (1.0 <= weight_value <= 100.0):
        raise HTTPException(status_code=400, detail="体重は 1.0kg 以上 100.0kg 以下で入力してください")
    if temperature_value is not None and not (30.0 <= temperature_value <= 45.0):
        raise HTTPException(status_code=400, detail="体温は 30.0℃ 以上 45.0℃ 以下で入力してください")

    now = utc_now()
    session.add(
        HealthCheckRecord(
            child_id=child_id,
            check_type=check_type_value,
            checked_at=checked_at_value,
            height_cm=height_value,
            weight_kg=weight_value,
            temperature=temperature_value,
            heart_rate=heart_rate_value,
            respiratory_rate=respiratory_rate_value,
            general_condition=general_condition.strip() or None,
            overall_result=overall_result.strip() or None,
            doctor_name=doctor_name.strip() or None,
            observer_name=observer_name.strip() or None,
            requires_followup=_checked(requires_followup),
            followup_notes=followup_notes.strip() or None,
            created_by=current_user.name,
            updated_by=current_user.name,
            created_at=now,
            updated_at=now,
        )
    )
    session.commit()
    return RedirectResponse(
        url=f"/children/{child_id}/health/check-records?range={selected_range}&notice=created",
        status_code=303,
    )


router.include_router(child_router)
