import json
import os
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


ALIGNMENTS = {
    "left": 0,
    "center": 1,
    "right": 2,
    "justify": 4,
}


def _document_tree(content: str | None) -> dict[str, Any]:
    try:
        value = json.loads(content or "")
    except (TypeError, json.JSONDecodeError):
        value = None
    if isinstance(value, dict) and value.get("type") == "doc":
        if not isinstance(value.get("content"), list):
            value["content"] = []
        return value

    paragraphs = []
    for line in (content or "").splitlines() or [""]:
        paragraphs.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": line}] if line else [],
            }
        )
    return {"type": "doc", "content": paragraphs}


def _text_from_node(node: dict[str, Any]) -> str:
    node_type = node.get("type")
    if node_type == "text":
        return str(node.get("text", ""))
    if node_type == "hardBreak":
        return "\n"

    children = node.get("content", [])
    if not isinstance(children, list):
        return ""
    text = "".join(
        _text_from_node(child) for child in children if isinstance(child, dict)
    )
    if node_type in {
        "paragraph",
        "heading",
        "listItem",
        "blockquote",
        "codeBlock",
    }:
        return text + "\n"
    return text


def extract_plain_text(content: str | None) -> str:
    tree = _document_tree(content)
    return _text_from_node(tree).rstrip()


def _add_docx_inline(paragraph, node: dict[str, Any]) -> None:
    node_type = node.get("type")
    if node_type == "text":
        run = paragraph.add_run(str(node.get("text", "")))
        for mark in node.get("marks", []):
            if not isinstance(mark, dict):
                continue
            mark_type = mark.get("type")
            if mark_type == "bold":
                run.bold = True
            elif mark_type == "italic":
                run.italic = True
            elif mark_type == "underline":
                run.underline = True
        return
    if node_type == "hardBreak":
        paragraph.add_run().add_break()
        return

    children = node.get("content", [])
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                _add_docx_inline(paragraph, child)


def _docx_alignment(value: Any):
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    return {
        0: WD_ALIGN_PARAGRAPH.LEFT,
        1: WD_ALIGN_PARAGRAPH.CENTER,
        2: WD_ALIGN_PARAGRAPH.RIGHT,
        4: WD_ALIGN_PARAGRAPH.JUSTIFY,
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
        "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
    }.get(value, WD_ALIGN_PARAGRAPH.LEFT)


def _add_docx_blocks(
    document,
    nodes: list[Any],
    list_style: str | None = None,
    depth: int = 0,
) -> None:
    from docx.shared import Inches

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        children = node.get("content", [])
        if not isinstance(children, list):
            children = []

        if node_type in {"bulletList", "orderedList"}:
            style = "List Bullet" if node_type == "bulletList" else "List Number"
            for item in children:
                if not isinstance(item, dict):
                    continue
                item_children = item.get("content", [])
                if not isinstance(item_children, list):
                    continue
                first_block = True
                for child in item_children:
                    if not isinstance(child, dict):
                        continue
                    if child.get("type") in {"bulletList", "orderedList"}:
                        _add_docx_blocks(document, [child], depth=min(depth + 1, 2))
                    else:
                        _add_docx_blocks(
                            document,
                            [child],
                            list_style=style if first_block else None,
                            depth=depth,
                        )
                        first_block = False
            continue

        if node_type not in {"paragraph", "heading", "blockquote", "codeBlock"}:
            _add_docx_blocks(document, children, list_style, depth)
            continue

        if node_type == "heading":
            attrs = node.get("attrs")
            level = attrs.get("level", 1) if isinstance(attrs, dict) else 1
            style = f"Heading {level}" if level in {1, 2, 3} else "Normal"
        elif node_type == "blockquote":
            style = "Quote"
        elif node_type == "codeBlock":
            style = "No Spacing"
        else:
            style = list_style or "Normal"
        paragraph = document.add_paragraph(style=style)
        if list_style:
            paragraph.paragraph_format.left_indent = Inches(depth * 0.25)
        attrs = node.get("attrs", {})
        if isinstance(attrs, dict):
            paragraph.alignment = _docx_alignment(attrs.get("textAlign"))
        for child in children:
            if isinstance(child, dict):
                _add_docx_inline(paragraph, child)


def create_docx(content: str | None) -> bytes:
    from docx import Document

    document = Document()
    _add_docx_blocks(document, _document_tree(content).get("content", []))
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def _pdf_markup(node: dict[str, Any]) -> str:
    if node.get("type") == "text":
        text = escape(str(node.get("text", ""))).replace("\n", "<br/>")
        marks = {
            mark.get("type")
            for mark in (
                node.get("marks")
                if isinstance(node.get("marks"), list)
                else []
            )
            if isinstance(mark, dict)
        }
        for mark_type, tag in (
            ("bold", "b"),
            ("italic", "i"),
            ("underline", "u"),
        ):
            if mark_type in marks:
                text = f"<{tag}>{text}</{tag}>"
        return text
    if node.get("type") == "hardBreak":
        return "<br/>"
    children = node.get("content", [])
    if not isinstance(children, list):
        return ""
    return "".join(
        _pdf_markup(child) for child in children if isinstance(child, dict)
    )


def _pdf_alignment(value: Any) -> int:
    return ALIGNMENTS.get(value, 0)


def _pdf_font_paths() -> tuple[str, str, str, str] | None:
    candidates = [
        (
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
            ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
        ),
        (
            Path("/usr/share/fonts/truetype/dejavu"),
            (
                "DejaVuSans.ttf",
                "DejaVuSans-Bold.ttf",
                "DejaVuSans-Oblique.ttf",
                "DejaVuSans-BoldOblique.ttf",
            ),
        ),
    ]
    for directory, filenames in candidates:
        paths = tuple(str(directory / filename) for filename in filenames)
        if all(Path(path).is_file() for path in paths):
            return paths
    return None


def _pdf_flowables(nodes: list[Any], styles, depth: int = 0) -> list[Any]:
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, Spacer

    flowables = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        children = node.get("content", [])
        if not isinstance(children, list):
            children = []

        if node_type in {"bulletList", "orderedList"}:
            items = []
            for item in children:
                if not isinstance(item, dict):
                    continue
                item_children = item.get("content", [])
                blocks = _pdf_flowables(
                    item_children if isinstance(item_children, list) else [],
                    styles,
                    depth + 1,
                )
                items.append(ListItem(blocks or [Paragraph("", styles["Normal"])]))
            if items:
                flowables.append(
                    ListFlowable(
                        items,
                        bulletType="bullet" if node_type == "bulletList" else "1",
                        leftIndent=18 + depth * 14,
                        bulletFontName=styles["Normal"].fontName,
                    )
                )
            continue

        if node_type in {"doc", "listItem", "blockquote"}:
            flowables.extend(_pdf_flowables(children, styles, depth))
            continue
        if node_type not in {"paragraph", "heading", "codeBlock"}:
            continue
        attrs = node.get("attrs", {})
        alignment = _pdf_alignment(
            attrs.get("textAlign") if isinstance(attrs, dict) else None
        )
        if node_type == "heading":
            level = attrs.get("level", 1) if isinstance(attrs, dict) else 1
            base_style = styles.get(f"Heading{level}", styles["Heading1"])
        else:
            base_style = styles["Normal"]
        style = base_style.clone(f"Aligned{alignment}")
        style.alignment = alignment
        markup = _pdf_markup(node)
        flowables.append(Paragraph(markup or " ", style))
        if node_type == "codeBlock":
            flowables.append(Spacer(1, 2))
    return flowables


def create_pdf(content: str | None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate

    styles = getSampleStyleSheet()
    styles["Normal"].fontName = "Helvetica"
    styles["Normal"].fontSize = 11
    styles["Normal"].leading = 17
    styles["Normal"].alignment = TA_LEFT
    styles["Normal"].spaceAfter = 8
    for level, size in ((1, 18), (2, 15), (3, 13)):
        heading = styles[f"Heading{level}"]
        heading.fontName = "Helvetica-Bold"
        heading.fontSize = size
        heading.leading = size + 4
        heading.spaceBefore = 10
        heading.spaceAfter = 8

    font_paths = _pdf_font_paths()
    if font_paths:
        regular, bold, italic, bold_italic = font_paths
        pdfmetrics.registerFont(TTFont("InkwellSans", regular))
        pdfmetrics.registerFont(TTFont("InkwellSans-Bold", bold))
        pdfmetrics.registerFont(TTFont("InkwellSans-Italic", italic))
        pdfmetrics.registerFont(TTFont("InkwellSans-BoldItalic", bold_italic))
        pdfmetrics.registerFontFamily(
            "InkwellSans",
            normal="InkwellSans",
            bold="InkwellSans-Bold",
            italic="InkwellSans-Italic",
            boldItalic="InkwellSans-BoldItalic",
        )
        styles["Normal"].fontName = "InkwellSans"
        for level in (1, 2, 3):
            styles[f"Heading{level}"].fontName = "InkwellSans-Bold"
            styles[f"Heading{level}"].textColor = colors.HexColor("#302c27")

    output = BytesIO()
    pdf = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=64,
        rightMargin=64,
        topMargin=64,
        bottomMargin=64,
        title="Inkwell document",
    )
    pdf.build(_pdf_flowables(_document_tree(content).get("content", []), styles))
    return output.getvalue()
