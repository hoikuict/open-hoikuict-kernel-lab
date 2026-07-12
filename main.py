import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress
from urllib.parse import urlencode

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse

from database import (
    bootstrap_health_records,
    bootstrap_family_records,
    create_db_and_tables,
    seed_calendar_data,
    seed_classroom_data,
    seed_extended_care_fee_rules,
    seed_parent_portal_data,
    seed_sample_data,
)
from routers.attendance import router as attendance_router
from routers.attendance_checks import router as attendance_checks_router
from routers.billing import router as billing_router
from routers.calendar import mock_login_router as calendar_mock_login_router
from routers.calendar import router as calendar_router
from routers.child_change_requests import router as child_change_requests_router
from routers.children import router as children_router
from routers.child_health import router as child_health_router
from routers.classrooms import router as classrooms_router
from routers.data_transfers import router as data_transfers_router
from routers.data_transfers import _cleanup_stale_previews
from routers.daily_contacts import router as daily_contacts_router
from routers.extended_care_fees import router as extended_care_fees_router
from routers.families import router as families_router
from routers.guardian import router as guardian_router
from routers.meeting_notes import router as meeting_notes_router
from routers.notices import router as notices_router
from routers.parent_accounts import router as parent_accounts_router
from routers.parent_portal import mock_login_router as parent_portal_mock_login_router
from routers.parent_portal import router as parent_portal_router
from routers.staff_auth import mock_login_router as staff_mock_login_router
from routers.staff_auth import router as staff_auth_router
from routers.staff_rooms import router as staff_rooms_router
from routers.staff_surveys import router as staff_surveys_router
from routers.surveys import router as surveys_router
from routers.zengin import router as zengin_router
from plan_docs.runtime import ensure_runtime_files
from plan_docs.routers.bunrei import router as plan_docs_bunrei_router
from plan_docs.routers.documents import router as plan_docs_documents_router
from plan_docs.routers.home import router as plan_docs_home_router
from plan_docs.routers.plans import router as plan_docs_plans_router
from url_utils import safe_internal_redirect
from auth import mock_auth_enabled, require_mock_staff_auth, staff_auth_http_exception_handler
from csrf import CsrfTokenMiddleware, verify_csrf
from security_config import validate_runtime_security
from starlette.exceptions import HTTPException as StarletteHTTPException


def initialize_application() -> None:
    validate_runtime_security()
    _cleanup_stale_previews()
    ensure_runtime_files()
    create_db_and_tables()
    seed_classroom_data()
    seed_extended_care_fee_rules()
    seed_sample_data()
    bootstrap_family_records()
    bootstrap_health_records()
    seed_parent_portal_data()
    seed_calendar_data()


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_application()
    cleanup_task = asyncio.create_task(_preview_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task


async def _preview_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(60 * 60)
        await asyncio.to_thread(_cleanup_stale_previews)


app = FastAPI(
    title="open-hoikuict",
    version="0.1.0",
    lifespan=lifespan,
    dependencies=[Depends(verify_csrf)],
)
app.add_exception_handler(StarletteHTTPException, staff_auth_http_exception_handler)
app.add_middleware(CsrfTokenMiddleware)
app.include_router(classrooms_router)
app.include_router(data_transfers_router)
app.include_router(families_router)
app.include_router(children_router)
app.include_router(child_health_router)
app.include_router(child_change_requests_router)
app.include_router(attendance_router)
app.include_router(attendance_checks_router)
app.include_router(extended_care_fees_router)
app.include_router(billing_router)
app.include_router(guardian_router)
app.include_router(parent_accounts_router)
app.include_router(parent_portal_router)
app.include_router(calendar_router)
app.include_router(staff_auth_router)
app.include_router(meeting_notes_router)
app.include_router(notices_router)
app.include_router(daily_contacts_router)
app.include_router(staff_rooms_router)
app.include_router(surveys_router)
app.include_router(staff_surveys_router)
app.include_router(zengin_router)
if mock_auth_enabled():
    app.include_router(staff_mock_login_router)
    app.include_router(parent_portal_mock_login_router)
    app.include_router(calendar_mock_login_router)
app.include_router(plan_docs_home_router, prefix="/plans")
app.include_router(plan_docs_plans_router, prefix="/plans")
app.include_router(plan_docs_documents_router, prefix="/plans")
app.include_router(plan_docs_bunrei_router, prefix="/plans")

@app.get("/")
def root():
    return RedirectResponse(url="/children")


@app.get("/switch-role", dependencies=[Depends(require_mock_staff_auth)])
def switch_role(redirect: str = "/children"):
    target = safe_internal_redirect(redirect, "/children")
    return RedirectResponse(url=f"/staff/login?{urlencode({'redirect': target})}", status_code=303)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
