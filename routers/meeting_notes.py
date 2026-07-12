import base64
import binascii
import logging
from collections import defaultdict
from typing import DefaultDict

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlmodel import Session, select

from auth import get_current_staff_user, require_can_edit, resolve_staff_principal
from database import get_session
from models import MeetingNote
from time_utils import utc_now
from security_config import websocket_origin_allowed

router = APIRouter(prefix="/meeting-notes", tags=["meeting_notes"])
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)


class SaveMeetingNotePayload(BaseModel):
    title: str
    content_base64: str


class MeetingNoteConnectionManager:
    def __init__(self) -> None:
        self._connections: DefaultDict[int, list[WebSocket]] = defaultdict(list)

    async def connect(self, note_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[note_id].append(websocket)

    def disconnect(self, note_id: int, websocket: WebSocket) -> None:
        connections = self._connections.get(note_id)
        if not connections:
            return
        if websocket in connections:
            connections.remove(websocket)
        if not connections and note_id in self._connections:
            del self._connections[note_id]

    async def broadcast(self, note_id: int, message: bytes, exclude: WebSocket | None = None) -> None:
        stale_connections: list[WebSocket] = []
        for connection in list(self._connections.get(note_id, [])):
            if connection is exclude:
                continue
            try:
                await connection.send_bytes(message)
            except Exception:
                stale_connections.append(connection)
        for connection in stale_connections:
            self.disconnect(note_id, connection)


manager = MeetingNoteConnectionManager()


def _display_name(current_user) -> str:
    name = getattr(current_user, "name", "") or ""
    if name.strip():
        return name.strip()
    role_label = getattr(current_user, "role_label", "") or ""
    if role_label.strip():
        return role_label.strip()
    return "スタッフ"


def _load_meeting_note(session: Session, note_id: int) -> MeetingNote:
    note = session.get(MeetingNote, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="議事録が見つかりません")
    return note


@router.get("/", response_class=HTMLResponse)
def meeting_note_list(
    request: Request,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    notes = session.exec(select(MeetingNote).order_by(MeetingNote.updated_at.desc(), MeetingNote.id.desc())).all()
    return templates.TemplateResponse(
        request,
        "meeting_notes/list.html",
        {
            "notes": notes,
            "current_user": current_user,
        },
    )


@router.post("/")
def create_meeting_note(
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
    title: str = Form("無題の議事録"),
):
    require_can_edit(current_user)
    display_name = _display_name(current_user)
    note = MeetingNote(
        title=(title or "").strip() or "無題の議事録",
        created_by=display_name,
        updated_by=display_name,
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return RedirectResponse(url=f"/meeting-notes/{note.id}", status_code=303)


@router.get("/{note_id}", response_class=HTMLResponse)
def meeting_note_detail(
    request: Request,
    note_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    note = _load_meeting_note(session, note_id)
    return templates.TemplateResponse(
        request,
        "meeting_notes/detail.html",
        {
            "note": note,
            "current_user": current_user,
            "editor_user_name": _display_name(current_user),
        },
    )


@router.get("/api/{note_id}/content")
def meeting_note_content(
    note_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    _ = current_user
    note = _load_meeting_note(session, note_id)
    if not note.content:
        return {"content_base64": None}
    return {"content_base64": base64.b64encode(note.content).decode("utf-8")}


@router.post("/api/{note_id}/save")
def save_meeting_note(
    note_id: int,
    payload: SaveMeetingNotePayload,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    note = _load_meeting_note(session, note_id)
    try:
        decoded_content = base64.b64decode(payload.content_base64 or "", validate=True)
    except binascii.Error as exc:
        raise HTTPException(status_code=400, detail="content_base64 が不正です") from exc

    note.title = (payload.title or "").strip() or "無題の議事録"
    note.content = decoded_content
    note.updated_at = utc_now()
    note.updated_by = _display_name(current_user)
    session.add(note)
    session.commit()
    return {"status": "ok"}


@router.websocket("/ws/{note_id}")
async def meeting_note_websocket(
    websocket: WebSocket,
    note_id: int,
    session: Session = Depends(get_session),
):
    principal = resolve_staff_principal(websocket)
    if principal is None:
        logger.warning("Meeting note WebSocket rejected: unauthenticated note_id=%s", note_id)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    if not principal.can_edit:
        logger.warning("Meeting note WebSocket rejected: insufficient permission note_id=%s", note_id)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    if not websocket_origin_allowed(websocket):
        logger.warning("Meeting note WebSocket rejected: origin policy note_id=%s", note_id)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    try:
        note_exists = session.get(MeetingNote, note_id) is not None
    finally:
        # Release the DB connection before entering the long-lived receive loop.
        session.close()
    if not note_exists:
        logger.warning("Meeting note WebSocket rejected: unknown note_id=%s", note_id)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(note_id, websocket)
    try:
        while True:
            data = await websocket.receive_bytes()
            await manager.broadcast(note_id, data, exclude=websocket)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(note_id, websocket)
