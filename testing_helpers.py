import os
from urllib.parse import quote
from uuid import UUID

from auth import (
    MOCK_CALENDAR_USER_COOKIE,
    MOCK_CHILD_RECORDS_PERMISSION_COOKIE,
    MOCK_ROLE_COOKIE,
    MOCK_STAFF_NAME_COOKIE,
    Role,
)

DEFAULT_TEST_STAFF_ID = UUID("00000000-0000-0000-0000-000000000001")


def configure_test_environment() -> None:
    os.environ["HOIKUICT_ENV"] = "development"
    os.environ["HOIKUICT_ENABLE_MOCK_AUTH"] = "1"
    os.environ["HOIKUICT_ENABLE_MOCK_ROLE_OVERRIDE"] = "1"
    os.environ["HOIKUICT_KIOSK_ACCESS_MODE"] = "open"


def authenticate_mock_staff(
    client,
    *,
    role: Role = Role.CAN_EDIT,
    user_id: UUID = DEFAULT_TEST_STAFF_ID,
    name: str = "テスト職員",
    can_manage_child_records: bool = False,
) -> None:
    configure_test_environment()
    client.cookies.set(MOCK_CALENDAR_USER_COOKIE, str(user_id))
    client.cookies.set(MOCK_ROLE_COOKIE, role.value)
    client.cookies.set(MOCK_STAFF_NAME_COOKIE, quote(name, safe=""))
    client.cookies.set(
        MOCK_CHILD_RECORDS_PERMISSION_COOKIE,
        "1" if can_manage_child_records or role == Role.ADMIN else "0",
    )
