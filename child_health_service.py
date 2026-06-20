from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional

from sqlmodel import Session, select

from models import (
    AllergenCategory,
    AllergySeverity,
    Child,
    ChildAllergy,
    ChildHealthProfile,
    HealthCheckRecord,
    HealthCheckType,
)
from time_utils import utc_now

GRAPH_CHECK_TYPES = {HealthCheckType.entrance, HealthCheckType.periodic}


def _normalized_legacy_allergy_names(extra_data: object) -> list[str]:
    if not isinstance(extra_data, dict):
        return []
    raw_value = extra_data.get("allergy", [])
    if isinstance(raw_value, str):
        candidates = raw_value.replace("、", ",").split(",")
    elif isinstance(raw_value, list):
        candidates = raw_value
    else:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _normalized_legacy_medical_notes(extra_data: object) -> Optional[str]:
    if not isinstance(extra_data, dict):
        return None
    value = str(extra_data.get("medical_notes", "") or "").strip()
    return value or None


def get_child_health_profile(session: Session, child_id: int) -> Optional[ChildHealthProfile]:
    return session.exec(
        select(ChildHealthProfile).where(ChildHealthProfile.child_id == child_id)
    ).first()


def get_or_create_child_health_profile(
    session: Session,
    child: Child,
    *,
    actor_name: Optional[str] = None,
) -> ChildHealthProfile:
    profile = get_child_health_profile(session, child.id)
    if profile is not None:
        return profile

    now = utc_now()
    profile = ChildHealthProfile(
        child_id=child.id,
        medical_history=_normalized_legacy_medical_notes(child.extra_data),
        created_by=actor_name,
        updated_by=actor_name,
        created_at=now,
        updated_at=now,
    )
    session.add(profile)
    session.flush()
    return profile


def load_child_allergies(
    session: Session,
    child_id: int,
    *,
    include_inactive: bool = True,
) -> list[ChildAllergy]:
    statement = (
        select(ChildAllergy)
        .where(ChildAllergy.child_id == child_id)
        .order_by(ChildAllergy.is_active.desc(), ChildAllergy.updated_at.desc(), ChildAllergy.id.desc())
    )
    if not include_inactive:
        statement = statement.where(ChildAllergy.is_active.is_(True))
    return session.exec(statement).all()


def load_health_check_records(session: Session, child_id: int) -> list[HealthCheckRecord]:
    return session.exec(
        select(HealthCheckRecord)
        .where(HealthCheckRecord.child_id == child_id)
        .order_by(HealthCheckRecord.checked_at.desc(), HealthCheckRecord.updated_at.desc(), HealthCheckRecord.id.desc())
    ).all()


def load_health_profiles_for_children(
    session: Session,
    child_ids: list[int],
) -> dict[int, ChildHealthProfile]:
    if not child_ids:
        return {}
    profiles = session.exec(
        select(ChildHealthProfile).where(ChildHealthProfile.child_id.in_(child_ids))
    ).all()
    return {profile.child_id: profile for profile in profiles}


def load_allergies_for_children(
    session: Session,
    child_ids: list[int],
    *,
    include_inactive: bool = True,
) -> dict[int, list[ChildAllergy]]:
    if not child_ids:
        return {}
    statement = select(ChildAllergy).where(ChildAllergy.child_id.in_(child_ids)).order_by(
        ChildAllergy.is_active.desc(),
        ChildAllergy.updated_at.desc(),
        ChildAllergy.id.desc(),
    )
    if not include_inactive:
        statement = statement.where(ChildAllergy.is_active.is_(True))
    allergies = session.exec(statement).all()
    grouped: dict[int, list[ChildAllergy]] = {}
    for allergy in allergies:
        grouped.setdefault(allergy.child_id, []).append(allergy)
    return grouped


def load_health_checks_for_children(
    session: Session,
    child_ids: list[int],
) -> dict[int, list[HealthCheckRecord]]:
    if not child_ids:
        return {}
    records = session.exec(
        select(HealthCheckRecord)
        .where(HealthCheckRecord.child_id.in_(child_ids))
        .order_by(
            HealthCheckRecord.checked_at.desc(),
            HealthCheckRecord.updated_at.desc(),
            HealthCheckRecord.id.desc(),
        )
    ).all()
    grouped: dict[int, list[HealthCheckRecord]] = {}
    for record in records:
        grouped.setdefault(record.child_id, []).append(record)
    return grouped


def sync_health_records_from_legacy_extra_data(
    session: Session,
    child: Child,
    *,
    actor_name: Optional[str] = None,
) -> bool:
    profile = get_or_create_child_health_profile(session, child, actor_name=actor_name)
    now = utc_now()
    changed = False

    medical_notes = _normalized_legacy_medical_notes(child.extra_data)
    if profile.medical_history != medical_notes:
        profile.medical_history = medical_notes
        profile.updated_by = actor_name
        profile.updated_at = now
        session.add(profile)
        changed = True

    desired_names = _normalized_legacy_allergy_names(child.extra_data)
    if desired_names:
        existing_allergies = load_child_allergies(session, child.id, include_inactive=True)
        existing_by_name: dict[str, list[ChildAllergy]] = {}
        for allergy in existing_allergies:
            key = allergy.allergen_name.strip()
            existing_by_name.setdefault(key, []).append(allergy)

        for name in desired_names:
            matches = existing_by_name.get(name, [])
            if matches:
                active_match = next((item for item in matches if item.is_active), None)
                if active_match is None:
                    allergy = matches[0]
                    allergy.is_active = True
                    allergy.updated_by = actor_name
                    allergy.updated_at = now
                    session.add(allergy)
                    changed = True
                continue

            session.add(
                ChildAllergy(
                    child_id=child.id,
                    allergen_category=AllergenCategory.other_food,
                    allergen_name=name,
                    severity=AllergySeverity.mild,
                    diagnosis_confirmed=False,
                    is_active=True,
                    created_by=actor_name,
                    updated_by=actor_name,
                    created_at=now,
                    updated_at=now,
                )
            )
            changed = True

    if changed:
        session.flush()
    return changed


def sync_child_extra_data_from_health_records(
    session: Session,
    child: Child,
    *,
    profile: Optional[ChildHealthProfile] = None,
    allergies: Optional[Iterable[ChildAllergy]] = None,
) -> bool:
    profile = profile or get_child_health_profile(session, child.id)
    active_allergies = list(allergies) if allergies is not None else load_child_allergies(
        session,
        child.id,
        include_inactive=False,
    )
    allergy_names = [allergy.allergen_name.strip() for allergy in active_allergies if allergy.allergen_name.strip()]
    extra_data = child.extra_data if isinstance(child.extra_data, dict) else {}
    normalized_extra_data = {
        **extra_data,
        "allergy": allergy_names,
        "medical_notes": (profile.medical_history if profile else None) or "",
    }
    if child.extra_data == normalized_extra_data:
        return False

    child.extra_data = normalized_extra_data
    child.updated_at = utc_now()
    session.add(child)
    session.flush()
    return True


def build_health_check_chart_records(
    records: Iterable[HealthCheckRecord],
    *,
    range_key: str = "1y",
) -> list[HealthCheckRecord]:
    today = date.today()
    since_date = today - timedelta(days=365) if range_key == "1y" else None

    filtered = [
        record
        for record in records
        if record.check_type in GRAPH_CHECK_TYPES and (since_date is None or record.checked_at >= since_date)
    ]
    return sorted(filtered, key=lambda item: (item.checked_at, item.check_type.value, item.id or 0))


def build_measurement_chart_payload(records: Iterable[HealthCheckRecord]) -> dict[str, list[object]]:
    ordered = list(records)
    return {
        "labels": [record.checked_at.isoformat() for record in ordered],
        "height_values": [record.height_cm for record in ordered],
        "weight_values": [record.weight_kg for record in ordered],
        "check_types": [record.check_type.label for record in ordered],
    }


def latest_measurement_summary(records: Iterable[HealthCheckRecord], attribute_name: str) -> dict[str, Optional[object]]:
    values = [
        (record.checked_at, getattr(record, attribute_name))
        for record in records
        if getattr(record, attribute_name) is not None
    ]
    if not values:
        return {"date": None, "value": None, "delta": None}

    latest_date, latest_value = values[-1]
    previous_value = values[-2][1] if len(values) > 1 else None
    delta = latest_value - previous_value if previous_value is not None else None
    return {"date": latest_date, "value": latest_value, "delta": delta}


def latest_health_check(records: Iterable[HealthCheckRecord]) -> Optional[HealthCheckRecord]:
    ordered = sorted(records, key=lambda item: (item.checked_at, item.updated_at, item.id or 0), reverse=True)
    return ordered[0] if ordered else None


def health_check_is_stale(records: Iterable[HealthCheckRecord], *, max_age_days: int = 180) -> bool:
    latest_record = latest_health_check(
        record for record in records if record.check_type in GRAPH_CHECK_TYPES
    )
    if latest_record is None:
        return True
    return (date.today() - latest_record.checked_at).days > max_age_days


def expired_allergy_count(allergies: Iterable[ChildAllergy], *, today: Optional[date] = None) -> int:
    target_day = today or date.today()
    return sum(1 for allergy in allergies if allergy.valid_until is not None and allergy.valid_until < target_day)


def build_health_attention_labels(
    *,
    profile: Optional[ChildHealthProfile],
    allergies: Iterable[ChildAllergy],
    check_records: Iterable[HealthCheckRecord],
    today: Optional[date] = None,
) -> list[str]:
    labels: list[str] = []
    latest_record = latest_health_check(check_records)

    if profile and profile.requires_medical_care:
        labels.append("医療的ケア")
    if profile and profile.epipen_required:
        labels.append("エピペン")
    if expired_allergy_count(allergies, today=today):
        labels.append("アレルギー期限切れ")
    if latest_record and latest_record.requires_followup:
        labels.append("要フォロー")
    if health_check_is_stale(check_records):
        labels.append("健診要確認")

    return labels
