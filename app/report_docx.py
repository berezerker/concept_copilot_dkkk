"""Простой перевод markdown-ответа агента в .docx (заголовки, абзацы, списки, таблицы, **жирный**, `код`)."""
from __future__ import annotations

import io
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|_[^_]+_)")


def _runs(par, text):
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            r = par.add_run(part[2:-2])
            r.bold = True
        elif part.startswith("`") and part.endswith("`"):
            r = par.add_run(part[1:-1])
            r.font.name = "Consolas"
            r.font.size = Pt(9)
            r.font.color.rgb = RGBColor(0x0B, 0x57, 0xA4)
        elif part.startswith("_") and part.endswith("_") and len(part) > 2:
            r = par.add_run(part[1:-1])
            r.italic = True
        else:
            par.add_run(part)


def _shade(cell, hex_fill):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tcPr.append(shd)


def md_to_docx(md: str) -> bytes:
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Arial"
    st.font.size = Pt(10.5)
    lines = md.split("\n")
    i = 0
    row = lambda s: [c.strip() for c in s.strip().strip("|").split("|")]
    while i < len(lines):
        l = lines[i]
        if re.match(r"^\s*\|.*\|\s*$", l) and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            head = row(l)
            i += 2
            body = []
            while i < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]):
                body.append(row(lines[i]))
                i += 1
            t = doc.add_table(rows=1 + len(body), cols=len(head))
            t.style = "Table Grid"
            for j, h in enumerate(head):
                c = t.rows[0].cells[j]
                c.text = ""
                _runs(c.paragraphs[0], h.replace("**", ""))
                c.paragraphs[0].runs and setattr(c.paragraphs[0].runs[0], "bold", True)
                _shade(c, "E8EEF7")
            for r_i, r in enumerate(body, 1):
                for j in range(len(head)):
                    c = t.rows[r_i].cells[j]
                    c.text = ""
                    txt = r[j] if j < len(r) else ""
                    _runs(c.paragraphs[0], txt)
                    if re.match(r"^[−\-+]?[\d\s.,]+%?(\s*\(.*\))?(\s*[✓✗*]+)?$", txt):
                        c.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
            for rr in t.rows:
                for c in rr.cells:
                    for p in c.paragraphs:
                        for r_ in p.runs:
                            r_.font.size = Pt(9)
            doc.add_paragraph()
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", l)
        if m:
            doc.add_heading(m.group(2).replace("**", ""), level=min(len(m.group(1)), 3))
        elif re.match(r"^\s*[-*•]\s+", l):
            _runs(doc.add_paragraph(style="List Bullet"), re.sub(r"^\s*[-*•]\s+", "", l))
        elif re.match(r"^\s*\d+\.\s+", l):
            _runs(doc.add_paragraph(style="List Number"), re.sub(r"^\s*\d+\.\s+", "", l))
        elif l.strip():
            _runs(doc.add_paragraph(), l.strip())
        i += 1
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
