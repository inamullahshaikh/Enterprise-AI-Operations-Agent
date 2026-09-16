"""Format-specific text extraction (docs/system-design.md section 11.1): every parser turns
raw bytes into a flat, format-agnostic `list[Block]` — headings, paragraphs, and tables in
document order — which `relay_core.rag.chunker` then splits into chunks without needing to know
whether the source was a PDF, DOCX, or Markdown file.

PDF scanned-page detection lives here (`ParsedDocument.needs_ocr`) but the actual Gemini
document-understanding fallback (docs/system-design.md section 11.1's "PDF scanned" branch)
doesn't: it needs the LLM gateway, which this module deliberately has no dependency on so it
stays a plain, fast, synchronous parser usable without a running event loop or API key. See
`relay_core.rag.ingest.ingest_document` for where the fallback is actually invoked.
"""

import io
import re
from dataclasses import dataclass
from typing import Literal

import pymupdf
from docx import Document as DocxDocument
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from markdown_it import MarkdownIt
from markdown_it.token import Token

BlockKind = Literal["heading", "paragraph", "table"]

# Below this many characters of extracted text per page, a PDF page is treated as scanned
# (image-only) rather than sparse-but-real text — chosen well under a typical single line of
# body text so a genuinely short page (e.g. a title page) doesn't false-positive.
_SCANNED_PAGE_CHAR_THRESHOLD = 20
# A PDF text line is a heading candidate when its font size is at least this many times the
# page's most common ("body") font size — a common, cheap heuristic; there's no ground truth
# to tune this against without a corpus of real PDFs, so it's deliberately conservative.
_HEADING_SIZE_RATIO = 1.2
_HEADING_MAX_CHARS = 120


@dataclass
class Block:
    kind: BlockKind
    text: str
    level: int | None = None  # heading level, 1-6; None for paragraph/table
    page: int | None = None  # 1-indexed; None for non-paginated sources (TXT/MD/DOCX)


@dataclass
class ParsedDocument:
    blocks: list[Block]
    page_count: int | None = None
    needs_ocr: bool = False


_UNSUPPORTED = "Unsupported mime type for ingestion: {mime_type!r}"


def parse_document(mime_type: str, raw: bytes) -> ParsedDocument:
    if mime_type == "application/pdf":
        return parse_pdf(raw)
    if mime_type == "text/markdown":
        return parse_markdown(raw)
    if mime_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ):
        return parse_docx(raw)
    if mime_type == "text/plain":
        return parse_text(raw)
    raise ValueError(_UNSUPPORTED.format(mime_type=mime_type))


def parse_text(raw: bytes) -> ParsedDocument:
    text = raw.decode("utf-8", errors="replace")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return ParsedDocument(blocks=[Block(kind="paragraph", text=p) for p in paragraphs])


def parse_markdown(raw: bytes) -> ParsedDocument:
    tokens = MarkdownIt().parse(raw.decode("utf-8", errors="replace"))
    blocks: list[Block] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.type == "heading_open":
            text = _inline_text(tokens, i)
            if text:
                blocks.append(Block(kind="heading", text=text, level=int(token.tag[1:])))
        elif token.type == "paragraph_open":
            text = _inline_text(tokens, i)
            if text:
                blocks.append(Block(kind="paragraph", text=text))
        elif token.type == "fence" or token.type == "code_block":
            if token.content.strip():
                blocks.append(Block(kind="paragraph", text=token.content.strip()))
        i += 1
    return ParsedDocument(blocks=blocks)


def _inline_text(tokens: list[Token], open_index: int) -> str:
    """The `inline` token holding an opening tag's text always immediately follows it in
    markdown-it-py's flat token stream (see the module docstring's example in
    `tests/unit/test_rag_parsers.py`)."""
    if open_index + 1 < len(tokens) and tokens[open_index + 1].type == "inline":
        return tokens[open_index + 1].content.strip()
    return ""


def parse_docx(raw: bytes) -> ParsedDocument:
    document = DocxDocument(io.BytesIO(raw))
    blocks: list[Block] = []
    for item in document.iter_inner_content():
        if isinstance(item, DocxParagraph):
            text = item.text.strip()
            if not text:
                continue
            style = (item.style.name or "") if item.style else ""
            match = re.match(r"Heading (\d)", style)
            if match:
                blocks.append(Block(kind="heading", text=text, level=min(int(match.group(1)), 6)))
            else:
                blocks.append(Block(kind="paragraph", text=text))
        elif isinstance(item, DocxTable):
            markdown_table = _docx_table_to_markdown(item)
            if markdown_table:
                blocks.append(Block(kind="table", text=markdown_table))
    return ParsedDocument(blocks=blocks)


def _docx_table_to_markdown(table: DocxTable) -> str:
    rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    lines = [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join("---" for _ in rows[0]) + " |",
    ]
    lines.extend("| " + " | ".join(r) + " |" for r in rows[1:])
    return "\n".join(lines)


def parse_pdf(raw: bytes) -> ParsedDocument:
    blocks: list[Block] = []
    total_chars = 0
    with pymupdf.open(stream=raw, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
        page_count = doc.page_count
        for page_index, page in enumerate(doc, start=1):
            page_blocks, page_chars = _pdf_page_blocks(page, page_index)
            blocks.extend(page_blocks)
            total_chars += page_chars
    needs_ocr = page_count > 0 and (total_chars / page_count) < _SCANNED_PAGE_CHAR_THRESHOLD
    return ParsedDocument(blocks=blocks, page_count=page_count, needs_ocr=needs_ocr)


def _pdf_page_blocks(page: "pymupdf.Page", page_number: int) -> tuple[list[Block], int]:
    page_dict = page.get_text("dict")  # type: ignore[no-untyped-call]
    text_blocks: list[list[tuple[str, float]]] = []
    for pdf_block in page_dict.get("blocks", []):
        lines: list[tuple[str, float]] = []
        for line in pdf_block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(s.get("text", "") for s in spans).strip()
            if not text:
                continue
            size = max((s.get("size", 0.0) for s in spans), default=0.0)
            lines.append((text, size))
        if lines:
            text_blocks.append(lines)

    all_lines = [line for lines in text_blocks for line in lines]
    if not all_lines:
        return [], 0
    body_size = _most_common_size([size for _, size in all_lines])

    blocks: list[Block] = []
    for lines in text_blocks:
        # A PyMuPDF text "block" is usually one paragraph's worth of wrapped lines — merge
        # its non-heading lines into one paragraph rather than emitting one Block per line,
        # so a paragraph that wraps across several lines isn't chunked as if each line were
        # its own paragraph. A heading line found within a block still gets split out.
        paragraph_lines: list[str] = []
        for text, size in lines:
            is_heading = (
                size >= body_size * _HEADING_SIZE_RATIO
                and len(text) <= _HEADING_MAX_CHARS
                and not text.endswith((".", ",", ";"))
            )
            if is_heading:
                if paragraph_lines:
                    blocks.append(
                        Block(kind="paragraph", text=" ".join(paragraph_lines), page=page_number)
                    )
                    paragraph_lines = []
                blocks.append(Block(kind="heading", text=text, level=1, page=page_number))
            else:
                paragraph_lines.append(text)
        if paragraph_lines:
            blocks.append(Block(kind="paragraph", text=" ".join(paragraph_lines), page=page_number))
    return blocks, sum(len(text) for text, _ in all_lines)


def _most_common_size(sizes: list[float]) -> float:
    if not sizes:
        return 0.0
    rounded = [round(s, 1) for s in sizes]
    return max(set(rounded), key=rounded.count)
