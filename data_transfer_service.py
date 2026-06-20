from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from io import BytesIO, StringIO
from typing import Iterable, Optional
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from family_support import sync_family_to_children, sync_parent_child_links
from models import (
    Child,
    ChildStatus,
    Classroom,
    DataTransferLog,
    Family,
    ParentAccount,
    ParentAccountStatus,
    ParentChildLink,
)
from time_utils import utc_now


@dataclass(frozen=True)
class DatasetDefinition:
    id: str
    label: str
    sheet_name: str
    headers: tuple[str, ...]


@dataclass
class TransferMessage:
    row_number: int
    column: str
    value: str
    message: str


@dataclass
class ParsedImportRows:
    rows: list[tuple[int, dict[str, str]]]
    skipped_count: int = 0
    errors: list[TransferMessage] = field(default_factory=list)


@dataclass
class ImportPreviewResult:
    dataset: str
    dataset_label: str
    filename: str
    total_rows: int = 0
    create_count: int = 0
    update_count: int = 0
    skipped_count: int = 0
    errors: list[TransferMessage] = field(default_factory=list)
    warnings: list[TransferMessage] = field(default_factory=list)
    preview_token: Optional[str] = None

    @property
    def can_commit(self) -> bool:
        return not self.errors and self.total_rows > 0 and self.preview_token is not None


DATASETS: dict[str, DatasetDefinition] = {
    "classrooms": DatasetDefinition(
        id="classrooms",
        label="クラス",
        sheet_name="classrooms",
        headers=("ID", "クラス名", "表示順"),
    ),
    "families": DatasetDefinition(
        id="families",
        label="家庭",
        sheet_name="families",
        headers=("ID", "家庭名", "住所", "電話番号"),
    ),
    "children": DatasetDefinition(
        id="children",
        label="園児",
        sheet_name="children",
        headers=(
            "ID",
            "姓",
            "名",
            "姓カナ",
            "名カナ",
            "生年月日",
            "入園日",
            "退園日",
            "在園状態",
            "クラス名",
            "家庭ID",
            "家庭名",
            "住所",
            "電話番号",
        ),
    ),
    "parent_accounts": DatasetDefinition(
        id="parent_accounts",
        label="保護者アカウント",
        sheet_name="parent_accounts",
        headers=(
            "ID",
            "表示名",
            "メールアドレス",
            "電話番号",
            "住所",
            "勤務先",
            "勤務先住所",
            "勤務先電話番号",
            "家庭ID",
            "家庭名",
            "状態",
        ),
    ),
    "parent_child_links": DatasetDefinition(
        id="parent_child_links",
        label="保護者・園児紐づけ",
        sheet_name="parent_child_links",
        headers=(
            "ID",
            "保護者ID",
            "保護者メールアドレス",
            "園児ID",
            "園児姓カナ",
            "園児名カナ",
            "園児生年月日",
            "続柄",
            "主連絡先",
        ),
    ),
}


CHILD_STATUS_INPUTS = {
    "enrolled": ChildStatus.enrolled,
    "在園": ChildStatus.enrolled,
    "graduated": ChildStatus.graduated,
    "卒園": ChildStatus.graduated,
    "withdrawn": ChildStatus.withdrawn,
    "退園": ChildStatus.withdrawn,
}

PARENT_STATUS_INPUTS = {
    "active": ParentAccountStatus.active,
    "有効": ParentAccountStatus.active,
    "inactive": ParentAccountStatus.inactive,
    "停止中": ParentAccountStatus.inactive,
    "停止": ParentAccountStatus.inactive,
}

TRUE_INPUTS = {"true", "1", "yes", "y", "はい", "有", "あり"}
FALSE_INPUTS = {"false", "0", "no", "n", "いいえ", "無", "なし"}


def dataset_options() -> list[DatasetDefinition]:
    return list(DATASETS.values())


def get_dataset(dataset: str) -> DatasetDefinition:
    try:
        return DATASETS[dataset]
    except KeyError as exc:
        raise ValueError("未対応のデータ種別です。") from exc


def template_rows(dataset: str) -> list[list[str]]:
    return [list(get_dataset(dataset).headers)]


def build_csv_content(rows: list[list[str]]) -> bytes:
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _xlsx_column_name(index: int) -> str:
    name = ""
    current = index + 1
    while current:
        current, remainder = divmod(current - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _xlsx_column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return max(index - 1, 0)


def build_xlsx_content(rows: list[list[str]], sheet_name: str) -> bytes:
    safe_sheet_name = sheet_name[:31] or "Sheet1"
    row_xml_parts = []

    for row_index, row in enumerate(rows, start=1):
        cell_xml_parts = []
        for column_index, raw_value in enumerate(row):
            value = "" if raw_value is None else str(raw_value)
            cell_ref = f"{_xlsx_column_name(column_index)}{row_index}"
            cell_xml_parts.append(
                '<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{value}</t></is></c>'.format(
                    ref=cell_ref,
                    value=escape(value),
                )
            )
        row_xml_parts.append(f'<row r="{row_index}">{"".join(cell_xml_parts)}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        f'{"".join(row_xml_parts)}'
        "</sheetData>"
        "</worksheet>"
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(safe_sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )

    buffer = BytesIO()
    with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", rels_xml)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)

    return buffer.getvalue()


def export_rows(session: Session, dataset: str, *, classroom_id: str = "", status: str = "") -> list[list[str]]:
    definition = get_dataset(dataset)
    rows = [list(definition.headers)]
    if dataset == "classrooms":
        rows.extend(_export_classrooms(session))
    elif dataset == "families":
        rows.extend(_export_families(session))
    elif dataset == "children":
        rows.extend(_export_children(session, classroom_id=classroom_id, status=status))
    elif dataset == "parent_accounts":
        rows.extend(_export_parent_accounts(session, status=status))
    elif dataset == "parent_child_links":
        rows.extend(_export_parent_child_links(session, classroom_id=classroom_id, status=status))
    return rows


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _export_classrooms(session: Session) -> list[list[str]]:
    classrooms = session.exec(select(Classroom).order_by(Classroom.display_order, Classroom.id)).all()
    return [[_text(item.id), item.name, _text(item.display_order)] for item in classrooms]


def _export_families(session: Session) -> list[list[str]]:
    families = session.exec(select(Family).order_by(Family.family_name, Family.id)).all()
    return [[_text(item.id), item.family_name, _text(item.home_address), _text(item.home_phone)] for item in families]


def _export_children(session: Session, *, classroom_id: str = "", status: str = "") -> list[list[str]]:
    stmt = select(Child).options(selectinload(Child.classroom), selectinload(Child.family))
    if classroom_id and classroom_id.isdigit():
        stmt = stmt.where(Child.classroom_id == int(classroom_id))
    normalized_status = _parse_child_status_value(status)
    if normalized_status is not None:
        stmt = stmt.where(Child.status == normalized_status)
    children = session.exec(stmt.order_by(Child.last_name_kana, Child.first_name_kana, Child.id)).all()
    return [
        [
            _text(child.id),
            child.last_name,
            child.first_name,
            child.last_name_kana,
            child.first_name_kana,
            child.birth_date.isoformat(),
            child.enrollment_date.isoformat(),
            child.withdrawal_date.isoformat() if child.withdrawal_date else "",
            child.status.label,
            child.classroom.name if child.classroom else "",
            _text(child.family_id),
            child.family.family_name if child.family else "",
            _text(child.home_address),
            _text(child.home_phone),
        ]
        for child in children
    ]


def _export_parent_accounts(session: Session, *, status: str = "") -> list[list[str]]:
    stmt = select(ParentAccount).options(selectinload(ParentAccount.family))
    normalized_status = _parse_parent_status_value(status)
    if normalized_status is not None:
        stmt = stmt.where(ParentAccount.status == normalized_status)
    accounts = session.exec(stmt.order_by(ParentAccount.display_name, ParentAccount.id)).all()
    return [
        [
            _text(account.id),
            account.display_name,
            account.email,
            _text(account.phone),
            _text(account.home_address),
            _text(account.workplace),
            _text(account.workplace_address),
            _text(account.workplace_phone),
            _text(account.family_id),
            account.family.family_name if account.family else "",
            account.status.label,
        ]
        for account in accounts
    ]


def _export_parent_child_links(session: Session, *, classroom_id: str = "", status: str = "") -> list[list[str]]:
    links = session.exec(
        select(ParentChildLink)
        .options(selectinload(ParentChildLink.parent_account), selectinload(ParentChildLink.child))
        .order_by(ParentChildLink.parent_account_id, ParentChildLink.child_id)
    ).all()
    normalized_status = _parse_child_status_value(status)
    rows: list[list[str]] = []
    for link in links:
        child = link.child
        parent = link.parent_account
        if not child or not parent:
            continue
        if classroom_id and classroom_id.isdigit() and child.classroom_id != int(classroom_id):
            continue
        if normalized_status is not None and child.status != normalized_status:
            continue
        rows.append(
            [
                _text(link.id),
                _text(parent.id),
                parent.email,
                _text(child.id),
                child.last_name_kana,
                child.first_name_kana,
                child.birth_date.isoformat(),
                link.relationship_label,
                "true" if link.is_primary_contact else "false",
            ]
        )
    return rows


def parse_import_file(dataset: str, filename: str, content: bytes) -> ParsedImportRows:
    definition = get_dataset(dataset)
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        if extension == "csv":
            matrix = _read_csv_matrix(content)
        elif extension == "xlsx":
            matrix = _read_xlsx_matrix(content)
        else:
            return ParsedImportRows(
                rows=[],
                errors=[TransferMessage(1, "ファイル", filename, "CSV または Excel ファイルを選択してください。")],
            )
    except ValueError as exc:
        return ParsedImportRows(rows=[], errors=[TransferMessage(1, "ファイル", filename, str(exc))])

    if not matrix:
        return ParsedImportRows(rows=[], errors=[TransferMessage(1, "ファイル", filename, "ファイルにデータがありません。")])

    headers = [_normalize_header(item) for item in matrix[0]]
    errors: list[TransferMessage] = []
    duplicate_headers = sorted({header for header in headers if header and headers.count(header) > 1})
    for header in duplicate_headers:
        errors.append(TransferMessage(1, header, "", "列名が重複しています。"))
    for expected_header in definition.headers:
        if expected_header not in headers:
            errors.append(TransferMessage(1, expected_header, "", "必須列がありません。"))
    if errors:
        return ParsedImportRows(rows=[], errors=errors)

    rows: list[tuple[int, dict[str, str]]] = []
    skipped_count = 0
    for row_number, raw_row in enumerate(matrix[1:], start=2):
        values = {header: _normalize_cell(raw_row[index] if index < len(raw_row) else "") for index, header in enumerate(headers)}
        expected_values = {header: values.get(header, "") for header in definition.headers}
        if not any(expected_values.values()):
            skipped_count += 1
            continue
        rows.append((row_number, expected_values))

    return ParsedImportRows(rows=rows, skipped_count=skipped_count, errors=[])


def preview_import(session: Session, dataset: str, filename: str, content: bytes) -> ImportPreviewResult:
    definition = get_dataset(dataset)
    parsed = parse_import_file(dataset, filename, content)
    result = ImportPreviewResult(
        dataset=dataset,
        dataset_label=definition.label,
        filename=filename,
        total_rows=len(parsed.rows),
        skipped_count=parsed.skipped_count,
        errors=list(parsed.errors),
    )
    if result.errors:
        return result

    _plan_import(session, dataset, parsed.rows, result, commit=False)
    return result


def commit_import(session: Session, dataset: str, filename: str, content: bytes, *, actor_name: str) -> ImportPreviewResult:
    result = preview_import(session, dataset, filename, content)
    if result.errors:
        _record_import_log(session, dataset, filename, actor_name, result, "failed")
        session.commit()
        return result

    parsed = parse_import_file(dataset, filename, content)
    applied = ImportPreviewResult(
        dataset=result.dataset,
        dataset_label=result.dataset_label,
        filename=result.filename,
        total_rows=result.total_rows,
        skipped_count=result.skipped_count,
    )
    _plan_import(session, dataset, parsed.rows, applied, commit=True)
    if applied.errors:
        session.rollback()
        _record_import_log(session, dataset, filename, actor_name, applied, "failed")
        session.commit()
        return applied

    _record_import_log(session, dataset, filename, actor_name, applied, "success")
    session.commit()
    return applied


def _read_csv_matrix(content: bytes) -> list[list[str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp932")
    reader = csv.reader(StringIO(text, newline=""))
    return [list(row) for row in reader]


def _read_xlsx_matrix(content: bytes) -> list[list[str]]:
    try:
        archive = ZipFile(BytesIO(content))
    except Exception as exc:
        raise ValueError("Excel ファイルを読み込めません。") from exc

    with archive:
        sheet_paths = sorted(
            path
            for path in archive.namelist()
            if path.startswith("xl/worksheets/") and path.endswith(".xml")
        )
        if len(sheet_paths) != 1:
            raise ValueError("Excel ファイルは 1 シートのみ対応しています。")

        shared_strings = _read_shared_strings(archive)
        sheet_xml = archive.read(sheet_paths[0])

    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(sheet_xml)
    rows: list[list[str]] = []
    for row in root.findall(".//main:sheetData/main:row", namespace):
        values: list[str] = []
        for cell in row.findall("main:c", namespace):
            cell_ref = cell.attrib.get("r", "")
            column_index = _xlsx_column_index(cell_ref)
            while len(values) <= column_index:
                values.append("")
            values[column_index] = _xlsx_cell_value(cell, shared_strings, namespace)
        rows.append(values)
    return rows


def _read_shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    strings = []
    for item in root.findall("main:si", namespace):
        strings.append("".join(text_node.text or "" for text_node in item.findall(".//main:t", namespace)))
    return strings


def _xlsx_cell_value(cell: ET.Element, shared_strings: list[str], namespace: dict[str, str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(text_node.text or "" for text_node in cell.findall(".//main:t", namespace))
    value_node = cell.find("main:v", namespace)
    if value_node is None or value_node.text is None:
        return ""
    raw_value = value_node.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw_value)]
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return "true" if raw_value == "1" else "false"
    return raw_value


def _plan_import(
    session: Session,
    dataset: str,
    rows: list[tuple[int, dict[str, str]]],
    result: ImportPreviewResult,
    *,
    commit: bool,
) -> None:
    if dataset == "classrooms":
        _plan_classrooms(session, rows, result, commit=commit)
    elif dataset == "families":
        _plan_families(session, rows, result, commit=commit)
    elif dataset == "children":
        _plan_children(session, rows, result, commit=commit)
    elif dataset == "parent_accounts":
        _plan_parent_accounts(session, rows, result, commit=commit)
    elif dataset == "parent_child_links":
        _plan_parent_child_links(session, rows, result, commit=commit)
    else:
        result.errors.append(TransferMessage(1, "データ種別", dataset, "未対応のデータ種別です。"))


def _plan_classrooms(
    session: Session,
    rows: list[tuple[int, dict[str, str]]],
    result: ImportPreviewResult,
    *,
    commit: bool,
) -> None:
    seen: set[str] = set()
    for row_number, row in rows:
        start_errors = len(result.errors)
        classroom = _resolve_classroom_for_import(session, row, row_number, result)
        name = row["クラス名"]
        display_order = _parse_int(row["表示順"], row_number, "表示順", result, required=False, default=None)
        if display_order is not None and display_order < 1:
            result.errors.append(TransferMessage(row_number, "表示順", row["表示順"], "表示順は 1 以上で入力してください。"))

        if classroom is None and not name:
            result.errors.append(TransferMessage(row_number, "クラス名", name, "新規登録時はクラス名が必須です。"))

        target_name = name or (classroom.name if classroom else "")
        if target_name:
            existing_same_name = session.exec(select(Classroom).where(Classroom.name == target_name)).first()
            if existing_same_name and (classroom is None or existing_same_name.id != classroom.id):
                result.errors.append(TransferMessage(row_number, "クラス名", target_name, "同じクラス名がすでに登録されています。"))

        key = _row_key("classrooms", row, fallback=target_name)
        _check_duplicate_key(seen, key, row_number, "クラス名", target_name, result)

        if len(result.errors) != start_errors:
            continue

        if classroom is None:
            result.create_count += 1
            if commit:
                session.add(Classroom(name=name, display_order=display_order or 1, updated_at=utc_now()))
        else:
            result.update_count += 1
            if commit:
                if name:
                    classroom.name = name
                if display_order is not None:
                    classroom.display_order = display_order
                classroom.updated_at = utc_now()
                session.add(classroom)


def _plan_families(
    session: Session,
    rows: list[tuple[int, dict[str, str]]],
    result: ImportPreviewResult,
    *,
    commit: bool,
) -> None:
    seen: set[str] = set()
    touched_family_ids: set[int] = set()
    for row_number, row in rows:
        start_errors = len(result.errors)
        family = _resolve_family_for_import(session, row, row_number, result)
        family_name = row["家庭名"]
        home_phone = row["電話番号"]

        if family is None and not family_name:
            result.errors.append(TransferMessage(row_number, "家庭名", family_name, "新規登録時は家庭名が必須です。"))

        key = _row_key("families", row, fallback=f"{family_name}|{home_phone}")
        _check_duplicate_key(seen, key, row_number, "家庭名", family_name, result)

        if len(result.errors) != start_errors:
            continue

        if family is None:
            result.create_count += 1
            if commit:
                family = Family(
                    family_name=family_name,
                    home_address=row["住所"] or None,
                    home_phone=home_phone or None,
                    updated_at=utc_now(),
                )
                session.add(family)
                session.flush()
                if family.id is not None:
                    touched_family_ids.add(family.id)
        else:
            result.update_count += 1
            if commit:
                if family_name:
                    family.family_name = family_name
                if row["住所"]:
                    family.home_address = row["住所"]
                if home_phone:
                    family.home_phone = home_phone
                family.updated_at = utc_now()
                session.add(family)
                session.flush()
                if family.id is not None:
                    touched_family_ids.add(family.id)

    if commit:
        _sync_family_ids(session, touched_family_ids, sync_children=True)


def _plan_children(
    session: Session,
    rows: list[tuple[int, dict[str, str]]],
    result: ImportPreviewResult,
    *,
    commit: bool,
) -> None:
    seen: set[str] = set()
    touched_family_ids: set[int] = set()
    for row_number, row in rows:
        start_errors = len(result.errors)
        child = _resolve_child_for_import(session, row, row_number, result)
        family = _resolve_family_reference(session, row, row_number, result)
        classroom = _resolve_classroom_reference(session, row, row_number, result)
        birth_date = _parse_date(row["生年月日"], row_number, "生年月日", result, required=child is None)
        enrollment_date = _parse_date(row["入園日"], row_number, "入園日", result, required=child is None)
        withdrawal_date = _parse_date(row["退園日"], row_number, "退園日", result, required=False)
        status = _parse_child_status(row["在園状態"], row_number, result, required=False)

        for header in ("姓", "名", "姓カナ", "名カナ"):
            if child is None and not row[header]:
                result.errors.append(TransferMessage(row_number, header, row[header], "新規登録時は必須です。"))

        target_last_kana = row["姓カナ"] or (child.last_name_kana if child else "")
        target_first_kana = row["名カナ"] or (child.first_name_kana if child else "")
        target_birth_date = birth_date or (child.birth_date if child else None)
        if target_last_kana and target_first_kana and target_birth_date:
            same_child = _find_child_by_natural(session, target_last_kana, target_first_kana, target_birth_date)
            if same_child and (child is None or same_child.id != child.id):
                result.errors.append(
                    TransferMessage(row_number, "園児", f"{target_last_kana} {target_first_kana}", "同じ園児がすでに登録されています。")
                )

        key = _row_key("children", row, fallback=f"{target_last_kana}|{target_first_kana}|{target_birth_date}")
        _check_duplicate_key(seen, key, row_number, "園児", key, result)

        if len(result.errors) != start_errors:
            continue

        if child is None:
            result.create_count += 1
            if commit:
                child = Child(
                    last_name=row["姓"],
                    first_name=row["名"],
                    last_name_kana=row["姓カナ"],
                    first_name_kana=row["名カナ"],
                    birth_date=birth_date,
                    enrollment_date=enrollment_date,
                    withdrawal_date=withdrawal_date,
                    status=status or ChildStatus.enrolled,
                    classroom_id=classroom.id if classroom else None,
                    family_id=family.id if family else None,
                    home_address=row["住所"] or None,
                    home_phone=row["電話番号"] or None,
                    extra_data={"allergy": [], "medical_notes": ""},
                    updated_at=utc_now(),
                )
                session.add(child)
                session.flush()
                if child.family_id:
                    touched_family_ids.add(child.family_id)
        else:
            result.update_count += 1
            if commit:
                old_family_id = child.family_id
                _set_if_present(child, "last_name", row["姓"])
                _set_if_present(child, "first_name", row["名"])
                _set_if_present(child, "last_name_kana", row["姓カナ"])
                _set_if_present(child, "first_name_kana", row["名カナ"])
                if birth_date is not None:
                    child.birth_date = birth_date
                if enrollment_date is not None:
                    child.enrollment_date = enrollment_date
                if row["退園日"]:
                    child.withdrawal_date = withdrawal_date
                if status is not None:
                    child.status = status
                if row["クラス名"]:
                    child.classroom_id = classroom.id if classroom else None
                if row["家庭ID"] or row["家庭名"]:
                    child.family_id = family.id if family else None
                _set_if_present(child, "home_address", row["住所"])
                _set_if_present(child, "home_phone", row["電話番号"])
                child.updated_at = utc_now()
                session.add(child)
                session.flush()
                for family_id in (old_family_id, child.family_id):
                    if family_id:
                        touched_family_ids.add(family_id)

    if commit:
        _sync_family_ids(session, touched_family_ids, sync_children=False)


def _plan_parent_accounts(
    session: Session,
    rows: list[tuple[int, dict[str, str]]],
    result: ImportPreviewResult,
    *,
    commit: bool,
) -> None:
    seen: set[str] = set()
    touched_family_ids: set[int] = set()
    for row_number, row in rows:
        start_errors = len(result.errors)
        account = _resolve_parent_account_for_import(session, row, row_number, result)
        family = _resolve_family_reference(session, row, row_number, result)
        status = _parse_parent_status(row["状態"], row_number, result, required=False)

        if account is None:
            for header in ("表示名", "メールアドレス"):
                if not row[header]:
                    result.errors.append(TransferMessage(row_number, header, row[header], "新規登録時は必須です。"))

        target_email = row["メールアドレス"] or (account.email if account else "")
        if target_email:
            existing_same_email = session.exec(select(ParentAccount).where(ParentAccount.email == target_email)).first()
            if existing_same_email and (account is None or existing_same_email.id != account.id):
                result.errors.append(TransferMessage(row_number, "メールアドレス", target_email, "同じメールアドレスがすでに登録されています。"))

        key = _row_key("parent_accounts", row, fallback=target_email)
        _check_duplicate_key(seen, key, row_number, "メールアドレス", target_email, result)

        if len(result.errors) != start_errors:
            continue

        if account is None:
            result.create_count += 1
            if commit:
                account = ParentAccount(
                    display_name=row["表示名"],
                    email=row["メールアドレス"],
                    phone=row["電話番号"] or None,
                    home_address=row["住所"] or None,
                    workplace=row["勤務先"] or None,
                    workplace_address=row["勤務先住所"] or None,
                    workplace_phone=row["勤務先電話番号"] or None,
                    family_id=family.id if family else None,
                    status=status or ParentAccountStatus.active,
                    invited_at=utc_now(),
                    updated_at=utc_now(),
                )
                session.add(account)
                session.flush()
                if account.family_id:
                    touched_family_ids.add(account.family_id)
        else:
            result.update_count += 1
            if commit:
                old_family_id = account.family_id
                _set_if_present(account, "display_name", row["表示名"])
                _set_if_present(account, "email", row["メールアドレス"])
                _set_if_present(account, "phone", row["電話番号"])
                _set_if_present(account, "home_address", row["住所"])
                _set_if_present(account, "workplace", row["勤務先"])
                _set_if_present(account, "workplace_address", row["勤務先住所"])
                _set_if_present(account, "workplace_phone", row["勤務先電話番号"])
                if row["家庭ID"] or row["家庭名"]:
                    account.family_id = family.id if family else None
                if status is not None:
                    account.status = status
                account.updated_at = utc_now()
                session.add(account)
                session.flush()
                for family_id in (old_family_id, account.family_id):
                    if family_id:
                        touched_family_ids.add(family_id)

    if commit:
        _sync_family_ids(session, touched_family_ids, sync_children=False)


def _plan_parent_child_links(
    session: Session,
    rows: list[tuple[int, dict[str, str]]],
    result: ImportPreviewResult,
    *,
    commit: bool,
) -> None:
    seen: set[str] = set()
    for row_number, row in rows:
        start_errors = len(result.errors)
        link = _resolve_parent_child_link_for_import(session, row, row_number, result)
        parent = _resolve_parent_reference(session, row, row_number, result, required=link is None)
        child = _resolve_child_reference(session, row, row_number, result, required=link is None)
        is_primary = _parse_bool(row["主連絡先"], row_number, "主連絡先", result, required=False)

        if link is not None and parent is not None and link.parent_account_id != parent.id:
            result.errors.append(TransferMessage(row_number, "保護者ID", row["保護者ID"], "紐づけIDの保護者と一致しません。"))
        if link is not None and child is not None and link.child_id != child.id:
            result.errors.append(TransferMessage(row_number, "園児ID", row["園児ID"], "紐づけIDの園児と一致しません。"))

        target_parent_id = parent.id if parent else (link.parent_account_id if link else None)
        target_child_id = child.id if child else (link.child_id if link else None)
        if link is None and target_parent_id and target_child_id:
            link = session.exec(
                select(ParentChildLink).where(
                    ParentChildLink.parent_account_id == target_parent_id,
                    ParentChildLink.child_id == target_child_id,
                )
            ).first()

        if target_parent_id and target_child_id:
            key = _row_key("parent_child_links", row, fallback=f"{target_parent_id}|{target_child_id}")
            _check_duplicate_key(seen, key, row_number, "保護者・園児", key, result)

        if is_primary is True and target_child_id:
            existing_primary = session.exec(
                select(ParentChildLink).where(
                    ParentChildLink.child_id == target_child_id,
                    ParentChildLink.is_primary_contact.is_(True),
                )
            ).first()
            if existing_primary and (link is None or existing_primary.id != link.id):
                result.warnings.append(
                    TransferMessage(row_number, "主連絡先", row["主連絡先"], "同一園児に別の主連絡先が登録されています。")
                )

        if len(result.errors) != start_errors:
            continue

        if link is None:
            result.create_count += 1
            if commit:
                session.add(
                    ParentChildLink(
                        parent_account_id=target_parent_id,
                        child_id=target_child_id,
                        relationship_label=row["続柄"] or "保護者",
                        is_primary_contact=is_primary if is_primary is not None else False,
                    )
                )
        else:
            result.update_count += 1
            if commit:
                if row["続柄"]:
                    link.relationship_label = row["続柄"]
                if is_primary is not None:
                    link.is_primary_contact = is_primary
                session.add(link)


def _resolve_classroom_for_import(
    session: Session,
    row: dict[str, str],
    row_number: int,
    result: ImportPreviewResult,
) -> Optional[Classroom]:
    item_id = _parse_int(row["ID"], row_number, "ID", result, required=False, default=None)
    if item_id is not None:
        classroom = session.get(Classroom, item_id)
        if classroom is None:
            result.errors.append(TransferMessage(row_number, "ID", row["ID"], "指定されたクラスが見つかりません。"))
        return classroom
    if row["クラス名"]:
        return session.exec(select(Classroom).where(Classroom.name == row["クラス名"])).first()
    return None


def _resolve_family_for_import(
    session: Session,
    row: dict[str, str],
    row_number: int,
    result: ImportPreviewResult,
) -> Optional[Family]:
    item_id = _parse_int(row["ID"], row_number, "ID", result, required=False, default=None)
    if item_id is not None:
        family = session.get(Family, item_id)
        if family is None:
            result.errors.append(TransferMessage(row_number, "ID", row["ID"], "指定された家庭が見つかりません。"))
        return family
    if not row["家庭名"]:
        return None
    matches = [
        family
        for family in session.exec(select(Family).where(Family.family_name == row["家庭名"])).all()
        if (family.home_phone or "") == row["電話番号"]
    ]
    if len(matches) > 1:
        result.errors.append(TransferMessage(row_number, "家庭名", row["家庭名"], "同じ家庭名と電話番号の家庭が複数あります。"))
        return None
    return matches[0] if matches else None


def _resolve_child_for_import(
    session: Session,
    row: dict[str, str],
    row_number: int,
    result: ImportPreviewResult,
) -> Optional[Child]:
    item_id = _parse_int(row["ID"], row_number, "ID", result, required=False, default=None)
    if item_id is not None:
        child = session.get(Child, item_id)
        if child is None:
            result.errors.append(TransferMessage(row_number, "ID", row["ID"], "指定された園児が見つかりません。"))
        return child
    parsed_birth_date = _parse_date(row["生年月日"], row_number, "生年月日", result, required=False)
    if row["姓カナ"] and row["名カナ"] and parsed_birth_date:
        return _find_child_by_natural(session, row["姓カナ"], row["名カナ"], parsed_birth_date)
    return None


def _resolve_parent_account_for_import(
    session: Session,
    row: dict[str, str],
    row_number: int,
    result: ImportPreviewResult,
) -> Optional[ParentAccount]:
    item_id = _parse_int(row["ID"], row_number, "ID", result, required=False, default=None)
    if item_id is not None:
        account = session.get(ParentAccount, item_id)
        if account is None:
            result.errors.append(TransferMessage(row_number, "ID", row["ID"], "指定された保護者アカウントが見つかりません。"))
        return account
    if row["メールアドレス"]:
        return session.exec(select(ParentAccount).where(ParentAccount.email == row["メールアドレス"])).first()
    return None


def _resolve_parent_child_link_for_import(
    session: Session,
    row: dict[str, str],
    row_number: int,
    result: ImportPreviewResult,
) -> Optional[ParentChildLink]:
    item_id = _parse_int(row["ID"], row_number, "ID", result, required=False, default=None)
    if item_id is None:
        return None
    link = session.get(ParentChildLink, item_id)
    if link is None:
        result.errors.append(TransferMessage(row_number, "ID", row["ID"], "指定された紐づけが見つかりません。"))
    return link


def _resolve_family_reference(
    session: Session,
    row: dict[str, str],
    row_number: int,
    result: ImportPreviewResult,
) -> Optional[Family]:
    family_id = _parse_int(row.get("家庭ID", ""), row_number, "家庭ID", result, required=False, default=None)
    family_name = row.get("家庭名", "")
    if family_id is None and not family_name:
        return None
    if family_id is not None:
        family = session.get(Family, family_id)
        if family is None:
            result.errors.append(TransferMessage(row_number, "家庭ID", row.get("家庭ID", ""), "指定された家庭が見つかりません。"))
            return None
        if family_name and family.family_name != family_name:
            result.errors.append(TransferMessage(row_number, "家庭名", family_name, "家庭IDの家庭名と一致しません。"))
        return family

    matches = session.exec(select(Family).where(Family.family_name == family_name)).all()
    if not matches:
        result.errors.append(TransferMessage(row_number, "家庭名", family_name, "指定された家庭が見つかりません。"))
        return None
    if len(matches) > 1:
        result.errors.append(TransferMessage(row_number, "家庭名", family_name, "同じ家庭名の家庭が複数あります。家庭IDを指定してください。"))
        return None
    return matches[0]


def _resolve_classroom_reference(
    session: Session,
    row: dict[str, str],
    row_number: int,
    result: ImportPreviewResult,
) -> Optional[Classroom]:
    classroom_name = row.get("クラス名", "")
    if not classroom_name:
        return None
    classroom = session.exec(select(Classroom).where(Classroom.name == classroom_name)).first()
    if classroom is None:
        result.errors.append(TransferMessage(row_number, "クラス名", classroom_name, "指定されたクラスが見つかりません。"))
    return classroom


def _resolve_parent_reference(
    session: Session,
    row: dict[str, str],
    row_number: int,
    result: ImportPreviewResult,
    *,
    required: bool,
) -> Optional[ParentAccount]:
    parent_id = _parse_int(row["保護者ID"], row_number, "保護者ID", result, required=False, default=None)
    email = row["保護者メールアドレス"]
    if parent_id is None and not email:
        if required:
            result.errors.append(TransferMessage(row_number, "保護者", "", "保護者IDまたは保護者メールアドレスが必須です。"))
        return None
    if parent_id is not None:
        account = session.get(ParentAccount, parent_id)
        if account is None:
            result.errors.append(TransferMessage(row_number, "保護者ID", row["保護者ID"], "指定された保護者が見つかりません。"))
            return None
        if email and account.email != email:
            result.errors.append(TransferMessage(row_number, "保護者メールアドレス", email, "保護者IDのメールアドレスと一致しません。"))
        return account
    account = session.exec(select(ParentAccount).where(ParentAccount.email == email)).first()
    if account is None:
        result.errors.append(TransferMessage(row_number, "保護者メールアドレス", email, "指定された保護者が見つかりません。"))
    return account


def _resolve_child_reference(
    session: Session,
    row: dict[str, str],
    row_number: int,
    result: ImportPreviewResult,
    *,
    required: bool,
) -> Optional[Child]:
    child_id = _parse_int(row["園児ID"], row_number, "園児ID", result, required=False, default=None)
    birth_date = _parse_date(row["園児生年月日"], row_number, "園児生年月日", result, required=False)
    if child_id is None:
        if not row["園児姓カナ"] or not row["園児名カナ"] or birth_date is None:
            if required:
                result.errors.append(TransferMessage(row_number, "園児", "", "園児IDまたは園児姓カナ・園児名カナ・園児生年月日が必須です。"))
            return None
        child = _find_child_by_natural(session, row["園児姓カナ"], row["園児名カナ"], birth_date)
        if child is None:
            result.errors.append(TransferMessage(row_number, "園児", f"{row['園児姓カナ']} {row['園児名カナ']}", "指定された園児が見つかりません。"))
        return child

    child = session.get(Child, child_id)
    if child is None:
        result.errors.append(TransferMessage(row_number, "園児ID", row["園児ID"], "指定された園児が見つかりません。"))
        return None
    if row["園児姓カナ"] and child.last_name_kana != row["園児姓カナ"]:
        result.errors.append(TransferMessage(row_number, "園児姓カナ", row["園児姓カナ"], "園児IDの姓カナと一致しません。"))
    if row["園児名カナ"] and child.first_name_kana != row["園児名カナ"]:
        result.errors.append(TransferMessage(row_number, "園児名カナ", row["園児名カナ"], "園児IDの名カナと一致しません。"))
    if birth_date and child.birth_date != birth_date:
        result.errors.append(TransferMessage(row_number, "園児生年月日", row["園児生年月日"], "園児IDの生年月日と一致しません。"))
    return child


def _find_child_by_natural(session: Session, last_name_kana: str, first_name_kana: str, birth_date: date) -> Optional[Child]:
    return session.exec(
        select(Child).where(
            Child.last_name_kana == last_name_kana,
            Child.first_name_kana == first_name_kana,
            Child.birth_date == birth_date,
        )
    ).first()


def _parse_int(
    value: str,
    row_number: int,
    column: str,
    result: ImportPreviewResult,
    *,
    required: bool,
    default: Optional[int],
) -> Optional[int]:
    if not value:
        if required:
            result.errors.append(TransferMessage(row_number, column, value, "数値を入力してください。"))
        return default
    try:
        return int(value)
    except ValueError:
        result.errors.append(TransferMessage(row_number, column, value, "数値で入力してください。"))
        return default


def _parse_date(
    value: str,
    row_number: int,
    column: str,
    result: ImportPreviewResult,
    *,
    required: bool,
) -> Optional[date]:
    if not value:
        if required:
            result.errors.append(TransferMessage(row_number, column, value, "日付を入力してください。"))
        return None
    normalized = value.strip()
    for date_format in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(normalized, date_format).date()
        except ValueError:
            pass
    try:
        numeric_value = float(normalized)
    except ValueError:
        result.errors.append(TransferMessage(row_number, column, value, "日付は YYYY-MM-DD 形式で入力してください。"))
        return None
    if 1 <= numeric_value <= 100000:
        return (datetime(1899, 12, 30) + timedelta(days=numeric_value)).date()
    result.errors.append(TransferMessage(row_number, column, value, "日付は YYYY-MM-DD 形式で入力してください。"))
    return None


def _parse_child_status(value: str, row_number: int, result: ImportPreviewResult, *, required: bool) -> Optional[ChildStatus]:
    if not value:
        if required:
            result.errors.append(TransferMessage(row_number, "在園状態", value, "在園状態を入力してください。"))
        return None
    status = _parse_child_status_value(value)
    if status is None:
        result.errors.append(TransferMessage(row_number, "在園状態", value, "在園、卒園、退園のいずれかで入力してください。"))
    return status


def _parse_child_status_value(value: str) -> Optional[ChildStatus]:
    return CHILD_STATUS_INPUTS.get((value or "").strip())


def _parse_parent_status(
    value: str,
    row_number: int,
    result: ImportPreviewResult,
    *,
    required: bool,
) -> Optional[ParentAccountStatus]:
    if not value:
        if required:
            result.errors.append(TransferMessage(row_number, "状態", value, "状態を入力してください。"))
        return None
    status = _parse_parent_status_value(value)
    if status is None:
        result.errors.append(TransferMessage(row_number, "状態", value, "有効または停止中で入力してください。"))
    return status


def _parse_parent_status_value(value: str) -> Optional[ParentAccountStatus]:
    return PARENT_STATUS_INPUTS.get((value or "").strip())


def _parse_bool(
    value: str,
    row_number: int,
    column: str,
    result: ImportPreviewResult,
    *,
    required: bool,
) -> Optional[bool]:
    if not value:
        if required:
            result.errors.append(TransferMessage(row_number, column, value, "true または false で入力してください。"))
        return None
    normalized = value.strip().lower()
    if normalized in TRUE_INPUTS:
        return True
    if normalized in FALSE_INPUTS:
        return False
    result.errors.append(TransferMessage(row_number, column, value, "true または false で入力してください。"))
    return None


def _normalize_header(value: object) -> str:
    return str(value or "").replace("\ufeff", "").strip()


def _normalize_cell(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0", text):
        return text[:-2]
    return text


def _set_if_present(item: object, attribute: str, value: str) -> None:
    if value:
        setattr(item, attribute, value)


def _row_key(dataset: str, row: dict[str, str], *, fallback: str) -> str:
    item_id = row.get("ID", "")
    if item_id:
        return f"{dataset}:id:{item_id}"
    return f"{dataset}:key:{fallback}"


def _check_duplicate_key(
    seen: set[str],
    key: str,
    row_number: int,
    column: str,
    value: str,
    result: ImportPreviewResult,
) -> None:
    if not key.endswith(":key:"):
        if key in seen:
            result.errors.append(TransferMessage(row_number, column, value, "ファイル内で同じデータが重複しています。"))
        seen.add(key)


def _sync_family_ids(session: Session, family_ids: Iterable[int], *, sync_children: bool) -> None:
    for family_id in sorted(set(family_ids)):
        family = session.exec(
            select(Family)
            .options(selectinload(Family.children), selectinload(Family.parent_accounts))
            .where(Family.id == family_id)
        ).first()
        if not family:
            continue
        if sync_children:
            sync_family_to_children(session, family, updated_at=utc_now())
        sync_parent_child_links(session, family)


def _record_import_log(
    session: Session,
    dataset: str,
    filename: str,
    actor_name: str,
    result: ImportPreviewResult,
    status: str,
) -> None:
    session.add(
        DataTransferLog(
            transfer_type="import",
            dataset=dataset,
            filename=filename,
            actor_name=actor_name,
            result=status,
            created_count=result.create_count,
            updated_count=result.update_count,
            skipped_count=result.skipped_count,
            error_count=len(result.errors),
        )
    )
