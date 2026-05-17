from __future__ import annotations

import uuid

from sqlmodel import Session, select

from models import User


STAFF_USER_SORT_ORDER_LIMIT = 200


def staff_user_identity_key(user: User) -> tuple[int, str, str]:
    return (
        user.staff_sort_order,
        (user.display_name or "").strip(),
        (user.staff_role or "").strip(),
    )


def _staff_user_order_key(user: User) -> tuple[int, str, str]:
    return (user.staff_sort_order, user.display_name or "", user.email or "")


def deduplicate_staff_users(users: list[User]) -> list[User]:
    selected: dict[tuple[int, str, str], User] = {}
    for user in users:
        key = staff_user_identity_key(user)
        current = selected.get(key)
        if current is None or _staff_user_order_key(user) < _staff_user_order_key(current):
            selected[key] = user
    return sorted(selected.values(), key=_staff_user_order_key)


def list_active_staff_users(session: Session, *, deduplicate: bool = True) -> list[User]:
    users = session.exec(
        select(User)
        .where(User.is_active.is_(True), User.staff_sort_order < STAFF_USER_SORT_ORDER_LIMIT)
        .order_by(User.staff_sort_order, User.display_name, User.email)
    ).all()
    return deduplicate_staff_users(users) if deduplicate else users


def equivalent_staff_user_ids(session: Session, staff_user_id: uuid.UUID) -> set[uuid.UUID]:
    user = session.get(User, staff_user_id)
    if user is None:
        return {staff_user_id}

    identity_key = staff_user_identity_key(user)
    return {
        item.id
        for item in list_active_staff_users(session, deduplicate=False)
        if item.id is not None and staff_user_identity_key(item) == identity_key
    } or {staff_user_id}
