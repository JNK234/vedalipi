# ABOUTME: Tests for normalize (canonical mark order, kampa, sa-only) and validate (text checks, image counts).
# ABOUTME: Synthetic line images cover match/mismatch/bbox/table paths; no real PDFs or models needed.

from pathlib import Path

import cv2
import numpy as np

from vedalipi.normalize import normalize_page, normalize_text
from vedalipi.schema import Block, Page
from vedalipi.validate import validate_page

ABOVE = "॑"  # U+0951
BELOW = "॒"  # U+0952


def test_reorders_marks_within_akshara():
    assert normalize_text("क" + ABOVE + "िं") == "किं" + ABOVE


def test_nfc_applied():
    assert normalize_text("e" + "́") == "é"


def test_kampa_on_digits_kept():
    assert normalize_text("१" + BELOW + ABOVE) == "१" + BELOW + ABOVE


def test_non_devanagari_untouched():
    assert normalize_text("Contents 123 न: ...") == "Contents 123 न: ..."


def test_normalize_page_only_sa_and_returns_copy(tmp_path):
    del tmp_path  # page is built in memory
    page = Page(
        page=1,
        blocks=[
            Block(id="v", type="verse", lines=["क" + ABOVE + "िं"]),
            Block(id="t", type="table", rows=[["क" + ABOVE + "िं"]]),
            Block(id="e", type="foreign", lang="en", lines=["Contents"]),
        ],
    )
    out = normalize_page(page)
    assert out.block("v").lines == ["किं" + ABOVE]
    assert out.block("t").rows == [["किं" + ABOVE]]
    assert out.block("e").lines == ["Contents"]
    assert page.block("v").lines == ["क" + ABOVE + "िं"]  # original untouched


def test_text_only_clean_page_has_no_flags():
    page = Page(page=1, blocks=[Block(id="v", type="verse", lines=["किं" + ABOVE, "शान्तिः"])])
    report = validate_page(page, None)
    assert report.block_flags == {}
    assert report.page == 1


def test_text_only_orphan_mark_flagged(tmp_path):
    page = Page(page=1, blocks=[Block(id="v", type="verse", lines=["enin " + ABOVE + "x", " ॒शब्द"])])
    report = validate_page(page, tmp_path / "missing.png")  # missing image -> text checks only
    assert report.block_flags == {"v": ["svara_count_mismatch"]}
    assert len(report.notes) == 2


def test_mark_after_matra_is_not_orphan():
    page = Page(page=1, blocks=[Block(id="v", type="verse", lines=["शान्ति" + ABOVE + "ः"])])
    assert validate_page(page, None).block_flags == {}


def make_line_image(path: Path, width: int = 1200, marks: str = "both") -> None:
    """One fake Devanagari line: headline, three glyph blocks, an optional stroke above and bar below."""
    img = np.full((300, width), 255, np.uint8)
    for x in (100, 500, 900):
        cv2.rectangle(img, (x - 40, 100), (x + 240, 108), 0, -1)  # word headline (shirorekha)
        cv2.rectangle(img, (x, 108), (x + 120, 190), 0, -1)  # glyph bodies
    if marks in ("both", "above"):
        cv2.rectangle(img, (540, 40), (548, 95), 0, -1)  # svarita stroke
    if marks in ("both", "below"):
        cv2.rectangle(img, (920, 220), (980, 228), 0, -1)  # anudatta bar
    cv2.imwrite(str(path), img)


def test_image_match_no_flag(tmp_path):
    img = tmp_path / "line.png"
    make_line_image(img)
    page = Page(page=1, blocks=[Block(id="v", type="verse", lines=["क" + ABOVE + "ख" + BELOW])])
    assert validate_page(page, img).block_flags == {}


def test_image_extra_mark_in_text_flagged(tmp_path):
    img = tmp_path / "line.png"
    make_line_image(img)
    page = Page(page=1, blocks=[Block(id="v", type="verse", lines=["क" + ABOVE + "ख" + BELOW + "ग" + ABOVE])])
    report = validate_page(page, img)
    assert report.block_flags == {"v": ["svara_count_mismatch"]}
    assert report.notes == ["v line 1: text above/below 2/1 != image 1/1"]


def test_image_mark_missing_from_text_flagged(tmp_path):
    img = tmp_path / "line.png"
    make_line_image(img)
    page = Page(page=1, blocks=[Block(id="v", type="verse", lines=["कख"])])
    report = validate_page(page, img)
    assert report.block_flags == {"v": ["svara_count_mismatch"]}


def make_two_line_image(path: Path) -> None:
    img = np.full((600, 1200), 255, np.uint8)
    for x in (100, 500, 900):
        cv2.rectangle(img, (x - 40, 100), (x + 240, 108), 0, -1)
        cv2.rectangle(img, (x, 108), (x + 120, 190), 0, -1)
    cv2.rectangle(img, (540, 40), (548, 95), 0, -1)  # stroke on line 1 only
    for x in (100, 500, 900):
        cv2.rectangle(img, (x - 40, 350), (x + 240, 358), 0, -1)
        cv2.rectangle(img, (x, 358), (x + 120, 440), 0, -1)
    cv2.rectangle(img, (920, 470), (980, 478), 0, -1)  # bar on line 2 only
    cv2.imwrite(str(path), img)


def test_two_lines_without_bbox_checked_in_order(tmp_path):
    img = tmp_path / "two.png"
    make_two_line_image(img)
    page = Page(page=1, blocks=[Block(id="v", type="verse", lines=["क" + ABOVE, "ख" + BELOW])])
    assert validate_page(page, img).block_flags == {}
    swapped = Page(page=1, blocks=[Block(id="v", type="verse", lines=["ख" + BELOW, "क" + ABOVE])])
    assert swapped.block("v").lines[0] != page.block("v").lines[0]
    assert validate_page(swapped, img).block_flags == {"v": ["svara_count_mismatch"]}


def test_bbox_selects_region_and_table_rows_flattened(tmp_path):
    img = tmp_path / "two.png"
    make_two_line_image(img)
    page = Page(
        page=1,
        blocks=[Block(id="t", type="table", bbox=[0.0, 0.55, 1.0, 1.0], rows=[["ख" + BELOW]])],
    )
    assert validate_page(page, img).block_flags == {}


def test_invalid_sign_order_is_flagged_not_fixed():
    from vedalipi.validate import invalid_sequences

    assert invalid_sequences("सिुद्ध्यर्थं") == ["सिुद"]
    assert invalid_sequences("क़ि भ॒द्रं कर्णे॑भिः शृणु॒याम॑ देवाः ओं") == []
    page = Page(page=1, blocks=[Block(id="b1", type="prose", lines=["कार्य सिुद्ध्यर्थं"])])
    report = validate_page(page, None)
    assert report.block_flags["b1"] == ["invalid_sequence"]
    assert page.blocks[0].lines == ["कार्य सिुद्ध्यर्थं"]


def test_table_cells_with_several_lines_expand_to_printed_lines():
    from vedalipi.validate import table_lines

    assert table_lines([["अ\nआ", "इ"], ["उ", "ऊ"]]) == ["अ इ", "आ", "उ ऊ"]


def test_second_read_disagreement_is_flagged_per_line():
    first = Page(page=1, blocks=[Block(id="b1", type="verse", lines=["प॒वि॑त्रं प्र॒जाप॑तेः", "यत् स॑ह॒जं"])])
    second = Page(page=1, blocks=[Block(id="x", type="verse", lines=["पवित्रं॑ प्र॒जाप॑तेः", "यत् स॑ह॒जं"])])
    report = validate_page(first, None, second)
    assert report.block_flags == {"b1": ["reader_disagreement"]}
    assert any("line 1" in n and "प॒वि॑त्रं / पवित्रं॑" in n for n in report.notes)
    assert validate_page(first, None, first).block_flags == {}
