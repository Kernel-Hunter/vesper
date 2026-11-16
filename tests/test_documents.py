"""Office documents are read as text (offline, documents built in the test)."""

import sys
import zipfile

import pytest

from vesper import documents
from vesper.workspace import ToolError, Workspace

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
REL = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def make_zip(path, parts: dict):
    with zipfile.ZipFile(path, "w") as z:
        for name, body in parts.items():
            z.writestr(name, body)
    return path


def docx(path):
    return make_zip(path, {"word/document.xml": f"""<w:document {W}><w:body>
        <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Budget 2026</w:t></w:r></w:p>
        <w:p><w:r><w:t xml:space="preserve">Rent is </w:t></w:r><w:r><w:t>1200</w:t></w:r><w:r><w:tab/><w:t>EUR</w:t></w:r></w:p>
        <w:p><w:pPr><w:numPr/></w:pPr><w:r><w:t>first item</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Month</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Cost</w:t></w:r></w:p></w:tc></w:tr>
               <w:tr><w:tc><w:p><w:r><w:t>May</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>40</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>"""})


def test_word_document_keeps_headings_lists_and_tables(tmp_path):
    text = documents.extract(docx(tmp_path / "budget.docx"))
    assert text.splitlines()[0] == "# Budget 2026"
    assert "Rent is 1200\tEUR" in text and "- first item" in text
    assert "| Month | Cost |" in text and "| May | 40 |" in text


def test_excel_sheets_with_shared_strings_numbers_and_gaps(tmp_path):
    S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    path = make_zip(tmp_path / "data.xlsx", {
        "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="Costs" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships {REL}><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/sharedStrings.xml": f"<sst {S}><si><t>Item</t></si><si><t>Coffee</t></si></sst>",
        "xl/worksheets/sheet1.xml": f'''<worksheet {S}><sheetData>
            <row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="inlineStr"><is><t>Paid</t></is></c></row>
            <row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>3.5</v></c><c r="C2" t="b"><v>1</v></c></row>
            </sheetData></worksheet>'''})
    text = documents.extract(path)
    assert "## Sheet: Costs" in text
    assert "1\tItem |  | Paid" in text and "2\tCoffee | 3.5 | TRUE" in text


def test_powerpoint_slides_in_order_with_notes(tmp_path):
    A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
    R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    slide = lambda t: f"<p:sld {P} {A}><a:p><a:r><a:t>{t}</a:t></a:r></a:p></p:sld>"  # noqa: E731
    path = make_zip(tmp_path / "talk.pptx", {
        "ppt/presentation.xml": f'<p:presentation {P} {R}><p:sldIdLst><p:sldId r:id="rB"/><p:sldId r:id="rA"/></p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": f'<Relationships {REL}><Relationship Id="rA" Target="slides/slide1.xml"/>'
                                           f'<Relationship Id="rB" Target="slides/slide2.xml"/></Relationships>',
        "ppt/slides/slide1.xml": slide("Second shown"),
        "ppt/slides/slide2.xml": slide("Title slide"),
        "ppt/slides/_rels/slide2.xml.rels": f'<Relationships {REL}><Relationship Id="n" Target="../notesSlides/notesSlide1.xml"/></Relationships>',
        "ppt/notesSlides/notesSlide1.xml": slide("Say hello first"),
    })
    text = documents.extract(path)
    assert text.index("Title slide") < text.index("Second shown")  # the order PowerPoint shows
    assert "## Slide 1\nTitle slide" in text and "Notes: Say hello first" in text


def test_opendocument_text_and_rtf(tmp_path):
    T = 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
    O = 'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
    path = make_zip(tmp_path / "note.odt", {"content.xml": f'''<office:document-content {O} {T}><office:body><office:text>
        <text:h text:outline-level="2">Plan</text:h><text:p>one<text:s text:c="2"/>two</text:p></office:text></office:body></office:document-content>'''})
    assert documents.extract(path) == "## Plan\none  two"  # text:s with c=2 is two spaces
    rtf = r"{\rtf1\ansi{\fonttbl{\f0 Helvetica;}}{\*\generator x;}\f0 Caf\'e9 \b bold\b0\par second\tab line \u8364? done}"
    assert documents.rtf_text(rtf) == "Café bold\nsecond\tline € done"


def test_damaged_and_unsupported_files_say_what_to_do(tmp_path):
    (tmp_path / "broken.docx").write_bytes(b"not a zip")
    with pytest.raises(documents.DocumentError, match="damaged"):
        documents.extract(tmp_path / "broken.docx")
    (tmp_path / "old.xls").write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(documents.DocumentError, match=r"\.xlsx or \.csv"):
        documents.extract(tmp_path / "old.xls")
    (tmp_path / "doc.pages").write_bytes(b"PK")
    with pytest.raises(documents.DocumentError, match="Export"):
        documents.extract(tmp_path / "doc.pages")


def test_a_zip_bomb_part_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_PART_BYTES", 1000)
    path = make_zip(tmp_path / "big.docx", {"word/document.xml": f"<w:document {W}><w:body>" + "<w:p/>" * 500 + "</w:body></w:document>"})
    with pytest.raises(documents.DocumentError, match="too large"):
        documents.extract(path)


def test_read_file_pages_through_a_document_and_reports_errors(tmp_path):
    ws = Workspace(tmp_path)
    docx(tmp_path / "budget.docx")
    out = ws.read_file("budget.docx", offset=1, limit=2)
    assert out.startswith("1\t# Budget 2026\n2\tRent is 1200") and "offset=3" in out
    (tmp_path / "broken.pptx").write_bytes(b"nope")
    with pytest.raises(ToolError, match="damaged"):
        ws.read_file("broken.pptx")


@pytest.mark.skipif(sys.platform != "darwin", reason="textutil is part of macOS")
def test_old_word_files_are_converted_on_macos(tmp_path):
    import subprocess

    (tmp_path / "in.txt").write_text("Hello from an old Word file\n")
    subprocess.run(["textutil", "-convert", "doc", "-output", str(tmp_path / "old.doc"), str(tmp_path / "in.txt")], check=True)
    assert "Hello from an old Word file" in documents.extract(tmp_path / "old.doc")


def test_hostile_or_odd_documents_stay_small_and_correct(tmp_path):
    # An emoji in RTF is two \u escapes (a UTF-16 pair); they must join into one character.
    assert documents.rtf_text(r"{\rtf1 Hi \u-10179?\u-8704? there}") == "Hi \U0001F600 there"
    # A cell far to the right must not make a row of sixteen thousand empty columns.
    S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    path = make_zip(tmp_path / "wide.xlsx", {
        "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships {REL}><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'''<worksheet {S}><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>ok</t></is></c>
            <c r="ZZZZZ1" t="inlineStr"><is><t>far</t></is></c></row></sheetData></worksheet>'''})
    text = documents.extract(path)
    assert "ok" in text and len(text) < 200
    # A huge repeat count for spaces is capped.
    T = 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
    O = 'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
    path = make_zip(tmp_path / "spaces.odt", {"content.xml": f'''<office:document-content {O} {T}><office:body><office:text>
        <text:p>a<text:s text:c="999999999"/>b</text:p></office:text></office:body></office:document-content>'''})
    text = documents.extract(path)
    assert text.startswith("a") and text.endswith("b") and len(text) <= 102
