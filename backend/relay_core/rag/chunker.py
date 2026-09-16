"""Structure-aware chunking (docs/system-design.md section 11.2): split by heading first, then
by paragraph up to a target token budget, with a contextual header prepended to every chunk
before embedding. Token counts are an approximation (`len(text) // 4`, the common
characters-per-token rule of thumb for English) — Relay embeds with Gemini, not a tokenizer with
a Python binding declared as a project dependency, and chunk-size *uniformity* is what matters
here, not an exact count.
"""

import hashlib
from dataclasses import dataclass

from relay_core.rag.parsers import Block

_TARGET_TOKENS = 500
_OVERLAP_TOKENS = 60
_CHARS_PER_TOKEN = 4


@dataclass
class Chunk:
    ordinal: int
    content: str
    context_header: str
    section_path: str | None
    page_start: int | None
    page_end: int | None
    token_count: int
    content_hash: str


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _overlap_tail(text: str, tokens: int) -> str:
    """The last `tokens` worth of `text`, cut on a word boundary so the overlap carried into
    the next chunk doesn't start mid-word."""
    char_budget = tokens * _CHARS_PER_TOKEN
    if len(text) <= char_budget:
        return text
    tail = text[-char_budget:]
    space = tail.find(" ")
    return tail[space + 1 :] if space != -1 else tail


class _Accumulator:
    def __init__(self, seed_text: str = "") -> None:
        self.parts: list[str] = [seed_text] if seed_text else []
        self.pages: list[int] = []

    def add(self, text: str, page: int | None) -> None:
        self.parts.append(text)
        if page is not None:
            self.pages.append(page)

    @property
    def text(self) -> str:
        return "\n\n".join(self.parts)

    @property
    def tokens(self) -> int:
        return _approx_tokens(self.text)

    @property
    def is_empty(self) -> bool:
        return not self.parts


def chunk_blocks(
    title: str,
    blocks: list[Block],
    *,
    target_tokens: int = _TARGET_TOKENS,
    overlap_tokens: int = _OVERLAP_TOKENS,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []
    acc = _Accumulator()

    def section_path() -> str | None:
        return " > ".join(h[1] for h in heading_stack) or None

    def flush() -> None:
        if acc.is_empty:
            return
        content = acc.text
        path = section_path()
        header = " > ".join([title, path] if path else [title])
        combined = f"{header}\n\n{content}"
        chunks.append(
            Chunk(
                ordinal=len(chunks),
                content=content,
                context_header=header,
                section_path=path,
                page_start=min(acc.pages) if acc.pages else None,
                page_end=max(acc.pages) if acc.pages else None,
                token_count=_approx_tokens(content),
                content_hash=hashlib.sha256(combined.encode("utf-8")).hexdigest(),
            )
        )

    for block in blocks:
        if block.kind == "heading":
            flush()
            acc = _Accumulator()
            # A document's very first heading commonly restates its own title (e.g. a
            # Markdown file's leading `# Title` or a DOCX's title paragraph) — pushing it
            # onto the section path would duplicate the title half of the header
            # ("{title} > {title} > ..."), so it's treated as the title, not a section.
            is_redundant_title = (
                not heading_stack
                and not chunks
                and block.text.strip().casefold() == title.strip().casefold()
            )
            if not is_redundant_title:
                while heading_stack and heading_stack[-1][0] >= (block.level or 1):
                    heading_stack.pop()
                heading_stack.append((block.level or 1, block.text))
            continue

        if block.kind == "table":
            # Tables are kept whole (section 11.2): flush whatever came before it, emit the
            # table as its own chunk, then continue accumulating fresh afterwards — never
            # split a table across chunks, and never merge it into a paragraph chunk either,
            # so a wide table doesn't blow out an otherwise on-target chunk's size.
            flush()
            acc = _Accumulator()
            acc.add(block.text, block.page)
            flush()
            acc = _Accumulator()
            continue

        # paragraph
        if not acc.is_empty and acc.tokens + _approx_tokens(block.text) > target_tokens:
            overlap = _overlap_tail(acc.text, overlap_tokens)
            flush()
            acc = _Accumulator(overlap)
        acc.add(block.text, block.page)

    flush()
    return chunks
