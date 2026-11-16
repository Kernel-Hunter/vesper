"""Text out of office documents: Word, Excel, PowerPoint, OpenDocument, RTF and EPUB.

Standard library only. The modern formats are zip files of XML, so each one is read part by
part with a size limit per part (a small zip can unpack to gigabytes). Old binary Word files
are converted by macOS's `textutil` where it exists.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

DOCUMENT_TYPES = {
    ".docx", ".docm", ".dotx", ".xlsx", ".xlsm", ".pptx", ".pptm", ".ppsx",
    ".odt", ".ods", ".odp", ".rtf", ".epub", ".doc", ".xls", ".ppt", ".pages", ".numbers", ".key",
}
MAX_PART_BYTES = 40 * 1024 * 1024    # one XML part, unpacked
MAX_TOTAL_BYTES = 120 * 1024 * 1024  # all parts read from one file, unpacked
MAX_SHEET_ROWS = 5000
MAX_SHEET_COLUMN = 16383  # Excel's last column (XFD); a larger reference is a damaged or hostile file

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"
TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"


class DocumentError(Exception):
    """The file could not be read; the message says why in words for the model and the user."""


def extract(path: Path) -> str:
    """The document's text. Raises DocumentError when it cannot be read."""
    suffix = path.suffix.lower()
    if suffix == ".rtf":
        with open(path, "rb") as f:
            return rtf_text(f.read(MAX_PART_BYTES).decode("latin-1"))
    if suffix == ".doc":
        return _textutil(path)
    if suffix in (".xls", ".ppt"):
        raise DocumentError(f"{path.name} is an old binary Office file. Ask the user to save it as "
                            f"{'.xlsx or .csv' if suffix == '.xls' else '.pptx or .pdf'} so it can be read.")
    if suffix in (".pages", ".numbers", ".key"):
        raise DocumentError(f"{path.name} is an Apple iWork file, which keeps its text in a private format. "
                            "Ask the user to export it (File > Export To) as PDF, Word, Excel or PowerPoint.")
    try:
        with zipfile.ZipFile(path) as z:
            reader = _Zip(z)
            if suffix in (".docx", ".docm", ".dotx"):
                return docx_text(reader)
            if suffix in (".xlsx", ".xlsm"):
                return xlsx_text(reader)
            if suffix in (".pptx", ".pptm", ".ppsx"):
                return pptx_text(reader)
            if suffix in (".odt", ".ods", ".odp"):
                return odf_text(reader)
            if suffix == ".epub":
                return epub_text(reader)
    except zipfile.BadZipFile:
        raise DocumentError(f"{path.name} is damaged or is not really a {suffix} file.") from None
    except ET.ParseError as exc:
        raise DocumentError(f"{path.name} has a part that is not valid XML ({exc}).") from None
    raise DocumentError(f"Vesper does not read {suffix} files.")


class _Zip:
    """Reads parts of a zip with a limit per part and in total."""

    def __init__(self, z: zipfile.ZipFile):
        self.z = z
        self.names = set(z.namelist())
        self.read_bytes = 0

    def raw(self, name: str) -> bytes | None:
        if name not in self.names:
            return None
        size = self.z.getinfo(name).file_size
        if size > MAX_PART_BYTES or self.read_bytes + size > MAX_TOTAL_BYTES:
            raise DocumentError(f"Part {name} of this document is too large to read ({size // 1_000_000} MB unpacked).")
        self.read_bytes += size
        return self.z.read(name)

    def xml(self, name: str) -> ET.Element | None:
        data = self.raw(name)
        return None if data is None else ET.fromstring(data)


def _normal(path: PurePosixPath) -> PurePosixPath:
    parts: list[str] = []
    for piece in path.parts:
        if piece == "..":
            if parts:
                parts.pop()
        elif piece != ".":
            parts.append(piece)
    return PurePosixPath(*parts)


def _rels(z: _Zip, part: str) -> dict[str, str]:
    """Relationship id -> target path, resolved against the part's folder."""
    folder = PurePosixPath(part).parent
    root = z.xml(str(folder / "_rels" / (PurePosixPath(part).name + ".rels")))
    out = {}
    for rel in root.iter(f"{PR}Relationship") if root is not None else []:
        target = rel.get("Target", "")
        out[rel.get("Id", "")] = target.lstrip("/") if target.startswith("/") else str(_normal(folder / target))
    return out


# ----------------------------------------------------------------------------- Word
def _w_paragraph(p: ET.Element) -> str:
    out = []
    for el in p.iter():
        if el.tag == f"{W}t":
            out.append(el.text or "")
        elif el.tag == f"{W}tab":
            out.append("\t")
        elif el.tag in (f"{W}br", f"{W}cr"):
            out.append("\n")
    text = "".join(out)
    style = p.find(f"{W}pPr/{W}pStyle")
    level = re.match(r"(?i)heading\s*(\d)", (style.get(f"{W}val") or "") if style is not None else "")
    if level and text.strip():
        return "#" * min(int(level.group(1)), 6) + " " + text
    if p.find(f"{W}pPr/{W}numPr") is not None and text.strip():
        return "- " + text
    return text


def _w_block(el: ET.Element, out: list[str]) -> None:
    for child in el:
        if child.tag == f"{W}p":
            out.append(_w_paragraph(child))
        elif child.tag == f"{W}tbl":
            for row in child.iter(f"{W}tr"):
                cells = [" ".join(_w_paragraph(p) for p in tc.iter(f"{W}p")).strip() for tc in row.findall(f"{W}tc")]
                out.append("| " + " | ".join(cells) + " |")
            out.append("")
        elif child.tag in (f"{W}sdt", f"{W}sdtContent", f"{W}customXml"):
            _w_block(child, out)


def docx_text(z: _Zip) -> str:
    root = z.xml("word/document.xml")
    if root is None:
        raise DocumentError("This Word file has no document body.")
    body = root.find(f"{W}body")
    out: list[str] = []
    _w_block(body if body is not None else root, out)
    return _tidy("\n".join(out))


# ----------------------------------------------------------------------------- PowerPoint
def pptx_text(z: _Zip) -> str:
    slides = sorted((n for n in z.names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                    key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
    order = []
    pres = z.xml("ppt/presentation.xml")
    if pres is not None:  # the order shown in PowerPoint, which can differ from the file numbers
        rels = _rels(z, "ppt/presentation.xml")
        P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
        order = [rels.get(s.get(f"{R}id", ""), "") for s in pres.iter(f"{P}sldId")]
    slides = [s for s in order if s in z.names] or slides
    out = []
    for n, name in enumerate(slides, start=1):
        root = z.xml(name)
        paras = ["".join(t.text or "" for t in p.iter(f"{A}t")) for p in root.iter(f"{A}p")] if root is not None else []
        out.append(f"## Slide {n}\n" + "\n".join(t for t in paras if t.strip()))
        notes = next((t for t in _rels(z, name).values() if "notesSlide" in t), None)
        note_root = z.xml(notes) if notes else None
        if note_root is not None:
            lines = ["".join(t.text or "" for t in p.iter(f"{A}t")) for p in note_root.iter(f"{A}p")]
            lines = [t for t in lines if t.strip() and not t.strip().isdigit()]
            if lines:
                out.append("Notes: " + " ".join(lines))
    return _tidy("\n\n".join(out))


# ----------------------------------------------------------------------------- Excel
def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]*", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return max(n - 1, 0)


def xlsx_text(z: _Zip) -> str:
    shared = []
    sst = z.xml("xl/sharedStrings.xml")
    if sst is not None:
        shared = ["".join(t.text or "" for t in si.iter(f"{S}t")) for si in sst.findall(f"{S}si")]
    book = z.xml("xl/workbook.xml")
    if book is None:
        raise DocumentError("This Excel file has no workbook.")
    rels = _rels(z, "xl/workbook.xml")
    out = []
    for sheet in book.iter(f"{S}sheet"):
        target = rels.get(sheet.get(f"{R}id", ""), "")
        root = z.xml(target) if target else None
        if root is None:
            continue
        rows, cut = [], False
        for row in root.iter(f"{S}row"):
            if len(rows) >= MAX_SHEET_ROWS:
                cut = True
                break
            cells: dict[int, str] = {}
            for c in row.findall(f"{S}c"):
                kind, v = c.get("t"), c.find(f"{S}v")
                if kind == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                    value = shared[int(v.text)]
                elif kind == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(f"{S}t"))
                elif kind == "b" and v is not None:
                    value = "TRUE" if v.text == "1" else "FALSE"
                else:
                    value = (v.text or "") if v is not None else ""
                col = _col(c.get("r", ""))
                if value != "" and col <= MAX_SHEET_COLUMN:
                    cells[col] = value.replace("\n", " ")
            if cells:
                width = max(cells) + 1
                rows.append(f"{row.get('r', '')}\t" + " | ".join(cells.get(i, "") for i in range(width)))
        tail = f"\n(only the first {MAX_SHEET_ROWS} rows are shown)" if cut else ""
        out.append(f"## Sheet: {sheet.get('name', '')}\n" + ("\n".join(rows) if rows else "(empty)") + tail)
    return "\n\n".join(out)


# ----------------------------------------------------------------------------- OpenDocument
def _odf_text(el: ET.Element) -> str:
    out = [el.text or ""]
    for child in el:
        if child.tag == f"{TEXT}s":
            out.append(" " * min(_int(child.get(f"{TEXT}c", "1")), 100))
        elif child.tag == f"{TEXT}tab":
            out.append("\t")
        elif child.tag == f"{TEXT}line-break":
            out.append("\n")
        else:
            out.append(_odf_text(child))
        out.append(child.tail or "")
    return "".join(out)


def _odf_block(el: ET.Element, out: list[str]) -> None:
    for child in el:
        if child.tag == f"{TEXT}h":
            out.append("#" * max(1, min(_int(child.get(f"{TEXT}outline-level", "1")), 6)) + " " + _odf_text(child))
        elif child.tag == f"{TEXT}p":
            out.append(_odf_text(child))
        elif child.tag == f"{TABLE}table":
            out.append(f"## {child.get(f'{TABLE}name', 'Table')}")
            for row in child.iter(f"{TABLE}table-row"):
                cells = []
                for cell in row:
                    if cell.tag not in (f"{TABLE}table-cell", f"{TABLE}covered-table-cell"):
                        continue
                    text = " ".join(_odf_text(p) for p in cell.iter(f"{TEXT}p")).strip()
                    repeat = min(_int(cell.get(f"{TABLE}number-columns-repeated", "1")), 50)
                    cells.extend([text] * (repeat if text else 1))
                while cells and not cells[-1]:
                    cells.pop()
                if cells:
                    out.append("| " + " | ".join(cells) + " |")
            out.append("")
        else:
            _odf_block(child, out)


def odf_text(z: _Zip) -> str:
    root = z.xml("content.xml")
    if root is None:
        raise DocumentError("This OpenDocument file has no content.")
    body = root.find(f"{OFFICE}body")
    out: list[str] = []
    _odf_block(body if body is not None else root, out)
    return _tidy("\n".join(out))


# ----------------------------------------------------------------------------- EPUB
def _drop_elements(markup: str, tag: str) -> str:
    """Remove <tag ...>...</tag> in one pass (a regex would be slow on many unclosed tags)."""
    low, out, at = markup.lower(), [], 0
    while True:
        start = low.find(f"<{tag}", at)
        if start < 0:
            out.append(markup[at:])
            return "".join(out)
        out.append(markup[at:start])
        end = low.find(f"</{tag}", start)
        if end < 0:
            return "".join(out)
        close = low.find(">", end)
        at = len(markup) if close < 0 else close + 1


def _int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _html_text(markup: str) -> str:
    for tag in ("script", "style", "head"):
        markup = _drop_elements(markup, tag)
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|h[1-6]|li|tr)>", "\n", markup)
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def epub_text(z: _Zip) -> str:
    container = z.xml("META-INF/container.xml")
    opf = None
    if container is not None:
        rootfile = next((e for e in container.iter() if e.tag.endswith("rootfile")), None)
        opf = rootfile.get("full-path") if rootfile is not None else None
    root = z.xml(opf) if opf else None
    if root is None:
        raise DocumentError("This EPUB has no package file (OPF).")
    folder = PurePosixPath(opf).parent
    items = {e.get("id"): e.get("href", "") for e in root.iter() if e.tag.endswith("}item")}
    out = []
    for ref in (e.get("idref") for e in root.iter() if e.tag.endswith("}itemref")):
        href = items.get(ref)
        data = z.raw(str(_normal(folder / href))) if href else None
        if data:
            out.append(_html_text(data.decode("utf-8", errors="replace")))
    return _tidy("\n\n".join(out))


# ----------------------------------------------------------------------------- RTF
_RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer", "listtable",
             "listoverridetable", "rsidtbl", "generator", "themedata", "colorschememapping", "latentstyles",
             "datastore", "xmlnstbl", "mmathPr", "filetbl", "revtbl"}


def rtf_text(rtf: str) -> str:
    """Plain text of an RTF document (paragraphs, tabs, \\'hh and \\uN characters)."""
    out, stack, skip, uc, pending = [], [], False, 1, 0
    for m in re.finditer(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}]+)", rtf):
        word, arg, hexcode, symbol, brace, text = m.groups()
        if brace == "{":
            stack.append((skip, uc))
        elif brace == "}":
            skip, uc = stack.pop() if stack else (False, 1)
        elif symbol:
            if symbol == "*":
                skip = True
            elif not skip and symbol in "{}\\":
                out.append(symbol)
            elif not skip and symbol == "~":
                out.append(" ")
            elif not skip and symbol in "\r\n":  # a backslash before a line break is a paragraph mark
                out.append("\n")
        elif word:
            if word in _RTF_SKIP:
                skip = True
            elif skip:
                continue
            elif word in ("par", "line", "sect", "page", "row"):
                out.append("\n")
            elif word in ("tab", "cell"):
                out.append("\t")
            elif word == "uc":
                uc = int(arg or 1)
            elif word == "u":
                code = int(arg or 0)
                out.append(chr(code + 65536 if code < 0 else code))
                pending = uc  # the next characters are a fallback for readers without Unicode
        elif hexcode:
            if pending:
                pending -= 1
            elif not skip:
                out.append(bytes([int(hexcode, 16)]).decode("cp1252", errors="replace"))
        elif text and not skip:
            text = text.replace("\r", "").replace("\n", "")
            if pending:
                drop = min(pending, len(text))
                text, pending = text[drop:], pending - drop
            out.append(text)
    return _tidy("".join(out))


# ----------------------------------------------------------------------------- old Word files
def _textutil(path: Path) -> str:
    if sys.platform != "darwin" or not shutil.which("textutil"):
        raise DocumentError(f"{path.name} is an old binary Word file. Ask the user to save it as .docx or PDF.")
    try:
        done = subprocess.run(["textutil", "-convert", "txt", "-stdout", "--", str(path)],
                              capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise DocumentError(f"Converting {path.name} took too long.") from None
    if done.returncode != 0:
        raise DocumentError(f"{path.name} could not be converted: {done.stderr.decode(errors='replace')[:200]}")
    return _tidy(done.stdout[:MAX_PART_BYTES].decode("utf-8", errors="replace"))


def _tidy(text: str) -> str:
    # Join UTF-16 halves (RTF writes an emoji as two \u values); a lone half becomes U+FFFD.
    text = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
