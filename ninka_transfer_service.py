from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from io import BytesIO
from posixpath import dirname, join, normpath
import re
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from sqlmodel import Session, select

from time_utils import local_now, local_today

from models import Child, ChildStatus


SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

ET.register_namespace("", SPREADSHEET_NS)
ET.register_namespace("r", OFFICE_REL_NS)

AGE_ROWS = {
    0: 25,
    1: 27,
    2: 29,
    3: 31,
    4: 33,
    5: 35,
}


@dataclass(frozen=True)
class NinkaAgeBucket:
    age: int
    child_count: int
    classroom_count: int


@dataclass(frozen=True)
class NinkaExportSummary:
    fiscal_year: int
    as_of: date
    total_children: int
    total_classrooms: int
    buckets: tuple[NinkaAgeBucket, ...]


def default_fiscal_year(today: date | None = None) -> int:
    current = today or local_today()
    return current.year if current.month >= 4 else current.year - 1


def build_ninka_xlsx_content(
    session: Session,
    template_content: bytes,
    *,
    fiscal_year: int | None = None,
    as_of: date | None = None,
) -> tuple[bytes, NinkaExportSummary]:
    resolved_as_of = as_of or local_today()
    resolved_fiscal_year = fiscal_year or default_fiscal_year(resolved_as_of)
    summary = _summarize_children(session, fiscal_year=resolved_fiscal_year, as_of=resolved_as_of)

    sheet4_updates: dict[str, int] = {}
    for bucket in summary.buckets:
        row = AGE_ROWS[bucket.age]
        sheet4_updates[f"H{row}"] = bucket.child_count
        sheet4_updates[f"L{row}"] = bucket.child_count
        sheet4_updates[f"P{row}"] = bucket.classroom_count
    sheet4_updates["H37"] = summary.total_children
    sheet4_updates["L37"] = summary.total_children
    sheet4_updates["P37"] = summary.total_classrooms

    updates: dict[str, dict[str, Any]] = {
        "施設情報": {
            "C2": local_now().strftime("%Y-%m-%d %H:%M:%S"),
            "D2": resolved_fiscal_year,
        },
        "シート４": sheet4_updates,
    }

    return _rewrite_xlsx(template_content, updates), summary


def _summarize_children(session: Session, *, fiscal_year: int, as_of: date) -> NinkaExportSummary:
    fiscal_start = date(fiscal_year, 4, 1)
    children = session.exec(
        select(Child)
        .where(Child.status == ChildStatus.enrolled)
        .order_by(Child.classroom_id, Child.birth_date, Child.id)
    ).all()

    child_counts = {age: 0 for age in AGE_ROWS}
    classroom_ids = {age: set() for age in AGE_ROWS}

    for child in children:
        if child.enrollment_date > as_of:
            continue
        if child.withdrawal_date and child.withdrawal_date <= as_of:
            continue
        age = _fiscal_age(child.birth_date, fiscal_start)
        child_counts[age] += 1
        if child.classroom_id is not None:
            classroom_ids[age].add(child.classroom_id)

    buckets = tuple(
        NinkaAgeBucket(age=age, child_count=child_counts[age], classroom_count=len(classroom_ids[age]))
        for age in sorted(AGE_ROWS)
    )
    return NinkaExportSummary(
        fiscal_year=fiscal_year,
        as_of=as_of,
        total_children=sum(bucket.child_count for bucket in buckets),
        total_classrooms=sum(bucket.classroom_count for bucket in buckets),
        buckets=buckets,
    )


def _fiscal_age(birth_date: date, fiscal_start: date) -> int:
    age = fiscal_start.year - birth_date.year - (
        (fiscal_start.month, fiscal_start.day) < (birth_date.month, birth_date.day)
    )
    return min(5, max(0, age))


def _rewrite_xlsx(template_content: bytes, updates: dict[str, dict[str, Any]]) -> bytes:
    try:
        source = ZipFile(BytesIO(template_content))
    except Exception as exc:
        raise ValueError("Excel ファイルを読み込めません。") from exc

    with source:
        sheet_paths = _sheet_paths_by_name(source)
        missing_sheets = [sheet_name for sheet_name in updates if sheet_name not in sheet_paths]
        if missing_sheets:
            raise ValueError(f"認可施設帳票の必須シートが見つかりません: {', '.join(missing_sheets)}")

        updates_by_path = {sheet_paths[sheet_name]: sheet_updates for sheet_name, sheet_updates in updates.items()}
        output = BytesIO()
        with ZipFile(output, mode="w", compression=ZIP_DEFLATED) as destination:
            for item in source.infolist():
                content = source.read(item.filename)
                if item.filename in updates_by_path:
                    content = _update_sheet_xml(content, updates_by_path[item.filename])
                destination.writestr(item, content)
        return output.getvalue()


def _sheet_paths_by_name(archive: ZipFile) -> dict[str, str]:
    try:
        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except KeyError as exc:
        raise ValueError("Excel ブック構造を読み込めません。") from exc

    rel_targets = {
        rel.attrib["Id"]: _resolve_part_path("xl/workbook.xml", rel.attrib["Target"])
        for rel in rels_root.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
        if "Id" in rel.attrib and "Target" in rel.attrib
    }

    sheet_paths: dict[str, str] = {}
    for sheet in workbook_root.findall(f"{{{SPREADSHEET_NS}}}sheets/{{{SPREADSHEET_NS}}}sheet"):
        relationship_id = sheet.attrib.get(f"{{{OFFICE_REL_NS}}}id")
        if relationship_id and relationship_id in rel_targets:
            sheet_paths[sheet.attrib["name"]] = rel_targets[relationship_id]
    return sheet_paths


def _resolve_part_path(base_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return normpath(join(dirname(base_part), target))


def _update_sheet_xml(content: bytes, updates: dict[str, Any]) -> bytes:
    root = ET.fromstring(content)
    sheet_data = root.find(f"{{{SPREADSHEET_NS}}}sheetData")
    if sheet_data is None:
        sheet_data = ET.SubElement(root, f"{{{SPREADSHEET_NS}}}sheetData")

    for cell_ref, value in updates.items():
        cell = _find_or_create_cell(sheet_data, cell_ref)
        _set_cell_value(cell, value)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _find_or_create_cell(sheet_data: ET.Element, cell_ref: str) -> ET.Element:
    column_name, row_number = _split_cell_ref(cell_ref)
    row = _find_or_create_row(sheet_data, row_number)
    for cell in row.findall(f"{{{SPREADSHEET_NS}}}c"):
        if cell.attrib.get("r") == cell_ref:
            return cell

    new_cell = ET.Element(f"{{{SPREADSHEET_NS}}}c", {"r": cell_ref})
    target_column = _column_index(column_name)
    inserted = False
    for index, cell in enumerate(row.findall(f"{{{SPREADSHEET_NS}}}c")):
        existing_ref = cell.attrib.get("r", "")
        existing_column = _split_cell_ref(existing_ref)[0] if existing_ref else ""
        if existing_column and _column_index(existing_column) > target_column:
            row.insert(index, new_cell)
            inserted = True
            break
    if not inserted:
        row.append(new_cell)
    return new_cell


def _find_or_create_row(sheet_data: ET.Element, row_number: int) -> ET.Element:
    for row in sheet_data.findall(f"{{{SPREADSHEET_NS}}}row"):
        if row.attrib.get("r") == str(row_number):
            return row

    new_row = ET.Element(f"{{{SPREADSHEET_NS}}}row", {"r": str(row_number)})
    inserted = False
    for index, row in enumerate(sheet_data.findall(f"{{{SPREADSHEET_NS}}}row")):
        existing = int(row.attrib.get("r", "0") or 0)
        if existing > row_number:
            sheet_data.insert(index, new_row)
            inserted = True
            break
    if not inserted:
        sheet_data.append(new_row)
    return new_row


def _set_cell_value(cell: ET.Element, value: Any) -> None:
    formula = cell.find(f"{{{SPREADSHEET_NS}}}f")
    for child in list(cell):
        if formula is not None and child is formula:
            continue
        if child.tag in {f"{{{SPREADSHEET_NS}}}v", f"{{{SPREADSHEET_NS}}}is"}:
            cell.remove(child)

    if value is None or value == "":
        cell.attrib.pop("t", None)
        return

    if isinstance(value, (int, float)):
        cell.attrib.pop("t", None)
        value_node = ET.SubElement(cell, f"{{{SPREADSHEET_NS}}}v")
        value_node.text = _format_number(value)
        return

    cell.attrib["t"] = "inlineStr"
    inline_string = ET.SubElement(cell, f"{{{SPREADSHEET_NS}}}is")
    text = ET.SubElement(inline_string, f"{{{SPREADSHEET_NS}}}t")
    text.text = str(value)


def _format_number(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _split_cell_ref(cell_ref: str) -> tuple[str, int]:
    match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", cell_ref.upper())
    if not match:
        raise ValueError(f"セル参照が不正です: {cell_ref}")
    return match.group(1), int(match.group(2))


def _column_index(column_name: str) -> int:
    index = 0
    for char in column_name.upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1
