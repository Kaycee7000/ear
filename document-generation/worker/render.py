"""Render trusted layouts with literal outline text. No remote templates or external assets."""

import os
import subprocess
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from pptx import Presentation
from pypdf import PdfReader
from slides import deck

from ear_document.contract import MAX_ARTIFACT_BYTES, Issue

INK = "172C42"
TEAL = "176A70"


def word(outline, destination):
    doc = Document()
    doc.core_properties.title = outline.title
    doc.core_properties.author = outline.author or ""
    doc.core_properties.subject = ""
    doc.core_properties.comments = ""
    page = doc.sections[0]
    page.page_width, page.page_height = Inches(8.5), Inches(11)
    page.left_margin = page.right_margin = Inches(0.95)
    page.top_margin = page.bottom_margin = Inches(0.8)
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = "DejaVu Sans", Pt(10.5)
    normal.font.color.rgb = RGBColor.from_string(INK)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.25
    for name, size, color in [("Title", 28, INK), ("Subtitle", 14, TEAL), ("Heading 1", 17, TEAL)]:
        style = doc.styles[name]
        # Remove the template's theme overrides so explicit typeface and color take effect.
        style.font.name, style.font.size, style.font.bold = "DejaVu Sans", Pt(size), name != "Subtitle"
        fonts = style.element.rPr.rFonts
        for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            fonts.attrib.pop(qn("w:" + attr), None)
        style.font.color.rgb = RGBColor.from_string(color)
        style.paragraph_format.keep_with_next = True
    doc.add_paragraph(outline.title, "Title")
    if outline.subtitle:
        doc.add_paragraph(outline.subtitle, "Subtitle")
    if outline.author:
        doc.add_paragraph(outline.author)
    for section in outline.sections:
        doc.add_heading(section.heading, 1)
        for paragraph in section.paragraphs:
            doc.add_paragraph(paragraph)
        for bullet in section.bullets:
            doc.add_paragraph(bullet, "List Bullet")
    footer = page.footer.paragraphs[0]
    footer.alignment = 2
    number = OxmlElement("w:fldSimple")
    number.set(qn("w:instr"), "PAGE")
    footer._p.append(number)
    doc.save(destination)


def render(task, folder):
    output = folder / f"output.{task.format}"
    if task.format == "pptx":
        deck(task.outline, output)
    else:
        source = folder / "output.docx"
        word(task.outline, source)
        if task.format == "pdf":
            env = {
                k: v
                for k, v in os.environ.items()
                if k in ("PATH", "LANG", "LC_ALL", "TZ", "LD_LIBRARY_PATH")
            }
            env.update(HOME=str(folder), TMPDIR=str(folder))
            subprocess.run(
                [
                    "soffice",
                    "--headless",
                    "-env:UserInstallation=" + (folder / "office-profile").as_uri(),
                    "--convert-to",
                    "pdf:writer_pdf_Export",
                    "--outdir",
                    str(folder),
                    str(source),
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=True,
            )
    return output


def validate(path: Path, task):
    """Reopen the generated file, including archive integrity and native content checks."""
    try:
        if not 0 < path.stat().st_size <= MAX_ARTIFACT_BYTES:
            raise ValueError
        if task.format == "pdf":
            reader = PdfReader(path, strict=True)
            if reader.is_encrypted or not reader.pages or not reader.pages[0].extract_text().strip():
                raise ValueError
            return len(reader.pages)
        with ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError
        if task.format == "docx":
            doc = Document(path)
            if doc.paragraphs[0].text != task.outline.title:
                raise ValueError
            return None  # The reader's page setup and installed fonts determine DOCX pagination.
        slides = Presentation(path).slides
        if len(slides) < len(task.outline.sections) + 1:
            raise ValueError
        return len(slides)
    except Exception:
        raise Issue("invalid_output") from None
