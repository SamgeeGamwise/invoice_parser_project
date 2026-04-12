import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class SpreadsheetReaderService:
    """
    Minimal XLSX reader for local reference data.

    The assignment only needs a couple of worksheets, so this keeps the project
    lightweight and avoids introducing another runtime dependency just to read
    local spreadsheets.
    """

    def read_rows(self, path: Path, sheet_index: int = 0) -> list[list[str]]:
        with zipfile.ZipFile(path) as archive:
            shared_strings = self._read_shared_strings(archive)
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            relationship_map = {
                rel.attrib["Id"]: rel.attrib["Target"]
                for rel in relationships
            }
            sheets = workbook.find("a:sheets", _NS)
            if sheets is None:
                return []
            sheet = sheets.findall("a:sheet", _NS)[sheet_index]
            relationship_id = sheet.attrib[
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            ]
            target = "xl/" + relationship_map[relationship_id]
            sheet_root = ET.fromstring(archive.read(target))
            data = sheet_root.find("a:sheetData", _NS)
            if data is None:
                return []

            rows: list[list[str]] = []
            for row in data.findall("a:row", _NS):
                rows.append(self._read_row(row, shared_strings))
            return rows

    def _read_shared_strings(self, archive: zipfile.ZipFile) -> list[str]:
        if "xl/sharedStrings.xml" not in archive.namelist():
            return []

        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        values: list[str] = []
        for item in root.findall("a:si", _NS):
            values.append("".join(node.text or "" for node in item.iterfind(".//a:t", _NS)))
        return values

    def _read_row(self, row: ET.Element, shared_strings: list[str]) -> list[str]:
        values: list[str] = []
        for cell in row.findall("a:c", _NS):
            cell_type = cell.attrib.get("t")
            value_node = cell.find("a:v", _NS)
            value = value_node.text if value_node is not None else ""
            if cell_type == "s" and value:
                value = shared_strings[int(value)]
            values.append((value or "").strip())
        return values
