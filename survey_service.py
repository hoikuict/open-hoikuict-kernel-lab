from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
import unicodedata
from uuid import uuid4

from sqlmodel import Session, select

from models import (
    Child,
    Classroom,
    Family,
    ParentAccount,
    ParentChildLink,
    QuestionType,
    Survey,
    SurveyAnswer,
    SurveyAnswerUnit,
    SurveyAudienceType,
    SurveyQuestion,
    SurveyQuestionOption,
    SurveyResponse,
    SurveyStatus,
    SurveyTargetType,
    User,
)
from staff_user_service import equivalent_staff_user_ids, list_active_staff_users
from time_utils import ensure_utc_from_local, utc_now


@dataclass(frozen=True)
class SurveyAnswerScope:
    family_id: int | None = None
    child_id: int | None = None
    staff_user_id: object | None = None

    @property
    def is_valid(self) -> bool:
        return sum(value is not None for value in (self.family_id, self.child_id, self.staff_user_id)) == 1


@dataclass(frozen=True)
class SurveyFormResult:
    answer: SurveyAnswer | None
    errors: list[str]


def generate_option_key() -> str:
    return uuid4().hex


def survey_is_open(survey: Survey, now: datetime | None = None) -> bool:
    current = now or utc_now()
    if survey.status != SurveyStatus.published:
        return False
    opens_at = ensure_utc_from_local(survey.opens_at)
    closes_at = ensure_utc_from_local(survey.closes_at)
    if opens_at and opens_at > current:
        return False
    if closes_at and closes_at < current:
        return False
    return True


def closes_soon(survey: Survey, now: datetime | None = None) -> bool:
    current = now or utc_now()
    closes_at = ensure_utc_from_local(survey.closes_at)
    return bool(closes_at and current <= closes_at <= current + timedelta(days=3))


def _parse_optional_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def linked_children(parent_account: ParentAccount) -> list[Child]:
    if parent_account.family and parent_account.family.children:
        children = list(parent_account.family.children)
    else:
        children = [link.child for link in parent_account.child_links if link.child is not None]

    unique_children: dict[int, Child] = {}
    for child in children:
        if child.id is not None:
            unique_children[child.id] = child
    return sorted(
        unique_children.values(),
        key=lambda child: (
            child.classroom.display_order if child.classroom else 999,
            child.last_name_kana,
            child.first_name_kana,
        ),
    )


def _parent_child_ids(parent_account: ParentAccount) -> set[int]:
    return {child.id for child in linked_children(parent_account) if child.id is not None}


def _parent_classroom_ids(parent_account: ParentAccount) -> set[int]:
    return {
        child.classroom_id
        for child in linked_children(parent_account)
        if child.classroom_id is not None
    }


def survey_matches_parent_targets(survey: Survey, parent_account: ParentAccount) -> bool:
    if survey.audience_type != SurveyAudienceType.parent:
        return False
    child_ids = _parent_child_ids(parent_account)
    classroom_ids = _parent_classroom_ids(parent_account)

    for target in survey.targets:
        if target.target_type == SurveyTargetType.all:
            return True
        if target.target_type == SurveyTargetType.classroom:
            value = _parse_optional_int(target.target_value)
            if value is not None and value in classroom_ids:
                return True
        if target.target_type == SurveyTargetType.child:
            value = _parse_optional_int(target.target_value)
            if value is not None and value in child_ids:
                return True
    return False


def eligible_children_for_survey(survey: Survey, parent_account: ParentAccount) -> list[Child]:
    children = linked_children(parent_account)
    if survey.audience_type != SurveyAudienceType.parent:
        return []
    if not survey.targets:
        return []

    eligible: dict[int, Child] = {}
    for target in survey.targets:
        if target.target_type == SurveyTargetType.all:
            for child in children:
                if child.id is not None:
                    eligible[child.id] = child
        elif target.target_type == SurveyTargetType.classroom:
            classroom_id = _parse_optional_int(target.target_value)
            if classroom_id is None:
                continue
            for child in children:
                if child.id is not None and child.classroom_id == classroom_id:
                    eligible[child.id] = child
        elif target.target_type == SurveyTargetType.child:
            child_id = _parse_optional_int(target.target_value)
            if child_id is None:
                continue
            for child in children:
                if child.id == child_id:
                    eligible[child.id] = child
    return [eligible[key] for key in sorted(eligible)]


def resolve_parent_answer_scope(
    survey: Survey,
    parent_account: ParentAccount,
    child_id: int | None = None,
) -> SurveyAnswerScope | None:
    if survey.audience_type != SurveyAudienceType.parent:
        return None
    if survey.answer_unit == SurveyAnswerUnit.family:
        if parent_account.family_id is not None:
            return SurveyAnswerScope(family_id=parent_account.family_id)
        family_ids = {
            child.family_id
            for child in eligible_children_for_survey(survey, parent_account)
            if child.family_id is not None
        }
        if len(family_ids) == 1:
            return SurveyAnswerScope(family_id=next(iter(family_ids)))
        return None

    if survey.answer_unit == SurveyAnswerUnit.child:
        eligible = eligible_children_for_survey(survey, parent_account)
        if child_id is None:
            return None
        if any(child.id == child_id for child in eligible):
            return SurveyAnswerScope(child_id=child_id)
    return None


def survey_matches_staff_targets(
    survey: Survey,
    staff_user: User,
    equivalent_user_ids: set[str] | None = None,
) -> bool:
    if survey.audience_type != SurveyAudienceType.staff:
        return False
    target_staff_user_ids = equivalent_user_ids or {str(staff_user.id)}
    for target in survey.targets:
        if target.target_type in {SurveyTargetType.all, SurveyTargetType.all_staff}:
            return True
        if target.target_type == SurveyTargetType.staff_role and target.target_value == staff_user.staff_role:
            return True
        if target.target_type == SurveyTargetType.staff_user and target.target_value in target_staff_user_ids:
            return True
    return False


def eligible_staff_users_for_survey(session: Session, survey: Survey) -> list[User]:
    users = list_active_staff_users(session)
    return [
        user
        for user in users
        if survey_matches_staff_targets(
            survey,
            user,
            {str(user_id) for user_id in equivalent_staff_user_ids(session, user.id)},
        )
    ]


def resolve_staff_answer_scope(survey: Survey, staff_user: User) -> SurveyAnswerScope | None:
    if survey.audience_type != SurveyAudienceType.staff or survey.answer_unit != SurveyAnswerUnit.staff_user:
        return None
    return SurveyAnswerScope(staff_user_id=staff_user.id)


def load_existing_survey_answer(
    session: Session,
    survey: Survey,
    scope: SurveyAnswerScope,
) -> SurveyAnswer | None:
    if scope.family_id is not None:
        return session.exec(
            select(SurveyAnswer).where(
                SurveyAnswer.survey_id == survey.id,
                SurveyAnswer.family_id == scope.family_id,
            )
        ).first()
    if scope.child_id is not None:
        return session.exec(
            select(SurveyAnswer).where(
                SurveyAnswer.survey_id == survey.id,
                SurveyAnswer.child_id == scope.child_id,
            )
        ).first()
    if scope.staff_user_id is not None:
        answer = session.exec(
            select(SurveyAnswer).where(
                SurveyAnswer.survey_id == survey.id,
                SurveyAnswer.staff_user_id == scope.staff_user_id,
            )
        ).first()
        if answer is not None:
            return answer

        staff_user_ids = equivalent_staff_user_ids(session, scope.staff_user_id)
        if len(staff_user_ids) <= 1:
            return None
        return session.exec(
            select(SurveyAnswer).where(
                SurveyAnswer.survey_id == survey.id,
                SurveyAnswer.staff_user_id.in_(staff_user_ids),
            )
        ).first()
    return None


def response_by_question(answer: SurveyAnswer | None) -> dict[int, SurveyResponse]:
    if not answer:
        return {}
    return {response.question_id: response for response in answer.responses}


def validate_survey_definition(
    *,
    title: str,
    audience_type: SurveyAudienceType,
    answer_unit: SurveyAnswerUnit,
    target_type: SurveyTargetType,
    target_value: str | None,
    questions: list[dict],
    opens_at: datetime | None = None,
    closes_at: datetime | None = None,
) -> list[str]:
    errors: list[str] = []
    if not title.strip():
        errors.append("タイトルを入力してください。")
    if len(title.strip()) > 255:
        errors.append("タイトルは255文字以内で入力してください。")
    if opens_at and closes_at and ensure_utc_from_local(opens_at) >= ensure_utc_from_local(closes_at):
        errors.append("公開開始は公開終了より前にしてください。")

    if audience_type == SurveyAudienceType.parent:
        if answer_unit not in {SurveyAnswerUnit.family, SurveyAnswerUnit.child}:
            errors.append("保護者向けアンケートの回答単位が不正です。")
        if target_type not in {SurveyTargetType.all, SurveyTargetType.classroom, SurveyTargetType.child}:
            errors.append("保護者向けアンケートの対象種別が不正です。")
    elif audience_type == SurveyAudienceType.staff:
        if answer_unit != SurveyAnswerUnit.staff_user:
            errors.append("職員向けアンケートの回答単位は職員単位にしてください。")
        if target_type not in {SurveyTargetType.all_staff, SurveyTargetType.staff_role, SurveyTargetType.staff_user}:
            errors.append("職員向けアンケートの対象種別が不正です。")

    if target_type in {SurveyTargetType.all, SurveyTargetType.all_staff} and (target_value or "").strip():
        errors.append("全体配信では対象値を指定しないでください。")
    if target_type in {SurveyTargetType.classroom, SurveyTargetType.child, SurveyTargetType.staff_role, SurveyTargetType.staff_user}:
        if not (target_value or "").strip():
            errors.append("対象を選択してください。")

    normalized_questions = [question for question in questions if str(question.get("label", "")).strip()]
    if not normalized_questions:
        errors.append("質問を1件以上入力してください。")
    for index, question in enumerate(normalized_questions, start=1):
        try:
            question_type = QuestionType(question.get("question_type", ""))
        except ValueError:
            errors.append(f"質問{index}の形式が不正です。")
            continue
        if question_type in {QuestionType.single_choice, QuestionType.multiple_choice}:
            options = [item.strip() for item in question.get("options", []) if item.strip()]
            if len(options) < 2:
                errors.append(f"質問{index}は選択肢を2件以上入力してください。")
    return errors


def normalize_question_specs(question_specs: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for spec in question_specs:
        label = str(spec.get("label", "")).strip()
        if not label:
            continue
        try:
            question_type = QuestionType(spec.get("question_type", QuestionType.text_short.value))
        except ValueError:
            question_type = QuestionType.text_short
        options = [item.strip() for item in spec.get("options", []) if item.strip()]
        normalized.append(
            {
                "label": label,
                "description": str(spec.get("description", "")).strip() or None,
                "question_type": question_type,
                "is_required": bool(spec.get("is_required")),
                "options": options,
            }
        )
    return normalized


def replace_survey_questions(session: Session, survey: Survey, question_specs: list[dict]) -> None:
    for question in list(survey.questions):
        for option in list(question.options):
            session.delete(option)
        session.delete(question)
    session.flush()

    for order, spec in enumerate(normalize_question_specs(question_specs), start=1):
        question = SurveyQuestion(
            survey_id=survey.id,
            order=order,
            question_type=spec["question_type"],
            label=spec["label"],
            description=spec["description"],
            is_required=spec["is_required"],
        )
        session.add(question)
        session.flush()
        for option_order, label in enumerate(spec["options"], start=1):
            session.add(
                SurveyQuestionOption(
                    question_id=question.id,
                    order=option_order,
                    option_key=generate_option_key(),
                    label=label,
                )
            )


def replace_survey_target(
    session: Session,
    survey: Survey,
    target_type: SurveyTargetType,
    target_value: str | None,
) -> None:
    from models import SurveyTarget

    for target in list(survey.targets):
        session.delete(target)
    session.flush()
    value = (target_value or "").strip() or None
    if target_type in {SurveyTargetType.all, SurveyTargetType.all_staff}:
        value = None
    session.add(SurveyTarget(survey_id=survey.id, target_type=target_type, target_value=value))


def _option_ids_for_question(question: SurveyQuestion) -> set[int]:
    return {option.id for option in question.options if option.id is not None}


def _values_for_question(form_data, question: SurveyQuestion) -> list[str]:
    field_name = f"q{question.id}"
    if hasattr(form_data, "getlist"):
        values = form_data.getlist(field_name)
        if not values:
            values = form_data.getlist(f"q_{question.id}")
    else:
        raw = form_data.get(field_name, [])
        if not raw:
            raw = form_data.get(f"q_{question.id}", [])
        values = raw if isinstance(raw, list) else [raw]
    return [str(value).strip() for value in values if str(value).strip()]


def save_survey_answer(
    session: Session,
    *,
    survey: Survey,
    scope: SurveyAnswerScope,
    form_data,
    parent_account: ParentAccount | None = None,
    staff_user: User | None = None,
) -> SurveyFormResult:
    if not scope.is_valid:
        return SurveyFormResult(answer=None, errors=["回答スコープを特定できません。"])

    if survey.audience_type == SurveyAudienceType.parent and parent_account is None:
        return SurveyFormResult(answer=None, errors=["保護者アカウントを特定できません。"])
    if survey.audience_type == SurveyAudienceType.staff and staff_user is None:
        return SurveyFormResult(answer=None, errors=["職員アカウントを特定できません。"])

    responses: list[SurveyResponse] = []
    errors: list[str] = []
    questions = sorted(survey.questions, key=lambda item: (item.order, item.id or 0))
    for question in questions:
        values = _values_for_question(form_data, question)
        if question.is_required and not values:
            errors.append(f"{question.label}を入力してください。")
            continue

        response = SurveyResponse(answer_id=0, question_id=question.id)
        if question.question_type in {QuestionType.text_short, QuestionType.text_long}:
            response.value_text = values[0] if values else None
        elif question.question_type == QuestionType.single_choice:
            if len(values) > 1:
                errors.append(f"{question.label}は1つだけ選択してください。")
                continue
            if values:
                option_id = _parse_optional_int(values[0])
                if option_id is None or option_id not in _option_ids_for_question(question):
                    errors.append(f"{question.label}の選択肢が不正です。")
                    continue
                response.value_option_ids = [option_id]
        elif question.question_type == QuestionType.multiple_choice:
            option_ids: list[int] = []
            for raw_value in values:
                option_id = _parse_optional_int(raw_value)
                if option_id is None or option_id not in _option_ids_for_question(question):
                    errors.append(f"{question.label}の選択肢が不正です。")
                    option_ids = []
                    break
                option_ids.append(option_id)
            if len(option_ids) != len(set(option_ids)):
                errors.append(f"{question.label}に重複した選択肢があります。")
                continue
            response.value_option_ids = option_ids
        elif question.question_type == QuestionType.scale:
            if values:
                value = _parse_optional_int(values[0])
                if value is None or value < 1 or value > 5:
                    errors.append(f"{question.label}は1から5で選択してください。")
                    continue
                response.value_scale = value
        elif question.question_type == QuestionType.yes_no:
            if values:
                if values[0] not in {"true", "false"}:
                    errors.append(f"{question.label}ははい/いいえを選択してください。")
                    continue
                response.value_bool = values[0] == "true"
        elif question.question_type == QuestionType.date:
            if values:
                try:
                    response.value_date = date.fromisoformat(values[0])
                except ValueError:
                    errors.append(f"{question.label}は有効な日付を入力してください。")
                    continue
        responses.append(response)

    if errors:
        return SurveyFormResult(answer=None, errors=errors)

    now = utc_now()
    answer = load_existing_survey_answer(session, survey, scope)
    if answer is None:
        answer = SurveyAnswer(
            survey_id=survey.id,
            family_id=scope.family_id,
            child_id=scope.child_id,
            staff_user_id=scope.staff_user_id,
            created_by_parent_account_id=parent_account.id if parent_account else None,
            created_by_staff_user_id=staff_user.id if staff_user else None,
            submitted_by_parent_account_id=parent_account.id if parent_account else None,
            submitted_by_staff_user_id=staff_user.id if staff_user else None,
            submitted_at=now,
        )
        session.add(answer)
        session.flush()
    else:
        existing_responses = session.exec(
            select(SurveyResponse).where(SurveyResponse.answer_id == answer.id)
        ).all()
        for response in existing_responses:
            session.delete(response)
        answer.submitted_by_parent_account_id = parent_account.id if parent_account else None
        answer.submitted_by_staff_user_id = staff_user.id if staff_user else None
        answer.submitted_at = now
        answer.updated_at = now
        session.add(answer)
        session.flush()

    for response in responses:
        response.answer_id = answer.id
        session.add(response)
    return SurveyFormResult(answer=answer, errors=[])


def answer_value_for_display(question: SurveyQuestion, response: SurveyResponse | None) -> str:
    if response is None:
        return ""
    if question.question_type in {QuestionType.text_short, QuestionType.text_long}:
        return response.value_text or ""
    if question.question_type in {QuestionType.single_choice, QuestionType.multiple_choice}:
        option_by_id = {option.id: option for option in question.options}
        labels = [
            option_by_id[option_id].label
            for option_id in (response.value_option_ids or [])
            if option_id in option_by_id
        ]
        return " / ".join(labels)
    if question.question_type == QuestionType.scale:
        return str(response.value_scale) if response.value_scale is not None else ""
    if question.question_type == QuestionType.yes_no:
        if response.value_bool is None:
            return ""
        return "はい" if response.value_bool else "いいえ"
    if question.question_type == QuestionType.date:
        return response.value_date.isoformat() if response.value_date else ""
    return ""


def target_label(
    survey: Survey,
    *,
    classrooms_by_id: dict[int, Classroom],
    children_by_id: dict[int, Child],
    users_by_id: dict[str, User],
) -> str:
    if not survey.targets:
        return "-"
    labels: list[str] = []
    for target in survey.targets:
        if target.target_type == SurveyTargetType.all:
            labels.append("全職員" if survey.audience_type == SurveyAudienceType.staff else "全保護者")
        elif target.target_type == SurveyTargetType.classroom:
            classroom = classrooms_by_id.get(_parse_optional_int(target.target_value) or -1)
            labels.append(f"クラス: {classroom.name}" if classroom else "クラス指定")
        elif target.target_type == SurveyTargetType.child:
            child = children_by_id.get(_parse_optional_int(target.target_value) or -1)
            labels.append(f"園児: {child.full_name}" if child else "園児指定")
        elif target.target_type == SurveyTargetType.all_staff:
            labels.append("全職員")
        elif target.target_type == SurveyTargetType.staff_role:
            labels.append(f"ロール: {target.target_value}")
        elif target.target_type == SurveyTargetType.staff_user:
            user = users_by_id.get(target.target_value or "")
            labels.append(f"職員: {user.display_name}" if user else "職員指定")
    return " / ".join(labels)


def unanswered_staff_survey_count(session: Session, staff_user: User) -> int:
    surveys = session.exec(
        select(Survey).where(
            Survey.audience_type == SurveyAudienceType.staff,
            Survey.status == SurveyStatus.published,
        )
    ).all()
    count = 0
    for survey in surveys:
        if not survey_is_open(survey) or not survey_matches_staff_targets(
            survey,
            staff_user,
            {str(user_id) for user_id in equivalent_staff_user_ids(session, staff_user.id)},
        ):
            continue
        scope = resolve_staff_answer_scope(survey, staff_user)
        if scope and load_existing_survey_answer(session, survey, scope) is None:
            count += 1
    return count


def sanitize_csv_header_label(label: str) -> str:
    value = unicodedata.normalize("NFKC", label or "")
    value = value.replace("\n", " ").replace("\t", " ").replace(",", " ")
    value = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff ]", "_", value)
    value = re.sub(r"[\s_]+", "_", value).strip("_ ")
    if len(value) > 40:
        value = value[:40]
    return value or "question"
