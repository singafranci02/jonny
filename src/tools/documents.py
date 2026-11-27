"""Documents as text: spreadsheets, PDFs, Word files and slide decks.

Shared by the file readers (the Jarvis folder, his project folders and
repos) and the index (src/life/index.py), so a search hit's "part 2" is the
same part 2 the reader shows. Stdlib only, apart from pypdf for PDFs.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

DOC_EXT = (".xlsx", ".xlsm", ".pdf", ".docx", ".pptx")
PDF_PAGES = 40  # pages of a PDF that are read (and indexed)

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_M = "{" + _NS["m"] + "}"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _date_styles(z: zipfile.ZipFile) -> set[int]:
    """Indexes of cell styles that show a date (so 46326 reads 2026-10-31)."""
    if "xl/styles.xml" not in z.namelist():
        return set()
    root = ET.fromstring(z.read("xl/styles.xml"))
    custom = {}
    for nf in root.iter(f"{_M}numFmt"):
        code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", nf.get("formatCode", "").lower())
        custom[int(nf.get("numFmtId"))] = code
    out = set()
    xfs = root.find("m:cellXfs", _NS)
    for i, xf in enumerate(xfs if xfs is not None else []):
        fid = int(xf.get("numFmtId", 0))
        code = custom.get(fid, "")
        if 14 <= fid <= 22 or fid in (45, 46, 47) or ("d" in code or "y" in code):
            out.add(i)
    return out


def xlsx_text(src, max_rows: int = 80) -> str:
    """Every sheet as text rows ("a | b | c"), dates as dates."""
    z = zipfile.ZipFile(src)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{_M}t")))
    dates = _date_styles(z)
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = []
    for sh in ET.fromstring(z.read("xl/workbook.xml")).find("m:sheets", _NS):
        target = rels[sh.get(f"{{{_NS['r']}}}id")].lstrip("/")
        target = target if target.startswith("xl/") else f"xl/{target}"
        out.append(f"== sheet: {sh.get('name')}")
        rows = 0
        for row in ET.fromstring(z.read(target)).iter(f"{_M}row"):
            cells: dict[int, str] = {}
            for c in row.findall("m:c", _NS):
                v, inline = c.find("m:v", _NS), c.find("m:is", _NS)
                if c.get("t") == "s" and v is not None:
                    val = shared[int(v.text)]
                elif inline is not None:
                    val = "".join(t.text or "" for t in inline.iter(f"{_M}t"))
                elif v is not None and v.text is not None:
                    val = v.text
                    if c.get("t") in (None, "n") and int(c.get("s", -1)) in dates:
                        try:
                            val = (datetime(1899, 12, 30) + timedelta(days=float(val))).strftime("%Y-%m-%d")
                        except (ValueError, OverflowError):
                            pass
                    elif re.fullmatch(r"-?\d+\.\d{5,}", val):
                        val = f"{float(val):.4g}"
                else:
                    continue
                val = " ".join(val.split())
                if val and c.get("r"):
                    cells[_col(c.get("r"))] = val
            if not cells:
                continue
            first, last = min(cells), max(cells)
            out.append(" | ".join(cells.get(i, "") for i in range(first, last + 1)))
            rows += 1
            if rows >= max_rows:
                out.append("… (more rows)")
                break
    return "\n".join(out)


def pdf_text(src, max_pages: int = PDF_PAGES) -> str:
    import logging

    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # "wrong pointing object" noise
    reader = PdfReader(src)
    pages = [(p.extract_text() or "").strip() for p in reader.pages[:max_pages]]
    text = "\n\n".join(f"[page {i + 1}]\n{t}" for i, t in enumerate(pages) if t)
    more = f"\n… ({len(reader.pages)} pages in all)" if len(reader.pages) > max_pages else ""
    return (text or "(no text in this PDF: it is probably all images)") + more


def docx_text(src) -> str:
    root = ET.fromstring(zipfile.ZipFile(src).read("word/document.xml"))
    paras = ["".join(t.text or "" for t in p.iter(f"{_W}t")) for p in root.iter(f"{_W}p")]
    return "\n".join(p for p in paras if p.strip())


def pptx_text(src) -> str:
    """Each slide's text, in slide order, under a "[slide N]" line."""
    z = zipfile.ZipFile(src)
    slides = sorted(
        (n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)),
    )
    out = []
    for i, name in enumerate(slides, 1):
        root = ET.fromstring(z.read(name))
        paras = ["".join(t.text or "" for t in p.iter(f"{_A}t")) for p in root.iter(f"{_A}p")]
        paras = [" ".join(p.split()) for p in paras if p.strip()]
        if paras:
            out.append(f"[slide {i}]\n" + "\n".join(paras))
    return "\n\n".join(out) or "(no text on these slides)"


def document_text(name: str, data: bytes) -> str:
    """Text of one file's bytes: spreadsheets, PDFs, Word files and slide
    decks are converted; other binaries are refused."""
    ext = Path(name).suffix.lower()
    try:
        if ext in (".xlsx", ".xlsm"):
            return xlsx_text(io.BytesIO(data))
        if ext == ".pdf":
            return pdf_text(io.BytesIO(data))
        if ext == ".docx":
            return docx_text(io.BytesIO(data))
        if ext == ".pptx":
            return pptx_text(io.BytesIO(data))
    except Exception as e:
        return f"(couldn't read this {ext} file: {type(e).__name__})"
    if b"\0" in data[:8000]:
        return f"(binary file, {len(data) // 1024} KB: can't show it)"
    return data.decode("utf-8", errors="replace")


def is_text(text: str) -> bool:
    """False for document_text's own "(can't read this)" answers."""
    return not re.match(r"\((?:binary file|couldn't read this|no text (?:in this PDF|on these slides))", text)


def split_parts(text: str, size: int) -> list[str]:
    """A long text as numbered parts of at most `size` characters, each cut
    at a line break where possible."""
    chunks, rest = [], text
    while len(rest) > size:
        cut = rest.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    chunks.append(rest)
    return chunks


def part_text(text: str, part: int = 1, size: int = 20000) -> str:
    """One numbered part of a long text, with a line saying how to go on."""
    chunks = split_parts(text, size)
    if len(chunks) == 1:
        return text
    part = max(1, min(int(part or 1), len(chunks)))
    tail = f"\n… part {part} of {len(chunks)}" + (f"; read part {part + 1} for more" if part < len(chunks) else "")
    return chunks[part - 1] + tail
