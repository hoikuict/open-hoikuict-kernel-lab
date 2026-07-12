import csv
import io
import os
import stat
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from auth import Role, StaffUser
import routers.data_transfers as data_transfers_module
from models import Child, ChildStatus, Classroom, DataTransferLog, Family


def _csv_bytes(rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _minimal_ninka_workbook():
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/worksheets/sheet2.xml" '
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
        '<sheets>'
        '<sheet name="施設情報" sheetId="1" r:id="rId1"/>'
        '<sheet name="シート４" sheetId="2" r:id="rId2"/>'
        '</sheets>'
        '</workbook>'
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet2.xml"/>'
        '</Relationships>'
    )
    facility_sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        '<row r="1"><c r="C1" t="inlineStr"><is><t>更新日時</t></is></c><c r="D1" t="inlineStr"><is><t>年度</t></is></c></row>'
        '<row r="2"><c r="C2" t="inlineStr"><is><t></t></is></c><c r="D2"><v>2025</v></c></row>'
        '</sheetData>'
        '</worksheet>'
    )
    sheet4_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        '<row r="25"><c r="H25"><v>0</v></c><c r="L25"><v>0</v></c><c r="P25"><v>0</v></c></row>'
        '<row r="27"><c r="H27"><v>0</v></c><c r="L27"><v>0</v></c><c r="P27"><v>0</v></c></row>'
        '<row r="29"><c r="H29"><v>0</v></c><c r="L29"><v>0</v></c><c r="P29"><v>0</v></c></row>'
        '<row r="31"><c r="H31"><v>0</v></c><c r="L31"><v>0</v></c><c r="P31"><v>0</v></c></row>'
        '<row r="33"><c r="H33"><v>0</v></c><c r="L33"><v>0</v></c><c r="P33"><v>0</v></c></row>'
        '<row r="35"><c r="H35"><v>0</v></c><c r="L35"><v>0</v></c><c r="P35"><v>0</v></c></row>'
        '<row r="37"><c r="H37"><f>H25+H27+H29+H31+H33+H35</f><v>0</v></c><c r="L37"><f>L25+L27+L29+L31+L33+L35</f><v>0</v></c><c r="P37"><f>P25+P27+P29+P31+P33+P35</f><v>0</v></c></row>'
        '</sheetData>'
        '</worksheet>'
    )

    buffer = io.BytesIO()
    with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", rels_xml)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        archive.writestr("xl/worksheets/sheet1.xml", facility_sheet_xml)
        archive.writestr("xl/worksheets/sheet2.xml", sheet4_xml)
    return buffer.getvalue()


def _xlsx_cell_value(content, sheet_path, cell_ref):
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(io.BytesIO(content)) as archive:
        root = ET.fromstring(archive.read(sheet_path))
    cell = root.find(f".//main:c[@r='{cell_ref}']", namespace)
    if cell is None:
        return ""
    if cell.attrib.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", namespace))
    value_node = cell.find("main:v", namespace)
    return value_node.text if value_node is not None else ""


class DataTransferTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        self.app = FastAPI()
        self.app.include_router(data_transfers_module.router)

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.app.dependency_overrides[data_transfers_module.get_session] = override_get_session
        self.app.dependency_overrides[data_transfers_module.get_current_staff_user] = (
            lambda: StaffUser(
                role=Role.CAN_EDIT,
                name="台帳担当",
                can_manage_child_records=True,
            )
        )
        self.client = TestClient(self.app)

        with Session(self.engine) as session:
            classroom = Classroom(name="ひよこ組", display_order=1)
            family = Family(family_name="田中家", home_phone="03-1111-1111")
            session.add(classroom)
            session.add(family)
            session.flush()
            child = Child(
                last_name="田中",
                first_name="さくら",
                last_name_kana="タナカ",
                first_name_kana="サクラ",
                birth_date=date(2021, 4, 5),
                enrollment_date=date(2024, 4, 1),
                status=ChildStatus.graduated,
                classroom_id=classroom.id,
                family_id=family.id,
                extra_data={"allergy": [], "medical_notes": ""},
            )
            session.add(child)
            session.commit()
            self.classroom_id = classroom.id
            self.family_id = family.id
            self.child_id = child.id

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_exports_children_as_csv(self):
        response = self.client.get("/data-transfers/export/children.csv")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
        text = response.content.decode("utf-8-sig")
        self.assertIn("姓,名,姓カナ", text)
        self.assertIn("田中,さくら,タナカ", text)
        self.assertIn("卒園", text)

    def test_data_transfer_page_has_dataset_visibility_controls(self):
        response = self.client.get("/data-transfers/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("表示するデータ", response.text)
        self.assertIn('data-dataset-toggle', response.text)
        self.assertIn('data-dataset-row="families"', response.text)
        self.assertIn("認可施設帳票入力連携", response.text)

    def test_exports_ninka_workbook_with_child_counts(self):
        with Session(self.engine) as session:
            session.add(
                Child(
                    last_name="鈴木",
                    first_name="あお",
                    last_name_kana="スズキ",
                    first_name_kana="アオ",
                    birth_date=date(2025, 5, 1),
                    enrollment_date=date(2026, 4, 1),
                    status=ChildStatus.enrolled,
                    classroom_id=self.classroom_id,
                    family_id=self.family_id,
                    extra_data={"allergy": [], "medical_notes": ""},
                )
            )
            session.commit()

        response = self.client.post(
            "/data-transfers/ninka/export",
            data={"fiscal_year": "2026"},
            files={
                "template_file": (
                    "ninka_input.xlsx",
                    _minimal_ninka_workbook(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertEqual(_xlsx_cell_value(response.content, "xl/worksheets/sheet1.xml", "D2"), "2026")
        self.assertTrue(_xlsx_cell_value(response.content, "xl/worksheets/sheet1.xml", "C2"))
        self.assertEqual(_xlsx_cell_value(response.content, "xl/worksheets/sheet2.xml", "H25"), "1")
        self.assertEqual(_xlsx_cell_value(response.content, "xl/worksheets/sheet2.xml", "L25"), "1")
        self.assertEqual(_xlsx_cell_value(response.content, "xl/worksheets/sheet2.xml", "P25"), "1")
        self.assertEqual(_xlsx_cell_value(response.content, "xl/worksheets/sheet2.xml", "H37"), "1")
        self.assertEqual(_xlsx_cell_value(response.content, "xl/worksheets/sheet2.xml", "L37"), "1")
        self.assertEqual(_xlsx_cell_value(response.content, "xl/worksheets/sheet2.xml", "P37"), "1")

    def test_import_new_child_defaults_blank_status_to_enrolled(self):
        rows = [
            ["ID", "姓", "名", "姓カナ", "名カナ", "生年月日", "入園日", "退園日", "在園状態", "クラス名", "家庭ID", "家庭名", "住所", "電話番号"],
            ["", "佐藤", "みお", "サトウ", "ミオ", "2022-05-06", "2025-04-01", "", "", "ひよこ組", str(self.family_id), "田中家", "", ""],
        ]
        response = self.client.post(
            "/data-transfers/import/children/commit",
            files={"file": ("children.csv", _csv_bytes(rows), "text/csv")},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with Session(self.engine) as session:
            child = session.exec(select(Child).where(Child.last_name_kana == "サトウ")).first()
            log = session.exec(select(DataTransferLog)).first()

        self.assertIsNotNone(child)
        self.assertEqual(child.status, ChildStatus.enrolled)
        self.assertEqual(child.family_id, self.family_id)
        self.assertIsNotNone(log)
        self.assertEqual(log.result, "success")
        self.assertEqual(log.created_count, 1)

    def test_import_existing_child_keeps_status_when_blank(self):
        rows = [
            ["ID", "姓", "名", "姓カナ", "名カナ", "生年月日", "入園日", "退園日", "在園状態", "クラス名", "家庭ID", "家庭名", "住所", "電話番号"],
            [str(self.child_id), "田中", "さくら", "タナカ", "サクラ", "2021-04-05", "2024-04-01", "", "", "ひよこ組", str(self.family_id), "田中家", "東京都", ""],
        ]
        response = self.client.post(
            "/data-transfers/import/children/commit",
            files={"file": ("children.csv", _csv_bytes(rows), "text/csv")},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with Session(self.engine) as session:
            child = session.get(Child, self.child_id)

        self.assertEqual(child.status, ChildStatus.graduated)
        self.assertEqual(child.home_address, "東京都")

    def test_family_id_and_name_conflict_is_validation_error(self):
        with Session(self.engine) as session:
            other = Family(family_name="佐藤家", home_phone="03-2222-2222")
            session.add(other)
            session.commit()

        rows = [
            ["ID", "姓", "名", "姓カナ", "名カナ", "生年月日", "入園日", "退園日", "在園状態", "クラス名", "家庭ID", "家庭名", "住所", "電話番号"],
            ["", "山田", "あおい", "ヤマダ", "アオイ", "2022-01-01", "2025-04-01", "", "在園", "ひよこ組", str(self.family_id), "佐藤家", "", ""],
        ]
        response = self.client.post(
            "/data-transfers/import/children/preview",
            files={"file": ("children.csv", _csv_bytes(rows), "text/csv")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("家庭IDの家庭名と一致しません", response.text)

    def test_import_rejects_duplicate_headers(self):
        rows = [
            ["ID", "クラス名", "クラス名", "表示順"],
            ["", "ひよこ組", "うさぎ組", "1"],
        ]

        response = self.client.post(
            "/data-transfers/import/classrooms/preview",
            files={"file": ("classrooms.csv", _csv_bytes(rows), "text/csv")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("列名が重複しています", response.text)

    def test_preview_cleanup_removes_only_expired_files_and_uses_private_mode(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"HOIKUICT_PREVIEW_DIR": directory},
        ):
            old_path = Path(directory) / "old.json"
            old_path.write_text("{}", encoding="utf-8")
            now = time.time()
            os.utime(old_path, (now - 90000, now - 90000))

            token = data_transfers_module._save_preview_file(
                "children",
                "children.csv",
                b"sample",
            )
            new_path = Path(directory) / f"{token}.json"
            self.assertTrue(new_path.exists())
            self.assertFalse(old_path.exists())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(new_path.stat().st_mode), 0o600)
            filename, content = data_transfers_module._load_preview_file(token, "children")
            self.assertEqual(filename, "children.csv")
            self.assertEqual(content, b"sample")


if __name__ == "__main__":
    unittest.main()
