"""Document formats the private websites may serve, with invented content (browser_env.document_text).

The public scenario serves HTML pages, so the sandbox runs never exercised these. The brief says the private dataset
uses other software, layouts and content: the text handed to extraction must keep values and their row together
whatever the file format.
"""
import io
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from browser_env import document_text  # noqa: E402


def zipped(files: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def xlsx(rows: list[list[str]]) -> bytes:
    """A minimal workbook: text cells go through the shared-strings table, numbers stay inline (as Excel writes)."""
    strings: list[str] = []
    cells_xml = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            try:
                float(value)
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            except ValueError:
                strings.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
        cells_xml.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    return zipped({
        "[Content_Types].xml": "<Types/>",
        "_rels/.rels": "<Relationships/>",
        "xl/workbook.xml": f'<workbook {ns}><sheets><sheet name="Preise" sheetId="1"/></sheets></workbook>',
        "xl/styles.xml": f"<styleSheet {ns}><fonts count=\"1\"><font><name val=\"Calibri\"/></font></fonts></styleSheet>",
        "xl/sharedStrings.xml": f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>",
        "xl/worksheets/sheet1.xml": f"<worksheet {ns}><sheetData>{''.join(cells_xml)}</sheetData></worksheet>",
    })


def docx(paragraphs: list[str], table: list[list[str]]) -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    body += "<w:tbl>" + "".join(
        "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row) + "</w:tr>"
        for row in table) + "</w:tbl>"
    return zipped({
        "[Content_Types].xml": "<Types/>",
        "_rels/.rels": "<Relationships/>",
        "word/styles.xml": f"<w:styles {w}><w:style><w:name w:val=\"Normal\"/></w:style></w:styles>",
        "word/document.xml": f"<w:document {w}><w:body>{body}</w:body></w:document>",
    })


def line_with(text: str, *values: str) -> str | None:
    """The first line of `text` that holds every value: values that belong together must stay on one line."""
    return next((line for line in text.splitlines() if all(value in line for value in values)), None)


class Spreadsheets(unittest.TestCase):
    def test_xlsx_rows_keep_their_text_and_number_cells_together(self):
        text = document_text(xlsx([["Material", "Staffel", "Preis"], ["MAT-Q7", "1", "412.5"], ["MAT-Q7", "50", "398"]]),
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "preise.xlsx")
        self.assertIsNotNone(line_with(text, "MAT-Q7", "50", "398"), text)
        self.assertIsNotNone(line_with(text, "Material", "Staffel", "Preis"), text)

    def test_xlsx_internals_do_not_reach_the_extraction(self):
        text = document_text(xlsx([["Material"], ["MAT-Q7"]]), "", "preise.xlsx")
        self.assertNotIn("Calibri", text)  # styles.xml is formatting, not content


class WordDocuments(unittest.TestCase):
    def test_docx_paragraphs_and_table_rows_stay_readable(self):
        text = document_text(docx(["Freigabe für Ausschreibung T-9001"], [["Schlüssel", "Wert je Ende"], ["RX-11", "7 mm"]]),
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "freigabe.docx")
        self.assertIn("Freigabe für Ausschreibung T-9001", text)
        self.assertIsNotNone(line_with(text, "RX-11", "7 mm"), text)
        self.assertNotIn("Normal", text)  # styles.xml


class TextFiles(unittest.TestCase):
    def test_csv_from_german_excel_in_windows_1252_keeps_its_umlauts(self):
        data = "Größe;Menge;Preis\nZylinder Ø 63;12;1.234,50\n".encode("cp1252")
        text = document_text(data, "text/csv", "liste.csv")
        self.assertIn("Größe", text)
        self.assertIsNotNone(line_with(text, "Ø 63", "12", "1.234,50"), text)

    def test_utf8_csv_is_unchanged(self):
        text = document_text("Größe;Menge\nA;3\n".encode("utf-8"), "text/csv", "liste.csv")
        self.assertIn("Größe", text)


class Archives(unittest.TestCase):
    def test_a_file_three_archives_deep_is_still_read(self):
        inner = zipped({"konditionen.txt": "Kondition K-55 gültig ab 01.03."})
        middle = zipped({"inner.zip": inner})
        outer = zipped({"middle.zip": middle})
        text = document_text(zipped({"outer.zip": outer}), "application/zip", "paket.zip")
        self.assertIn("K-55", text)


class HtmlTables(unittest.TestCase):
    def test_table_cells_of_one_row_stay_on_one_line(self):
        html = ("<html><body><table><tr><th>Position</th><th>Menge</th></tr>"
                "<tr><td>02.04</td><td>17</td></tr></table></body></html>")
        text = document_text(html.encode(), "text/html", "x.html")
        self.assertIsNotNone(line_with(text, "02.04", "17"), text)


if __name__ == "__main__":
    unittest.main()
