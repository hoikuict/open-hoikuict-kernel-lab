from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from auth import get_current_staff_user, require_can_edit
from database import get_session
from models import (
    Child,
    Classroom,
    Family,
    QuestionType,
    Survey,
    SurveyAnswer,
    SurveyAnswerUnit,
    SurveyAudienceType,
    SurveyQuestion,
    SurveyStatus,
    SurveyTargetType,
    User,
)
from staff_user_service import list_active_staff_users
from survey_service import (
    answer_value_for_display,
    eligible_staff_users_for_survey,
    load_existing_survey_answer,
    replace_survey_questions,
    replace_survey_target,
    resolve_staff_answer_scope,
    response_by_question,
    sanitize_csv_header_label,
    target_label,
    validate_survey_definition,
)
from time_utils import utc_now


router = APIRouter(prefix="/surveys", tags=["surveys"])
templates = Jinja2Templates(directory="templates")

QUESTION_ROW_COUNT = 6


@router.get("", include_in_schema=False)
def survey_list_without_trailing_slash():
    return RedirectResponse(url="/surveys/", status_code=307)


def _parse_optional_datetime(raw: str) -> Optional[datetime]:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _load_survey(session: Session, survey_id: int) -> Survey:
    survey = session.exec(
        select(Survey)
        .options(
            selectinload(Survey.targets),
            selectinload(Survey.questions).selectinload(SurveyQuestion.options),
            selectinload(Survey.answers).selectinload(SurveyAnswer.responses),
        )
        .where(Survey.id == survey_id)
    ).first()
    if survey is None:
        raise HTTPException(status_code=404, detail="アンケートが見つかりません")
    return survey


def _load_reference_data(session: Session) -> tuple[list[Classroom], list[Child], list[User]]:
    classrooms = session.exec(select(Classroom).order_by(Classroom.display_order, Classroom.id)).all()
    children = session.exec(select(Child).order_by(Child.last_name_kana, Child.first_name_kana)).all()
    users = list_active_staff_users(session)
    return classrooms, children, users


def _normalize_target_type_for_audience(
    audience_type: SurveyAudienceType,
    target_type: SurveyTargetType,
) -> SurveyTargetType:
    if audience_type == SurveyAudienceType.staff and target_type == SurveyTargetType.all:
        return SurveyTargetType.all_staff
    if audience_type == SurveyAudienceType.parent and target_type == SurveyTargetType.all_staff:
        return SurveyTargetType.all
    return target_type


def _target_labels(session: Session, surveys: list[Survey]) -> dict[int, str]:
    classrooms, children, users = _load_reference_data(session)
    classrooms_by_id = {item.id: item for item in classrooms if item.id is not None}
    children_by_id = {item.id: item for item in children if item.id is not None}
    users_by_id = {str(item.id): item for item in users}
    return {
        survey.id: target_label(
            survey,
            classrooms_by_id=classrooms_by_id,
            children_by_id=children_by_id,
            users_by_id=users_by_id,
        )
        for survey in surveys
        if survey.id is not None
    }


def _answer_scope_labels(session: Session, answers: list[SurveyAnswer]) -> dict[int, str]:
    families = {item.id: item for item in session.exec(select(Family)).all()}
    children = {item.id: item for item in session.exec(select(Child)).all()}
    users = {item.id: item for item in session.exec(select(User)).all()}
    labels: dict[int, str] = {}
    for answer in answers:
        if answer.id is None:
            continue
        if answer.family_id is not None:
            family = families.get(answer.family_id)
            labels[answer.id] = f"世帯: {family.family_name}" if family else "世帯"
        elif answer.child_id is not None:
            child = children.get(answer.child_id)
            labels[answer.id] = f"園児: {child.full_name}" if child else "園児"
        elif answer.staff_user_id is not None:
            staff_user = users.get(answer.staff_user_id)
            labels[answer.id] = f"職員: {staff_user.display_name}" if staff_user else "職員"
    return labels


def _question_specs_from_form(
    *,
    q1_label: str,
    q1_type: str,
    q1_required: str,
    q1_options: str,
    q2_label: str,
    q2_type: str,
    q2_required: str,
    q2_options: str,
    q3_label: str,
    q3_type: str,
    q3_required: str,
    q3_options: str,
    q4_label: str,
    q4_type: str,
    q4_required: str,
    q4_options: str,
    q5_label: str,
    q5_type: str,
    q5_required: str,
    q5_options: str,
    q6_label: str,
    q6_type: str,
    q6_required: str,
    q6_options: str,
) -> list[dict]:
    raw_rows = [
        (q1_label, q1_type, q1_required, q1_options),
        (q2_label, q2_type, q2_required, q2_options),
        (q3_label, q3_type, q3_required, q3_options),
        (q4_label, q4_type, q4_required, q4_options),
        (q5_label, q5_type, q5_required, q5_options),
        (q6_label, q6_type, q6_required, q6_options),
    ]
    specs: list[dict] = []
    for label, question_type, required, options in raw_rows:
        specs.append(
            {
                "label": label,
                "question_type": question_type,
                "is_required": required == "on",
                "options": [item.strip() for item in (options or "").splitlines()],
            }
        )
    return specs


def _form_context(
    request: Request,
    session: Session,
    *,
    survey: Survey | None,
    action_url: str,
    submit_label: str,
    errors: list[str] | None = None,
    form_data: dict | None = None,
    current_user=None,
):
    classrooms, children, users = _load_reference_data(session)
    question_rows = []
    if survey and survey.questions:
        for question in sorted(survey.questions, key=lambda item: (item.order, item.id or 0)):
            question_rows.append(
                {
                    "label": question.label,
                    "question_type": question.question_type.value,
                    "is_required": question.is_required,
                    "options": "\n".join(option.label for option in sorted(question.options, key=lambda item: item.order)),
                }
            )
    while len(question_rows) < QUESTION_ROW_COUNT:
        question_rows.append({"label": "", "question_type": QuestionType.text_short.value, "is_required": False, "options": ""})

    selected_target_type = SurveyTargetType.all.value
    selected_target_value = ""
    if survey and survey.targets:
        selected_target_type = survey.targets[0].target_type.value
        selected_target_value = survey.targets[0].target_value or ""

    if form_data:
        question_rows = form_data.get("question_rows", question_rows)
        selected_target_type = form_data.get("target_type", selected_target_type)
        selected_target_value = form_data.get("target_value", selected_target_value)

    return templates.TemplateResponse(
        request,
        "surveys/form.html",
        {
            "request": request,
            "survey": survey,
            "action_url": action_url,
            "submit_label": submit_label,
            "errors": errors or [],
            "current_user": current_user,
            "status_options": list(SurveyStatus),
            "audience_options": list(SurveyAudienceType),
            "answer_unit_options": list(SurveyAnswerUnit),
            "question_type_options": list(QuestionType),
            "classrooms": classrooms,
            "children": children,
            "staff_users": users,
            "question_rows": question_rows[:QUESTION_ROW_COUNT],
            "selected_target_type": selected_target_type,
            "selected_target_value": selected_target_value,
            "form_data": form_data or {},
        },
    )


@router.get("/", response_class=HTMLResponse)
def survey_list(
    request: Request,
    status: str = "",
    audience_type: str = "",
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    statement = (
        select(Survey)
        .options(selectinload(Survey.targets), selectinload(Survey.answers))
        .order_by(Survey.updated_at.desc(), Survey.created_at.desc())
    )
    if status:
        try:
            statement = statement.where(Survey.status == SurveyStatus(status))
        except ValueError:
            pass
    if audience_type:
        try:
            statement = statement.where(Survey.audience_type == SurveyAudienceType(audience_type))
        except ValueError:
            pass
    surveys = session.exec(statement).all()
    labels = _target_labels(session, surveys)
    unanswered_counts: dict[int, int] = {}
    for survey in surveys:
        if survey.audience_type == SurveyAudienceType.staff:
            eligible_users = eligible_staff_users_for_survey(session, survey)
            unanswered_count = 0
            for user in eligible_users:
                scope = resolve_staff_answer_scope(survey, user)
                if scope is None or load_existing_survey_answer(session, survey, scope) is None:
                    unanswered_count += 1
            unanswered_counts[survey.id] = unanswered_count
        else:
            unanswered_counts[survey.id] = 0

    return templates.TemplateResponse(
        request,
        "surveys/list.html",
        {
            "request": request,
            "surveys": surveys,
            "target_labels": labels,
            "unanswered_counts": unanswered_counts,
            "current_user": current_user,
            "selected_status": status,
            "selected_audience_type": audience_type,
            "status_options": list(SurveyStatus),
            "audience_options": list(SurveyAudienceType),
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_survey_form(
    request: Request,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    return _form_context(
        request,
        session,
        survey=None,
        action_url="/surveys/",
        submit_label="作成する",
        current_user=current_user,
    )


@router.post("/")
def create_survey(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    status: str = Form("draft"),
    audience_type: str = Form("parent"),
    answer_unit: str = Form("family"),
    opens_at: str = Form(""),
    closes_at: str = Form(""),
    target_type: str = Form("all"),
    target_classroom_id: str = Form(""),
    target_child_id: str = Form(""),
    target_staff_role: str = Form(""),
    target_staff_user_id: str = Form(""),
    q1_label: str = Form(""),
    q1_type: str = Form("text_short"),
    q1_required: str = Form(""),
    q1_options: str = Form(""),
    q2_label: str = Form(""),
    q2_type: str = Form("text_short"),
    q2_required: str = Form(""),
    q2_options: str = Form(""),
    q3_label: str = Form(""),
    q3_type: str = Form("text_short"),
    q3_required: str = Form(""),
    q3_options: str = Form(""),
    q4_label: str = Form(""),
    q4_type: str = Form("text_short"),
    q4_required: str = Form(""),
    q4_options: str = Form(""),
    q5_label: str = Form(""),
    q5_type: str = Form("text_short"),
    q5_required: str = Form(""),
    q5_options: str = Form(""),
    q6_label: str = Form(""),
    q6_type: str = Form("text_short"),
    q6_required: str = Form(""),
    q6_options: str = Form(""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    try:
        normalized_audience = SurveyAudienceType(audience_type)
    except ValueError:
        normalized_audience = SurveyAudienceType.parent
    try:
        normalized_answer_unit = SurveyAnswerUnit(answer_unit)
    except ValueError:
        normalized_answer_unit = SurveyAnswerUnit.family
    if normalized_audience == SurveyAudienceType.staff:
        normalized_answer_unit = SurveyAnswerUnit.staff_user
    try:
        normalized_status = SurveyStatus(status)
    except ValueError:
        normalized_status = SurveyStatus.draft
    try:
        normalized_target_type = SurveyTargetType(target_type)
    except ValueError:
        normalized_target_type = SurveyTargetType.all if normalized_audience == SurveyAudienceType.parent else SurveyTargetType.all_staff
    normalized_target_type = _normalize_target_type_for_audience(normalized_audience, normalized_target_type)

    target_value = {
        SurveyTargetType.classroom: target_classroom_id,
        SurveyTargetType.child: target_child_id,
        SurveyTargetType.staff_role: target_staff_role,
        SurveyTargetType.staff_user: target_staff_user_id,
    }.get(normalized_target_type, "")
    question_specs = _question_specs_from_form(
        q1_label=q1_label,
        q1_type=q1_type,
        q1_required=q1_required,
        q1_options=q1_options,
        q2_label=q2_label,
        q2_type=q2_type,
        q2_required=q2_required,
        q2_options=q2_options,
        q3_label=q3_label,
        q3_type=q3_type,
        q3_required=q3_required,
        q3_options=q3_options,
        q4_label=q4_label,
        q4_type=q4_type,
        q4_required=q4_required,
        q4_options=q4_options,
        q5_label=q5_label,
        q5_type=q5_type,
        q5_required=q5_required,
        q5_options=q5_options,
        q6_label=q6_label,
        q6_type=q6_type,
        q6_required=q6_required,
        q6_options=q6_options,
    )
    parsed_opens_at = _parse_optional_datetime(opens_at)
    parsed_closes_at = _parse_optional_datetime(closes_at)
    errors = validate_survey_definition(
        title=title,
        audience_type=normalized_audience,
        answer_unit=normalized_answer_unit,
        target_type=normalized_target_type,
        target_value=target_value,
        questions=question_specs,
        opens_at=parsed_opens_at,
        closes_at=parsed_closes_at,
    )
    if errors:
        return _form_context(
            request,
            session,
            survey=None,
            action_url="/surveys/",
            submit_label="作成する",
            errors=errors,
            current_user=current_user,
            form_data={
                "title": title,
                "description": description,
                "status": normalized_status.value,
                "audience_type": normalized_audience.value,
                "answer_unit": normalized_answer_unit.value,
                "opens_at": opens_at,
                "closes_at": closes_at,
                "target_type": normalized_target_type.value,
                "target_value": target_value,
                "question_rows": [
                    {
                        "label": spec.get("label", ""),
                        "question_type": spec.get("question_type", QuestionType.text_short.value),
                        "is_required": spec.get("is_required", False),
                        "options": "\n".join(spec.get("options", [])),
                    }
                    for spec in question_specs
                ],
            },
        )

    survey = Survey(
        title=title.strip(),
        description=description.strip() or None,
        status=normalized_status,
        audience_type=normalized_audience,
        answer_unit=normalized_answer_unit,
        opens_at=parsed_opens_at,
        closes_at=parsed_closes_at,
        created_by=current_user.name,
        updated_by=current_user.name,
    )
    session.add(survey)
    session.flush()
    replace_survey_target(session, survey, normalized_target_type, target_value)
    replace_survey_questions(session, survey, question_specs)
    session.commit()
    return RedirectResponse(url="/surveys/", status_code=303)


@router.get("/{survey_id}", response_class=HTMLResponse)
def survey_detail(
    request: Request,
    survey_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    survey = _load_survey(session, survey_id)
    labels = _target_labels(session, [survey])
    questions = sorted(survey.questions, key=lambda item: (item.order, item.id or 0))
    answers = sorted(survey.answers, key=lambda item: item.submitted_at, reverse=True)
    return templates.TemplateResponse(
        request,
        "surveys/detail.html",
        {
            "request": request,
            "survey": survey,
            "target_label": labels.get(survey.id, "-"),
            "questions": questions,
            "answers": answers,
            "answer_scope_labels": _answer_scope_labels(session, answers),
            "response_by_question": response_by_question,
            "answer_value_for_display": answer_value_for_display,
            "current_user": current_user,
        },
    )


@router.get("/{survey_id}/edit", response_class=HTMLResponse)
def edit_survey_form(
    request: Request,
    survey_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    survey = _load_survey(session, survey_id)
    return _form_context(
        request,
        session,
        survey=survey,
        action_url=f"/surveys/{survey_id}/edit",
        submit_label="更新する",
        current_user=current_user,
    )


@router.post("/{survey_id}/edit")
def update_survey(
    survey_id: int,
    title: str = Form(...),
    description: str = Form(""),
    status: str = Form("draft"),
    opens_at: str = Form(""),
    closes_at: str = Form(""),
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    require_can_edit(current_user)
    survey = _load_survey(session, survey_id)
    try:
        normalized_status = SurveyStatus(status)
    except ValueError:
        normalized_status = survey.status
    survey.title = title.strip()
    survey.description = description.strip() or None
    survey.status = normalized_status
    if not survey.answers:
        survey.opens_at = _parse_optional_datetime(opens_at)
    survey.closes_at = _parse_optional_datetime(closes_at)
    survey.updated_by = current_user.name
    survey.updated_at = utc_now()
    session.add(survey)
    session.commit()
    return RedirectResponse(url=f"/surveys/{survey_id}", status_code=303)


@router.get("/{survey_id}/answers.csv")
def survey_answers_csv(
    survey_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_staff_user),
):
    survey = _load_survey(session, survey_id)
    questions = sorted(survey.questions, key=lambda item: (item.order, item.id or 0))
    families = {item.id: item for item in session.exec(select(Family)).all()}
    children = {item.id: item for item in session.exec(select(Child)).all()}
    users = {item.id: item for item in session.exec(select(User)).all()}

    output = io.StringIO()
    writer = csv.writer(output)
    question_headers = [
        f"Q{question.order}_{question.id}_{sanitize_csv_header_label(question.label)}"
        for question in questions
    ]
    writer.writerow(
        [
            "survey_id",
            "survey_title",
            "audience_type",
            "answer_unit",
            "submitted_at",
            "family_id",
            "family_name",
            "child_id",
            "child_name",
            "staff_user_id",
            "staff_user_name",
            "staff_role",
        ]
        + question_headers
    )
    for answer in sorted(survey.answers, key=lambda item: item.submitted_at):
        family = families.get(answer.family_id)
        child = children.get(answer.child_id)
        staff_user = users.get(answer.staff_user_id)
        responses = response_by_question(answer)
        writer.writerow(
            [
                survey.id,
                survey.title,
                survey.audience_type.value,
                survey.answer_unit.value,
                answer.submitted_at.isoformat() if answer.submitted_at else "",
                answer.family_id or "",
                family.family_name if family else "",
                answer.child_id or "",
                child.full_name if child else "",
                str(answer.staff_user_id) if answer.staff_user_id else "",
                staff_user.display_name if staff_user else "",
                staff_user.staff_role if staff_user else "",
            ]
            + [answer_value_for_display(question, responses.get(question.id)) for question in questions]
        )

    csv_text = "\ufeff" + output.getvalue()
    return StreamingResponse(
        iter([csv_text]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="survey-{survey_id}-answers.csv"'},
    )
