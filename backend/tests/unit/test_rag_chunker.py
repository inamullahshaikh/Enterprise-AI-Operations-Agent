"""Coverage for `relay_core.rag.chunker.chunk_blocks` (docs/system-design.md section 11.2):
heading-boundary splitting, the redundant-leading-title dedup, table atomicity, and the
target-token-with-overlap paragraph accumulation.
"""

from relay_core.rag.chunker import chunk_blocks
from relay_core.rag.parsers import Block


def test_a_single_paragraph_with_no_headings_becomes_one_chunk() -> None:
    blocks = [Block(kind="paragraph", text="Just one short paragraph.")]
    chunks = chunk_blocks("Doc", blocks)
    assert len(chunks) == 1
    assert chunks[0].context_header == "Doc"
    assert chunks[0].section_path is None
    assert chunks[0].content == "Just one short paragraph."


def test_headings_split_into_separate_chunks_with_a_section_path() -> None:
    blocks = [
        Block(kind="heading", text="Intro", level=1),
        Block(kind="paragraph", text="Intro text."),
        Block(kind="heading", text="Details", level=2),
        Block(kind="paragraph", text="Details text."),
    ]
    chunks = chunk_blocks("Doc", blocks)
    assert [c.context_header for c in chunks] == ["Doc > Intro", "Doc > Intro > Details"]
    assert chunks[1].content == "Details text."


def test_a_leading_heading_matching_the_title_is_not_duplicated_into_the_header() -> None:
    blocks = [
        Block(kind="heading", text="Renewal Playbook", level=1),
        Block(kind="paragraph", text="Check usage trends first."),
    ]
    chunks = chunk_blocks("Renewal Playbook", blocks)
    assert chunks[0].context_header == "Renewal Playbook"
    assert chunks[0].section_path is None


def test_a_non_leading_heading_matching_the_title_text_is_still_a_real_section() -> None:
    # Only the *first* heading gets the redundant-title treatment — a later heading that
    # happens to repeat the title text is a real section change, not noise.
    blocks = [
        Block(kind="heading", text="Overview", level=1),
        Block(kind="paragraph", text="Overview text."),
        Block(kind="heading", text="Doc", level=2),
        Block(kind="paragraph", text="Nested text."),
    ]
    chunks = chunk_blocks("Doc", blocks)
    assert chunks[1].context_header == "Doc > Overview > Doc"


def test_a_deeper_heading_returning_to_a_shallower_level_pops_the_stack() -> None:
    blocks = [
        Block(kind="heading", text="A", level=1),
        Block(kind="heading", text="B", level=2),
        Block(kind="paragraph", text="Under B."),
        Block(kind="heading", text="C", level=1),
        Block(kind="paragraph", text="Under C."),
    ]
    chunks = chunk_blocks("Doc", blocks)
    headers = [c.context_header for c in chunks]
    assert headers == ["Doc > A > B", "Doc > C"]


def test_a_table_becomes_its_own_chunk_never_merged_with_surrounding_paragraphs() -> None:
    blocks = [
        Block(kind="paragraph", text="Before the table."),
        Block(kind="table", text="| a | b |\n| --- | --- |\n| 1 | 2 |"),
        Block(kind="paragraph", text="After the table."),
    ]
    chunks = chunk_blocks("Doc", blocks)
    assert [c.content for c in chunks] == [
        "Before the table.",
        "| a | b |\n| --- | --- |\n| 1 | 2 |",
        "After the table.",
    ]


def test_paragraphs_accumulate_past_target_and_carry_a_word_boundary_overlap() -> None:
    blocks = [
        Block(kind="paragraph", text=f"Paragraph number {i} has some padding words in it.")
        for i in range(10)
    ]
    chunks = chunk_blocks("Doc", blocks, target_tokens=30, overlap_tokens=8)
    assert len(chunks) > 1
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        overlap_candidate = prev.content[-32:].split(" ", 1)[-1]
        assert nxt.content.startswith(overlap_candidate[:10])
    # Overlap never starts mid-word.
    for chunk in chunks[1:]:
        assert not chunk.content[:1].isspace()


def test_page_start_and_end_track_the_min_and_max_pdf_page_seen() -> None:
    blocks = [
        Block(kind="paragraph", text="Page one text.", page=1),
        Block(kind="paragraph", text="Still page one.", page=1),
        Block(kind="paragraph", text="Now page two.", page=2),
    ]
    chunks = chunk_blocks("Doc", blocks, target_tokens=10_000)
    assert chunks[0].page_start == 1
    assert chunks[0].page_end == 2


def test_content_hash_changes_when_content_or_header_changes() -> None:
    a = chunk_blocks("Doc", [Block(kind="paragraph", text="Same text.")])[0]
    b = chunk_blocks("Doc", [Block(kind="paragraph", text="Same text.")])[0]
    c = chunk_blocks("Different Doc", [Block(kind="paragraph", text="Same text.")])[0]
    assert a.content_hash == b.content_hash
    assert a.content_hash != c.content_hash


def test_ordinals_are_sequential_from_zero() -> None:
    blocks = [
        Block(kind="heading", text="A", level=1),
        Block(kind="paragraph", text="a"),
        Block(kind="heading", text="B", level=1),
        Block(kind="paragraph", text="b"),
    ]
    chunks = chunk_blocks("Doc", blocks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
