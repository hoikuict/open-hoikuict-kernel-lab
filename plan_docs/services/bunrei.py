from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape
from xml.etree import ElementTree as ET

from ..auth_adapter import StaffUser
from ..contracts import (
    ANNUAL_TERM_ORDER,
    DocumentStatus,
    DocumentType,
    SectionDefinition,
    annual_section_definitions,
    evidence_tags_for,
)
from ..models import PlanDocument, SectionBlock


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATHS = (
    REPO_ROOT / "gen_bunrei" / "bunrei.sqlite",
    REPO_ROOT / "gen_bunnrei" / "bunrei.sqlite",
)
DEFAULT_FACILITY_DB_PATHS = (
    REPO_ROOT / "data" / "facility.sqlite",
    REPO_ROOT / "gen_bunrei" / "facility.sqlite",
    REPO_ROOT / "gen_bunnrei" / "facility.sqlite",
)

MONTHLY_SECTION_ITEMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("monthly_goal", "今月のねらい", ("教育のねらい", "養護のねらい")),
    ("children_snapshot", "子どもの姿の捉え", ("前月末の子どもの姿",)),
    ("monthly_environment", "環境構成", ("環境構成・保育者の援助",)),
    ("monthly_support", "援助", ("活動内容",)),
    ("monthly_health_safety", "健康・安全への配慮", ("健康・安全への配慮",)),
    ("monthly_food_education", "食育", ("食育",)),
    ("monthly_events", "行事", ("行事",)),
    ("monthly_10_perspectives", "10の姿", ("10の姿のねらい",)),
    ("monthly_family_collaboration", "家庭連携", ("家庭との連携",)),
    ("monthly_reflection_viewpoint", "月末の振り返り観点", ("評価・反省",)),
)

ANNUAL_SECTION_ITEMS = {
    "annual_goal": ("年間目標",),
    "outlook": ("予想される子どもの姿",),
    "environment": ("環境構成・保育者の援助",),
    "support": ("環境構成・保育者の援助",),
    "family_collaboration": ("家庭・地域との連携",),
    "reflection_viewpoint": ("期の振り返り観点",),
}

FACILITY_ITEM_OPTIONS: tuple[str, ...] = (
    "年間目標",
    "期の振り返り観点",
    "予想される子どもの姿",
    "教育のねらい",
    "養護のねらい",
    "前月末の子どもの姿",
    "活動内容",
    "環境構成・保育者の援助",
    "健康・安全への配慮",
    "食育",
    "行事",
    "10の姿のねらい",
    "家庭との連携",
    "家庭・地域との連携",
    "評価・反省",
    "生活リズム（食事・睡眠・排泄・遊び）",
)
FACILITY_IMPORT_HEADERS: tuple[str, ...] = (
    "計画種別",
    "年齢",
    "月",
    "項目",
    "領域・観点",
    "出所メモ",
    "本文",
)

_NAME_HONORIFIC = re.compile(
    r"(?P<name>[一-龥ぁ-んァ-ヶ\u30FCA-Za-zＡ-Ｚａ-ｚ]{1,6})(?P<honorific>ちゃん|くん|君)"
)
_LONG_DIGITS = re.compile(
    r"0[0-9０-９]{1,3}[\-‐ー－—\s]?[0-9０-９]{2,4}[\-‐ー－—\s]?[0-9０-９]{3,4}"
    r"|[0-9０-９]{7,}"
)
_CELL_REF = re.compile(r"([A-Z]+)")
_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_FACILITY_HEADER_ALIASES = {
    "plan_type": "plan_type",
    "計画種別": "plan_type",
    "計画タイプ": "plan_type",
    "age_class": "age_class",
    "年齢": "age_class",
    "年齢クラス": "age_class",
    "month": "month",
    "月": "month",
    "item": "item",
    "項目": "item",
    "ryoiki": "ryoiki",
    "領域": "ryoiki",
    "観点": "ryoiki",
    "領域・観点": "ryoiki",
    "text": "text",
    "本文": "text",
    "文例": "text",
    "文例本文": "text",
    "source_note": "source_note",
    "出所": "source_note",
    "出所メモ": "source_note",
}


@dataclass(frozen=True, slots=True)
class BunreiExample:
    id: str
    plan_type: str
    age_class: str
    time_unit: str | None
    month: int | None
    item: str
    ryoiki: str | None
    direction: str | None
    juu_no_sugata: str | None
    text: str
    needs_review: bool
    source: str = "bunrei"
    nursery_ref: str | None = None
    masked: bool = False

    @property
    def label(self) -> str:
        parts = [self.item]
        if self.juu_no_sugata:
            parts.append(self.juu_no_sugata)
        if self.ryoiki:
            parts.append(self.ryoiki)
        if self.direction:
            parts.append(self.direction)
        return " / ".join(parts)

    @property
    def source_label(self) -> str:
        if self.source == "facility":
            return "園文例"
        return "共通文例"

    @property
    def source_ref(self) -> str:
        return f"{self.source}.{self.id}"


@dataclass(frozen=True, slots=True)
class BunreiCandidateGroup:
    section_key: str
    section_title: str
    examples: list[BunreiExample]


@dataclass(frozen=True, slots=True)
class FacilityImportResult:
    imported: int
    skipped: int
    masked_rows: int
    warnings: list[str]


def bunrei_db_path() -> Path | None:
    env_path = os.getenv("HOIKU_BUNREI_DB_PATH")
    if env_path:
        path = Path(env_path)
        return path if path.exists() else None
    for path in DEFAULT_DB_PATHS:
        if path.exists():
            return path
    return None


def facility_db_path() -> Path | None:
    env_path = os.getenv("HOIKU_FACILITY_BUNREI_DB_PATH")
    if env_path:
        path = Path(env_path)
        return path if path.exists() else None
    for path in DEFAULT_FACILITY_DB_PATHS:
        if path.exists():
            return path
    return None


def facility_item_options() -> list[str]:
    return list(FACILITY_ITEM_OPTIONS)


def facility_import_template_csv() -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(FACILITY_IMPORT_HEADERS)
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def facility_import_template_xlsx() -> bytes:
    return _build_xlsx([list(FACILITY_IMPORT_HEADERS)])


def is_bunrei_available() -> bool:
    return bunrei_db_path() is not None


def _connect() -> sqlite3.Connection:
    path = bunrei_db_path()
    if path is None:
        raise FileNotFoundError("文例データベースが見つかりません")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _connect_facility() -> sqlite3.Connection | None:
    path = facility_db_path()
    if path is None:
        return None
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _facility_db_write_path() -> Path:
    env_path = os.getenv("HOIKU_FACILITY_BUNREI_DB_PATH")
    if env_path:
        return Path(env_path)
    existing_path = facility_db_path()
    if existing_path is not None:
        return existing_path
    return DEFAULT_FACILITY_DB_PATHS[-1]


def _ensure_facility_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        create table if not exists bunrei_facility (
            id text primary key,
            nursery_ref text not null,
            visibility text not null default 'facility_private',
            plan_type text,
            age_class text,
            month integer,
            item text,
            ryoiki text,
            text text not null,
            text_provenance text not null default 'facility',
            masked integer not null default 0,
            needs_review integer not null default 1,
            source_note text,
            imported_at text not null
        )
        """
    )
    con.execute(
        "create index if not exists idx_fac on bunrei_facility(nursery_ref, plan_type, age_class, month, item)"
    )


def _mask_facility_text(text: str) -> tuple[str, bool]:
    masked = False

    def _name_sub(match: re.Match[str]) -> str:
        nonlocal masked
        masked = True
        name = match.group("name")
        prefix = ""
        if len(name) > 4:
            prefix = name[:-3]
        elif len(name) > 3 and name[0] in "とやがはをにへで":
            prefix = name[0]
        return prefix + "◯◯" + match.group("honorific")

    masked_text = _NAME_HONORIFIC.sub(_name_sub, text)
    if _LONG_DIGITS.search(masked_text):
        masked_text = _LONG_DIGITS.sub("◯◯◯", masked_text)
        masked = True
    return masked_text, masked


def add_facility_example(
    *,
    nursery_ref: str,
    plan_type: str,
    age_class: str,
    text: str,
    item: str,
    month: int | None = None,
    ryoiki: str | None = None,
    source_note: str | None = None,
) -> BunreiExample:
    clean_text = text.strip()
    if not clean_text:
        raise ValueError("文例本文を入力してください")
    masked_text, masked = _mask_facility_text(clean_text)
    example_id = _facility_row_id(nursery_ref, plan_type, age_class, month, item, ryoiki, masked_text)
    now = datetime.now(UTC).isoformat()
    path = _facility_db_write_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as con:
        _ensure_facility_table(con)
        con.execute(
            """
            insert or replace into bunrei_facility
            (id, nursery_ref, visibility, plan_type, age_class, month, item, ryoiki,
             text, text_provenance, masked, needs_review, source_note, imported_at)
            values (?, ?, 'facility_private', ?, ?, ?, ?, ?, ?, 'facility', ?, 1, ?, ?)
            """,
            (
                example_id,
                nursery_ref,
                plan_type,
                age_class,
                month,
                item,
                ryoiki.strip() if ryoiki else None,
                masked_text,
                1 if masked else 0,
                source_note.strip() if source_note else None,
                now,
            ),
        )
        con.commit()
    return BunreiExample(
        id=example_id,
        plan_type=plan_type,
        age_class=age_class,
        time_unit=None,
        month=month,
        item=item,
        ryoiki=ryoiki.strip() if ryoiki else None,
        direction=None,
        juu_no_sugata=None,
        text=masked_text,
        needs_review=True,
        source="facility",
        nursery_ref=nursery_ref,
        masked=masked,
    )


def _facility_row_id(
    nursery_ref: str,
    plan_type: str,
    age_class: str,
    month: int | None,
    item: str,
    ryoiki: str | None,
    text: str,
) -> str:
    key = f"{nursery_ref}|{plan_type}|{age_class}|{month or ''}|{item}|{ryoiki or ''}|{text}"
    return "fac_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def import_facility_examples(
    *,
    nursery_ref: str,
    filename: str,
    content: bytes,
    default_plan_type: str = "月案",
    default_age_class: str = "5歳児",
    default_month: int | None = None,
    default_item: str = "活動内容",
    default_source_note: str | None = None,
) -> FacilityImportResult:
    rows = _read_facility_import_rows(filename, content)
    imported = 0
    skipped = 0
    masked_rows = 0
    warnings: list[str] = []
    for line_no, row in enumerate(rows, start=2):
        normalized = _normalize_facility_import_row(row)
        text = normalized.get("text", "").strip()
        if not text:
            skipped += 1
            continue
        plan_type = normalized.get("plan_type") or default_plan_type
        if plan_type == "individual_plan":
            plan_type = "個別指導計画"
        if plan_type not in {"年案", "月案", "個別指導計画"}:
            warnings.append(f"{line_no}行目: 計画種別を月案として取り込みました")
            plan_type = default_plan_type
        item = normalized.get("item") or default_item
        if item not in FACILITY_ITEM_OPTIONS:
            warnings.append(f"{line_no}行目: 項目を{default_item}として取り込みました")
            item = default_item
        try:
            month = _parse_month(normalized.get("month"), default_month)
        except ValueError:
            warnings.append(f"{line_no}行目: 月を読み取れなかったため既定値を使いました")
            month = default_month
        example = add_facility_example(
            nursery_ref=nursery_ref,
            plan_type=plan_type,
            age_class=normalized.get("age_class") or default_age_class,
            month=month,
            item=item,
            ryoiki=normalized.get("ryoiki") or None,
            text=text,
            source_note=normalized.get("source_note") or default_source_note,
        )
        imported += 1
        if example.masked:
            masked_rows += 1
    return FacilityImportResult(imported=imported, skipped=skipped, masked_rows=masked_rows, warnings=warnings)


def _read_facility_import_rows(filename: str, content: bytes) -> list[dict[str, str]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        return _read_csv_rows(content)
    if suffix == ".xlsx":
        return _read_xlsx_rows(content)
    raise ValueError("CSVまたはExcel（.xlsx）ファイルを選択してください")


def _read_csv_rows(content: bytes) -> list[dict[str, str]]:
    for encoding in ("utf-8-sig", "cp932"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("CSVの文字コードを読み取れませんでした")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    return [dict(row) for row in reader]


def _read_xlsx_rows(content: bytes) -> list[dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        sheet_path = _first_xlsx_sheet_path(archive)
        shared_strings = _xlsx_shared_strings(archive)
        sheet_root = ET.fromstring(archive.read(sheet_path))
    rows: list[list[str]] = []
    for row in sheet_root.findall(f".//{{{_XLSX_MAIN_NS}}}row"):
        values: dict[int, str] = {}
        fallback_index = 0
        for cell in row.findall(f"{{{_XLSX_MAIN_NS}}}c"):
            ref = cell.attrib.get("r", "")
            col_index = _xlsx_col_index(ref) if ref else fallback_index
            values[col_index] = _xlsx_cell_value(cell, shared_strings)
            fallback_index = col_index + 1
        if values:
            max_index = max(values)
            rows.append([values.get(index, "") for index in range(max_index + 1)])
    if not rows:
        return []
    headers = [value.strip() for value in rows[0]]
    return [
        {headers[index]: value.strip() for index, value in enumerate(row) if index < len(headers) and headers[index]}
        for row in rows[1:]
    ]


def _build_xlsx(rows: list[list[str]]) -> bytes:
    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{_xlsx_cell_ref(row_index, col_index)}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            for col_index, value in enumerate(row)
        )
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(sheet_rows)}</sheetData>"
        "</worksheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="園文例" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()


def _xlsx_cell_ref(row_index: int, col_index: int) -> str:
    name = ""
    col = col_index + 1
    while col:
        col, remainder = divmod(col - 1, 26)
        name = chr(ord("A") + remainder) + name
    return f"{name}{row_index}"


def _first_xlsx_sheet_path(archive: zipfile.ZipFile) -> str:
    names = set(archive.namelist())
    if "xl/workbook.xml" not in names or "xl/_rels/workbook.xml.rels" not in names:
        if "xl/worksheets/sheet1.xml" in names:
            return "xl/worksheets/sheet1.xml"
        raise ValueError("Excelファイルのシートを読み取れませんでした")
    workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
    sheet = workbook_root.find(f".//{{{_XLSX_MAIN_NS}}}sheet")
    rel_id = sheet.attrib.get(f"{{{_XLSX_REL_NS}}}id") if sheet is not None else None
    if not rel_id:
        return "xl/worksheets/sheet1.xml"
    rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for rel in rels_root.findall(f"{{{_PACKAGE_REL_NS}}}Relationship"):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib.get("Target", "")
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = f"xl/{target}"
            return target.replace("\\", "/")
    return "xl/worksheets/sheet1.xml"


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall(f"{{{_XLSX_MAIN_NS}}}si"):
        strings.append("".join(item.itertext()))
    return strings


def _xlsx_cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        inline = cell.find(f"{{{_XLSX_MAIN_NS}}}is")
        return "".join(inline.itertext()).strip() if inline is not None else ""
    value = cell.find(f"{{{_XLSX_MAIN_NS}}}v")
    if value is None or value.text is None:
        return ""
    text = value.text.strip()
    if cell_type == "s":
        try:
            return shared_strings[int(text)].strip()
        except (IndexError, ValueError):
            return ""
    return text


def _xlsx_col_index(cell_ref: str) -> int:
    match = _CELL_REF.match(cell_ref)
    if not match:
        return 0
    index = 0
    for char in match.group(1):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _normalize_facility_import_row(row: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_key, raw_value in row.items():
        key = _FACILITY_HEADER_ALIASES.get((raw_key or "").strip())
        if not key:
            continue
        normalized[key] = (raw_value or "").strip()
    return normalized


def _parse_month(raw_value: str | None, default_month: int | None) -> int | None:
    value = (raw_value or "").strip().replace("月", "")
    if not value:
        return default_month
    month = int(float(value))
    if month < 1 or month > 12:
        raise ValueError("month out of range")
    return month


def _row_to_example(row: sqlite3.Row) -> BunreiExample:
    keys = set(row.keys())
    return BunreiExample(
        id=row["id"],
        plan_type=row["plan_type"],
        age_class=row["age_class"],
        time_unit=row["time_unit"],
        month=row["month"],
        item=row["item"],
        ryoiki=row["ryoiki"],
        direction=row["direction"],
        juu_no_sugata=row["juu_no_sugata"] if "juu_no_sugata" in keys else None,
        text=row["text"],
        needs_review=bool(row["needs_review"]),
        source=row["source"] if "source" in keys else "bunrei",
        nursery_ref=row["nursery_ref"] if "nursery_ref" in keys else None,
        masked=bool(row["masked"]) if "masked" in keys else False,
    )


def age_class_options(plan_type: str) -> list[str]:
    with closing(_connect()) as con:
        rows = con.execute(
            "select distinct age_class from bunrei where plan_type = ? order by age_class",
            (plan_type,),
        ).fetchall()
    return [row["age_class"] for row in rows]


def count_examples() -> int:
    with closing(_connect()) as con:
        return int(con.execute("select count(*) from bunrei").fetchone()[0])


def _fetch_examples(
    *,
    plan_type: str,
    age_class: str,
    items: tuple[str, ...],
    nursery_ref: str | None = None,
    month: int | None = None,
    time_unit: str | None = None,
    limit: int = 8,
) -> list[BunreiExample]:
    if not items:
        return []
    placeholders = ",".join("?" for _ in items)
    params: list[object] = [plan_type, age_class, *items]
    where = ["plan_type = ?", "age_class = ?", f"item in ({placeholders})"]
    if month is not None:
        where.append("(month = ? or month is null)")
        params.append(month)
    if time_unit is not None:
        where.append("time_unit = ?")
        params.append(time_unit)

    sql = f"""
        select
            id, plan_type, age_class, time_unit, month, item, ryoiki, direction,
            juu_no_sugata, text, needs_review, 'bunrei' as source, null as nursery_ref, 0 as masked
        from bunrei
        where {" and ".join(where)}
        order by item, ryoiki is null, ryoiki, direction, id
        limit ?
    """
    params.append(limit)
    with closing(_connect()) as con:
        shared_rows = con.execute(sql, params).fetchall()

    facility_rows: list[sqlite3.Row] = []
    if nursery_ref:
        facility_con = _connect_facility()
        if facility_con is not None:
            with closing(facility_con) as con:
                facility_where = [
                    "nursery_ref = ?",
                    "visibility = 'facility_private'",
                    "plan_type = ?",
                    "age_class = ?",
                    f"item in ({placeholders})",
                ]
                facility_params: list[object] = [nursery_ref, plan_type, age_class, *items]
                if month is not None:
                    facility_where.append("(month = ? or month is null)")
                    facility_params.append(month)
                facility_sql = f"""
                    select
                        id, plan_type, age_class, null as time_unit, month, item, ryoiki,
                        null as direction, null as juu_no_sugata, text, needs_review,
                        'facility' as source, nursery_ref, masked
                    from bunrei_facility
                    where {" and ".join(facility_where)}
                    order by item, ryoiki is null, ryoiki, id
                    limit ?
                """
                facility_params.append(limit)
                facility_rows = con.execute(facility_sql, facility_params).fetchall()

    examples = [_row_to_example(row) for row in facility_rows]
    examples.extend(_row_to_example(row) for row in shared_rows)
    return examples[:limit]


def monthly_candidate_groups(
    age_class: str,
    month: int,
    *,
    nursery_ref: str | None = None,
    limit_per_section: int = 8,
) -> list[BunreiCandidateGroup]:
    groups: list[BunreiCandidateGroup] = []
    for section_key, section_title, items in MONTHLY_SECTION_ITEMS:
        groups.append(
            BunreiCandidateGroup(
                section_key=section_key,
                section_title=section_title,
                examples=_fetch_examples(
                    plan_type="月案",
                    age_class=age_class,
                    nursery_ref=nursery_ref,
                    month=month,
                    items=items,
                    limit=limit_per_section,
                ),
            )
        )
    return groups


def annual_candidate_groups(
    age_class: str,
    *,
    nursery_ref: str | None = None,
    limit_per_section: int = 5,
) -> list[BunreiCandidateGroup]:
    groups: list[BunreiCandidateGroup] = []
    definitions = annual_section_definitions()
    definition_map = {definition.key: definition for definition in definitions}
    for definition in definitions:
        if definition.key == "annual_goal":
            groups.append(
                BunreiCandidateGroup(
                    section_key=definition.key,
                    section_title=definition.title,
                    examples=_fetch_examples(
                        plan_type="年案",
                        age_class=age_class,
                        nursery_ref=nursery_ref,
                        time_unit="通年",
                        items=ANNUAL_SECTION_ITEMS["annual_goal"],
                        limit=limit_per_section,
                    ),
                )
            )
            continue
        term_key, suffix = _annual_term_and_suffix(definition.key)
        time_unit = _annual_time_unit(term_key)
        month = _annual_term_month(term_key)
        groups.append(
            BunreiCandidateGroup(
                section_key=definition.key,
                section_title=definition_map[definition.key].title,
                examples=_fetch_examples(
                    plan_type="年案",
                    age_class=age_class,
                    nursery_ref=nursery_ref,
                    month=month,
                    time_unit=time_unit,
                    items=ANNUAL_SECTION_ITEMS.get(suffix, ()),
                    limit=limit_per_section,
                ),
            )
        )
    return groups


def selected_examples(
    selection: dict[str, list[str]],
    *,
    nursery_ref: str | None = None,
) -> dict[str, list[BunreiExample]]:
    ids = [example_id for values in selection.values() for example_id in values]
    if not ids:
        return {section_key: [] for section_key in selection}
    placeholders = ",".join("?" for _ in ids)
    with closing(_connect()) as con:
        rows = con.execute(
            f"""
            select
                id, plan_type, age_class, time_unit, month, item, ryoiki, direction,
                juu_no_sugata, text, needs_review, 'bunrei' as source, null as nursery_ref, 0 as masked
            from bunrei
            where id in ({placeholders})
            """,
            ids,
        ).fetchall()
    by_id = {row["id"]: _row_to_example(row) for row in rows}
    if nursery_ref:
        facility_con = _connect_facility()
        if facility_con is not None:
            with closing(facility_con) as con:
                facility_rows = con.execute(
                    f"""
                    select
                        id, plan_type, age_class, null as time_unit, month, item, ryoiki,
                        null as direction, null as juu_no_sugata, text, needs_review,
                        'facility' as source, nursery_ref, masked
                    from bunrei_facility
                    where nursery_ref = ?
                      and visibility = 'facility_private'
                      and id in ({placeholders})
                    """,
                    [nursery_ref, *ids],
                ).fetchall()
            by_id.update({row["id"]: _row_to_example(row) for row in facility_rows})
    return {
        section_key: [by_id[example_id] for example_id in values if example_id in by_id]
        for section_key, values in selection.items()
    }


def build_document_from_bunrei(
    *,
    document_type: DocumentType,
    title: str,
    owner_name: str,
    classroom_ref: str,
    user: StaffUser,
    section_definitions: list[SectionDefinition],
    selected_by_section: dict[str, list[BunreiExample]],
    school_year: int | None = None,
    target_month: str | None = None,
) -> PlanDocument:
    sections: list[SectionBlock] = []
    confirmation_items: list[str] = []
    for definition in section_definitions:
        examples = selected_by_section.get(definition.key, [])
        if examples:
            body = "\n".join(example.text for example in examples)
            source_refs = [example.source_ref for example in examples]
            needs_confirmation = any(example.needs_review for example in examples)
            editor_note = "文例から作成しています。園やクラスの実態に合わせて修正してください。"
        else:
            body = ""
            source_refs = ["bunrei.unselected"]
            needs_confirmation = True
            editor_note = "文例を選ぶか、本文を入力してください。"
            confirmation_items.append(definition.title)
        sections.append(
            SectionBlock(
                section_key=definition.key,
                title=definition.title,
                body=body,
                source_refs=source_refs,
                evidence_tags=evidence_tags_for(source_refs),
                needs_confirmation=needs_confirmation,
                editor_note=editor_note,
            )
        )

    return PlanDocument(
        id=0,
        document_type=document_type,
        title=title,
        status=DocumentStatus.DRAFT,
        nursery_ref=user.nursery_ref,
        classroom_ref=classroom_ref,
        actor_ref=user.actor_ref,
        owner_name=owner_name,
        sections=sections,
        confirmation_items=confirmation_items,
        school_year=school_year,
        target_month=target_month,
    )


def _annual_term_and_suffix(section_key: str) -> tuple[str, str]:
    parts = section_key.split("_", 2)
    if len(parts) < 3:
        return "term_1", section_key
    return f"{parts[0]}_{parts[1]}", parts[2]


def _annual_time_unit(term_key: str) -> str:
    order = [key for key, _label in ANNUAL_TERM_ORDER]
    labels = ["Ⅰ期", "Ⅱ期", "Ⅲ期", "Ⅳ期"]
    if term_key in order:
        return labels[order.index(term_key)]
    return "Ⅰ期"


def _annual_term_month(term_key: str) -> int:
    order = [key for key, _label in ANNUAL_TERM_ORDER]
    months = [4, 7, 10, 1]
    if term_key in order:
        return months[order.index(term_key)]
    return 4
