from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlmodel import Session

from auth import get_current_staff_user, require_admin, require_can_edit
from database import get_session
from zengin_service import (
    ZenginError,
    ZenginFileValidationError,
    build_zengin_file,
    create_zengin_export,
    import_result_file,
    mark_zengin_export_submitted,
    supersede_zengin_export,
)

router = APIRouter(prefix="/billing/zengin", tags=["zengin"])


@router.post("/{cycle_id}/create")
def create_export(
    cycle_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    try:
        export = create_zengin_export(session, cycle_id, created_by=current_user.name)
    except ZenginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": export.id,
        "file_name": export.file_name,
        "total_count": export.total_count,
        "total_amount": export.total_amount,
        "content_hash": export.content_hash,
    }


@router.get("/exports/{export_id}/download")
def download_export(
    export_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_admin(current_user)
    try:
        file_bytes = build_zengin_file(session, export_id)
    except ZenginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=file_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="zengin_{export_id}.txt"'},
    )


@router.post("/exports/{export_id}/mark-submitted")
def mark_export_submitted(
    export_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_admin(current_user)
    try:
        export = mark_zengin_export_submitted(session, export_id)
    except ZenginError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": export.id, "status": export.status.value}


@router.post("/exports/{export_id}/supersede")
def supersede_export(
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
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "superseded_export_id": export_id,
        "replacement_export_id": replacement.id,
        "status": replacement.status.value,
    }


@router.post("/exports/{export_id}/results")
async def import_results(
    export_id: int,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    file_bytes = await file.read()
    try:
        parsed = import_result_file(session, file_bytes, export_id)
    except ZenginFileValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": "結果ファイルが不正です", "errors": exc.errors},
        )
    except ZenginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if parsed.errors:
        return JSONResponse(
            status_code=422,
            content={"detail": "結果ファイルが不正です", "errors": parsed.errors},
        )
    return {
        "records": len(parsed.records),
        "errors": parsed.errors,
        "warnings": parsed.warnings,
    }
