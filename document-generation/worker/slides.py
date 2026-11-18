"""Editable widescreen slides with measured line breaks and continuation slides."""

from functools import lru_cache
from unicodedata import east_asian_width

from PIL import ImageFont
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.util import Pt

NAVY, TEAL, INK, PAPER = "17324B", "167178", "233E51", "FAFBF8"
WIDTH, HEIGHT = 960, 540  # points, 16:9


@lru_cache(maxsize=12)
def font(size, bold):
    suffix = "-Bold" if bold else ""
    return ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/DejaVuSans{suffix}.ttf", size * 2)


def wrap(text, width, size, bold=False):
    metric = font(size, bold)

    def length(value):
        if value.isascii():
            return metric.getlength(value)
        # DejaVu has no CJK glyphs. Most viewer fallback glyphs occupy one full em.
        narrow = "".join(c for c in value if east_asian_width(c) not in ("W", "F"))
        return max(
            metric.getlength(value), metric.getlength(narrow) + (len(value) - len(narrow)) * size * 2
        )

    # Keep a few percent of slack for compatible font substitution in slide viewers.
    limit = width * 1.92
    lines, current = [], ""
    for word in text.split():
        candidate = (current + " " + word).lstrip()
        if length(candidate) <= limit:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = ""
        for char in word:
            if current and length(current + char) > limit:
                lines.append(current)
                current = ""
            current += char
    if current:
        lines.append(current)
    return lines


def box(slide, lines, x, y, width, size, leading, color, bold=False):
    height = leading * len(lines) + 5
    shape = slide.shapes.add_textbox(Pt(x), Pt(y), Pt(width), Pt(height))
    frame = shape.text_frame
    frame.margin_top = frame.margin_bottom = frame.margin_left = frame.margin_right = 0
    frame.word_wrap, frame.auto_size = False, MSO_AUTO_SIZE.NONE
    for i, line in enumerate(lines):
        p = frame.add_paragraph() if i else frame.paragraphs[0]
        p.space_after = p.space_before = Pt(0)
        p.line_spacing = Pt(leading)
        p.text = line
        p.font.name, p.font.bold, p.font.size = "DejaVu Sans", bold, Pt(size)
        p.font.color.rgb = RGBColor.from_string(color)
    return height


def rule(slide, y, color):
    mark = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Pt(56), Pt(y), Pt(58), Pt(4))
    mark.fill.solid()
    mark.fill.fore_color.rgb = RGBColor.from_string(color)
    mark.line.fill.background()


def deck(outline, destination):
    prs = Presentation()
    prs.slide_width, prs.slide_height = Pt(WIDTH), Pt(HEIGHT)
    prs.core_properties.title, prs.core_properties.author = outline.title, outline.author or ""

    def slide(background):
        page = prs.slides.add_slide(prs.slide_layouts[6])
        page.background.fill.solid()
        page.background.fill.fore_color.rgb = RGBColor.from_string(background)
        return page

    cover = slide(NAVY)
    rule(cover, 72, "66D4C3")
    for title_size in (36, 32, 28, 24):
        sub_size = title_size // 2 + 2
        title_lines = wrap(outline.title, 848, title_size, True)
        sub_lines = wrap(outline.subtitle or "", 848, sub_size)
        title_leading, sub_leading = title_size + 10, sub_size + 8
        if len(title_lines) * title_leading + len(sub_lines) * sub_leading < 309:
            break
    y = 110 + box(cover, title_lines, 56, 110, 848, title_size, title_leading, "FFFFFF", True)
    if sub_lines:
        box(cover, sub_lines, 56, y + 16, 848, sub_size, sub_leading, "C9E9E5")
    if outline.author:
        box(cover, wrap(outline.author, 848, 14), 56, 456, 848, 14, 21, "FFFFFF")

    for section in outline.sections:
        heading = wrap(section.heading, 848, 28, True)

        def section_page():
            page = slide(PAPER)
            rule(page, 36, TEAL)
            height = box(page, heading, 56, 60, 848, 28, 35, NAVY, True)
            box(page, [str(len(prs.slides))], 868, 501, 40, 11, 16, TEAL)
            return page, 80 + height

        page, y = section_page()
        entries = [(False, p) for p in section.paragraphs] + [(True, b) for b in section.bullets]
        for bullet, text in entries:
            left = 82 if bullet else 56
            lines = wrap(text, 904 - left, 19)
            first = True
            while lines:
                capacity = (480 - y) // 27
                if capacity < 1:
                    page, y = section_page()
                    capacity = (480 - y) // 27
                chunk, lines = lines[:capacity], lines[capacity:]
                if bullet and first:
                    box(page, ["•"], 56, y, 22, 19, 27, TEAL)
                box(page, chunk, left, y, 904 - left, 19, 27, INK)
                y += len(chunk) * 27 + 12
                first = False
    prs.save(destination)
