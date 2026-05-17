from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from auth import get_current_staff_user, require_can_edit
from data_transfer_service import (
    build_csv_content,
    build_xlsx_content,
    commit_import,
    dataset_options,
    export_rows,
    get_dataset,
    preview_import,
    template_rows,
)
from database import get_session
from models import ChildStatus, Classroom, DataTransferLog, ParentAccountStatus
from ninka_transfer_service import build_ninka_xlsx_content, default_fiscal_year
from time_utils import utc_now

router = APIRouter(prefix="/data-transfers", tags=["data_transfers"])
templates = Jinja2Templates(directory="templates")

PREVIEW_DIR = Path("storage/data_transfer_previews")


def _split_file_name(file_name: str) -> tuple[str, str]:
    if "." not in file_name:
        raise HTTPException(status_code=404, detail="ファイル形式が指定されていません")
    dataset, extension = file_name.rsplit(".", 1)
    extension = extension.lower()
    if extension not in {"csv", "xlsx"}:
        raise HTTPException(status_code=404, detail="未対応のファイル形式です")
    try:
        get_dataset(dataset)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return dataset, extension


def _download_response(*, rows: list[list[str]], dataset: str, extension: str, filename: str) -> Response:
    definition = get_dataset(dataset)
    if extension == "csv":
        return Response(
            content=build_csv_content(rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return Response(
        content=build_xlsx_content(rows, definition.sheet_name),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _export_filename(dataset: str, extension: str) -> str:
    timestamp = utc_now().strftime("%Y%m%d-%H%M")
    return f"hoikuict-{dataset.replace('_', '-')}-{timestamp}.{extension}"


def _template_filename(dataset: str, extension: str) -> str:
    return f"hoikuict-{dataset.replace('_', '-')}-template.{extension}"


def _recent_logs(session: Session) -> list[DataTransferLog]:
    return session.exec(
        select(DataTransferLog)
        .order_by(DataTransferLog.created_at.desc(), DataTransferLog.id.desc())
        .limit(10)
    ).all()


def _classrooms(session: Session) -> list[Classroom]:
    return session.exec(select(Classroom).order_by(Classroom.display_order, Classroom.id)).all()


def _render_index(
    request: Request,
    session: Session,
    current_user,
    *,
    preview_result=None,
    ninka_error: str = "",
    notice: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        "data_transfers/index.html",
        {
            "request": request,
            "current_user": current_user,
            "datasets": dataset_options(),
            "classrooms": _classrooms(session),
            "child_status_options": list(ChildStatus),
            "parent_status_options": list(ParentAccountStatus),
            "logs": _recent_logs(session),
            "preview_result": preview_result,
            "ninka_default_fiscal_year": default_fiscal_year(),
            "ninka_error": ninka_error,
            "notice": notice,
        },
        status_code=status_code,
    )


def _save_preview_file(dataset: str, filename: str, content: bytes) -> str:
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    payload = {
        "dataset": dataset,
        "filename": filename,
        "content": base64.b64encode(content).decode("ascii"),
    }
    (PREVIEW_DIR / f"{token}.json").write_text(json.dumps(payload), encoding="utf-8")
    return token


def _load_preview_file(token: str, expected_dataset: str) -> tuple[str, bytes]:
    if not token or not token.replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="インポート確認データが見つかりません")
    path = PREVIEW_DIR / f"{token}.json"
    if not path.exists():
        raise HTTPException(status_code=400, detail="インポート確認データが見つかりません")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("dataset") != expected_dataset:
        raise HTTPException(status_code=400, detail="インポート確認データの種類が一致しません")
    content = base64.b64decode(payload["content"])
    return str(payload.get("filename") or f"{expected_dataset}.csv"), content


def _delete_preview_file(token: str) -> None:
    path = PREVIEW_DIR / f"{token}.json"
    if path.exists():
        path.unlink()


@router.get("/", response_class=HTMLResponse)
def data_transfer_page(
    request: Request,
    notice: str = Query(default=""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    return _render_index(request, session, current_user, notice=notice)


@router.get("/templates/{file_name}")
def download_template(
    file_name: str,
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    dataset, extension = _split_file_name(file_name)
    return _download_response(
        rows=template_rows(dataset),
        dataset=dataset,
        extension=extension,
        filename=_template_filename(dataset, extension),
    )


@router.get("/export/{file_name}")
def download_export(
    file_name: str,
    classroom_id: str = Query(default=""),
    status: str = Query(default=""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    dataset, extension = _split_file_name(file_name)
    rows = export_rows(session, dataset, classroom_id=classroom_id, status=status)
    return _download_response(
        rows=rows,
        dataset=dataset,
        extension=extension,
        filename=_export_filename(dataset, extension),
    )


@router.post("/import/{dataset}/preview", response_class=HTMLResponse)
async def preview_import_file(
    request: Request,
    dataset: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    try:
        get_dataset(dataset)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    content = await file.read()
    filename = file.filename or f"{dataset}.csv"
    result = preview_import(session, dataset, filename, content)
    if not result.errors and result.total_rows > 0:
        result.preview_token = _save_preview_file(dataset, filename, content)
    return _render_index(request, session, current_user, preview_result=result)


@router.post("/import/{dataset}/commit", response_class=HTMLResponse)
async def commit_import_file(
    request: Request,
    dataset: str,
    preview_token: str = Form(""),
    file: UploadFile | None = File(None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    try:
        get_dataset(dataset)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if preview_token:
        filename, content = _load_preview_file(preview_token, dataset)
    elif file is not None:
        content = await file.read()
        filename = file.filename or f"{dataset}.csv"
    else:
        raise HTTPException(status_code=400, detail="インポートファイルがありません")

    result = commit_import(session, dataset, filename, content, actor_name=current_user.name)
    if preview_token and not result.errors:
        _delete_preview_file(preview_token)
    if result.errors:
        return _render_index(request, session, current_user, preview_result=result, status_code=400)
    return RedirectResponse(
        url=f"/data-transfers/?notice=imported-{dataset}-{result.create_count}-{result.update_count}",
        status_code=303,
    )


@router.post("/ninka/export")
async def export_ninka_input(
    request: Request,
    template_file: UploadFile = File(...),
    fiscal_year: int | None = Form(None),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    filename = template_file.filename or "ninka_input.xlsx"
    if not filename.lower().endswith(".xlsx"):
        return _render_index(
            request,
            session,
            current_user,
            ninka_error="認可施設帳票の Excel ファイル（.xlsx）を選択してください。",
            status_code=400,
        )

    content = await template_file.read()
    if not content:
        return _render_index(
            request,
            session,
            current_user,
            ninka_error="認可施設帳票ファイルが空です。",
            status_code=400,
        )

    try:
        output, summary = build_ninka_xlsx_content(session, content, fiscal_year=fiscal_year)
    except ValueError as exc:
        return _render_index(request, session, current_user, ninka_error=str(exc), status_code=400)

    export_filename = f"hoikuict-ninka-input-{summary.fiscal_year}-{utc_now().strftime('%Y%m%d-%H%M')}.xlsx"
    return Response(
        content=output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{export_filename}"'},
    )
