from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from auth import get_current_staff_user, get_current_staff_user_record
from database import get_session
from models import Survey, SurveyAnswer, SurveyAudienceType, SurveyQuestion, SurveyStatus
from staff_user_service import equivalent_staff_user_ids
from survey_service import (
    answer_value_for_display,
    closes_soon,
    load_existing_survey_answer,
    resolve_staff_answer_scope,
    response_by_question,
    save_survey_answer,
    survey_is_open,
    survey_matches_staff_targets,
)
from time_utils import utc_now


router = APIRouter(prefix="/staff-surveys", tags=["staff_surveys"])
templates = Jinja2Templates(directory="templates")


@router.get("", include_in_schema=False)
def staff_survey_list_without_trailing_slash():
    return RedirectResponse(url="/staff-surveys/", status_code=307)


def _login_redirect(request: Request) -> RedirectResponse:
    redirect_to = quote(request.url.path, safe="/")
    return RedirectResponse(url=f"/staff/login?redirect={redirect_to}", status_code=303)


def _load_current_staff_user(request: Request, session: Session):
    return get_current_staff_user_record(request, session)


def _load_staff_survey(session: Session, survey_id: int) -> Survey | None:
    return session.exec(
        select(Survey)
        .options(
            selectinload(Survey.targets),
            selectinload(Survey.questions).selectinload(SurveyQuestion.options),
            selectinload(Survey.answers).selectinload(SurveyAnswer.responses),
        )
        .where(Survey.id == survey_id)
    ).first()


def _equivalent_staff_user_id_strings(session: Session, staff_user) -> set[str]:
    return {str(user_id) for user_id in equivalent_staff_user_ids(session, staff_user.id)}


def _staff_can_access_survey(session: Session, survey: Survey, staff_user) -> bool:
    return survey_is_open(survey, utc_now()) and survey_matches_staff_targets(
        survey,
        staff_user,
        _equivalent_staff_user_id_strings(session, staff_user),
    )


def _visible_staff_surveys(session: Session, staff_user) -> list[Survey]:
    surveys = session.exec(
        select(Survey)
        .options(
            selectinload(Survey.targets),
            selectinload(Survey.questions).selectinload(SurveyQuestion.options),
            selectinload(Survey.answers),
        )
        .where(
            Survey.audience_type == SurveyAudienceType.staff,
            Survey.status == SurveyStatus.published,
        )
        .order_by(Survey.closes_at, Survey.updated_at.desc())
    ).all()
    return [
        survey
        for survey in surveys
        if _staff_can_access_survey(session, survey, staff_user)
    ]


@router.get("/", response_class=HTMLResponse)
def staff_survey_list(
    request: Request,
    notice: str = "",
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    staff_user = _load_current_staff_user(request, session)
    if staff_user is None:
        return _login_redirect(request)

    cards = []
    for survey in _visible_staff_surveys(session, staff_user):
        scope = resolve_staff_answer_scope(survey, staff_user)
        answer = load_existing_survey_answer(session, survey, scope) if scope else None
        is_answered = answer is not None
        cards.append(
            {
                "survey": survey,
                "answer": answer,
                "is_answered": is_answered,
                "closes_soon": closes_soon(survey) and not is_answered,
            }
        )

    return templates.TemplateResponse(
        request,
        "staff_surveys/list.html",
        {
            "request": request,
            "current_user": current_user,
            "staff_user": staff_user,
            "cards": cards,
            "notice": "回答しました。" if notice == "saved" else "",
        },
    )


@router.get("/{survey_id}", response_class=HTMLResponse)
def staff_survey_form(
    request: Request,
    survey_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    staff_user = _load_current_staff_user(request, session)
    if staff_user is None:
        return _login_redirect(request)

    survey = _load_staff_survey(session, survey_id)
    if not survey or not _staff_can_access_survey(session, survey, staff_user):
        raise HTTPException(status_code=404, detail="アンケートが見つかりません")
    scope = resolve_staff_answer_scope(survey, staff_user)
    answer = load_existing_survey_answer(session, survey, scope) if scope else None

    return templates.TemplateResponse(
        request,
        "staff_surveys/form.html",
        {
            "request": request,
            "current_user": current_user,
            "staff_user": staff_user,
            "survey": survey,
            "questions": sorted(survey.questions, key=lambda item: (item.order, item.id or 0)),
            "answer": answer,
            "responses": response_by_question(answer),
            "errors": [],
            "answer_value_for_display": answer_value_for_display,
        },
    )


@router.post("/{survey_id}")
async def save_staff_survey_answer(
    request: Request,
    survey_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    staff_user = _load_current_staff_user(request, session)
    if staff_user is None:
        return _login_redirect(request)

    survey = _load_staff_survey(session, survey_id)
    if not survey or not _staff_can_access_survey(session, survey, staff_user):
        raise HTTPException(status_code=404, detail="アンケートが見つかりません")
    scope = resolve_staff_answer_scope(survey, staff_user)
    if scope is None:
        raise HTTPException(status_code=404, detail="アンケートが見つかりません")

    form_data = await request.form()
    result = save_survey_answer(
        session,
        survey=survey,
        scope=scope,
        form_data=form_data,
        staff_user=staff_user,
    )
    if result.errors:
        answer = load_existing_survey_answer(session, survey, scope)
        return templates.TemplateResponse(
            request,
            "staff_surveys/form.html",
            {
                "request": request,
                "current_user": current_user,
                "staff_user": staff_user,
                "survey": survey,
                "questions": sorted(survey.questions, key=lambda item: (item.order, item.id or 0)),
                "answer": answer,
                "responses": response_by_question(answer),
                "errors": result.errors,
                "answer_value_for_display": answer_value_for_display,
            },
            status_code=400,
        )

    session.commit()
    return RedirectResponse(url="/staff-surveys/?notice=saved", status_code=303)
