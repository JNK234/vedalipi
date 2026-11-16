# ABOUTME: Tests for normalize's gum rule (ग्ं and ग्ग्ं become one U+A8F3, svaras after it stay on it) and joiner removal.
# ABOUTME: Counts come from the real Kannada OCR fixtures and expected.json; small Telugu/Devanagari cases cover the rest.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vedalipi import transliterate
from vedalipi.normalize import NORMALIZE_VERSION, normalize_page, normalize_text
from vedalipi.schema import Block, Label, Page
from vedalipi.validate import validate_page

FIXTURES = Path(__file__).parent / "fixtures" / "kannada_ocr"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
A8F3 = "\ua8f3"
ZWJ, ZWNJ = "\u200d", "\u200c"


def converted(page: Page, script: str) -> Page:
    """What the normalize stage writes for a book printed in `script`."""
    return normalize_page(transliterate.page_to_devanagari(page, script))


def sa_text(page: Page) -> str:
    return "\n".join(line for b in page.blocks if b.is_sanskrit for line in b.lines)


# ---------------------------------------------------------------- the real Kannada pages


@pytest.mark.parametrize("name", sorted(EXPECTED["files"]))
def test_a8f3_count_matches_expected(name):
    text = sa_text(converted(Page.load(FIXTURES / name), "kannada"))
    assert text.count(A8F3) == EXPECTED["files"][name]["expected_A8F3_after_normalize"]
    assert "ग्ं" not in text  # no single or doubled gum form is left behind


def test_p0064_doubled_form_becomes_one_a8f3_and_svaras_stay_where_read():
    lines = converted(Page.load(FIXTURES / "p0064.read1.json"), "kannada").block("b3").lines
    # Read 1 put the svarita on ಷ; the rule only collapses the form, it never moves a svara.
    assert lines[0].startswith("परि॒ष॑द्यꣳ स्तु॒वंतः। अहि॑ꣳ ")
    # A svara printed after the form stays on ꣳ.
    assert "अश्वा॒ꣳ॒ अन्वे॑तु" in lines[4] and "अन्न॒ꣳ॒ रक्षं॑तौ" in lines[4]


def test_svara_after_the_doubled_form_ends_on_a8f3_as_in_the_proofread_edition():
    page = Page(page=1, blocks=[Block(id="v", type="verse", lines=["ಪರಿ॒ಷದ್ಯಗ್ಗ್ಂ॑ ಸ್ತು॒ವಂತಃ।"])])
    assert converted(page, "kannada").block("v").lines == ["परि॒षद्यꣳ॑ स्तु॒वंतः।"]


def test_ga_with_anusvara_is_not_gum():
    lines = converted(Page.load(FIXTURES / "p0005.read1.json"), "kannada").block("b3").lines
    assert any("गं॑तु" in line for line in lines)  # printed ಗಂ॑ತು: ga, anusvara, svarita
    assert normalize_text("गं गं॒तु") == "गं गं॒तु"


# ---------------------------------------------------------------- one rule for every script


@pytest.mark.parametrize("script, line, want", [
    ("telugu", "అహి॑గ్ం", "अहि॑ꣳ"),
    ("telugu", "ద్యగ్గ్ం॑", "द्यꣳ॑"),
    ("telugu", "అశ్వా॒గ్ం॒", "अश्वा॒ꣳ॒"),
    ("kannada", "ಅಹಿ॑ಗ್ಂ", "अहि॑ꣳ"),
    ("devanagari", "अहि॑ग्ग्ं॒", "अहि॑ꣳ॒"),
])
def test_gum_rule_for_every_source_script(script, line, want):
    page = Page(page=1, blocks=[Block(id="v", type="verse", lines=[line])])
    assert converted(page, script).block("v").lines == [want]


def test_svara_read_inside_the_form_still_ends_on_a8f3():
    # Canonical mark order puts a svara read between ್ and ಂ after the anusvara, so it stays on ꣳ.
    assert normalize_text("अहिग्॒ं") == "अहिꣳ॒"


def test_gum_rule_is_for_sanskrit_only():
    page = Page(page=1, blocks=[
        Block(id="kn", type="prose", lang="kn", lines=["ಗ್ಂ"]),
        Block(id="sa", type="table", rows=[["अग्ं", "१"]]),
        Block(id="i", type="image", bbox=[0, 0, 1, 1], labels=[
            Label(text="अग्ं", lang="sa", bbox=[0, 0, 1, 1]), Label(text="ಗ್ಂ", lang="kn", bbox=[0, 0, 1, 1])]),
    ])
    out = normalize_page(page)
    assert out.block("kn").lines == ["ಗ್ಂ"]
    assert out.block("sa").rows == [["अꣳ", "१"]]
    assert [lab.text for lab in out.block("i").labels] == ["अꣳ", "ಗ್ಂ"]


# ---------------------------------------------------------------- ZWJ and ZWNJ


def test_zwj_in_shri_gives_no_invalid_sequence():
    page = normalize_page(Page(page=1, blocks=[Block(id="v", type="verse", lines=["श" + ZWJ + "ृ"])]))
    assert page.block("v").lines == ["शृ"]
    assert "invalid_sequence" not in validate_page(page, None).block_flags.get("v", [])


def test_joiners_are_removed_from_sanskrit_only():
    assert normalize_text("क्" + ZWNJ + "ष स" + ZWJ + "्" + ZWJ + "य") == "क्ष स्य"
    page = normalize_page(Page(page=1, blocks=[Block(id="ml", type="prose", lang="ml", lines=["ന്" + ZWJ])]))
    assert page.block("ml").lines == ["ന്" + ZWJ]  # other languages keep their joiners


def test_a_normalize_version_change_renormalizes_cached_pages(tmp_path, monkeypatch):
    # Cached pages are normalized again when the normalize version changes, and only then.
    from vedalipi import normalize, pipeline
    from vedalipi.pipeline import RunConfig

    src = tmp_path / "a.txt"
    src.write_text("ಅಹಿ॑ಗ್ಂ\n", encoding="utf-8")
    cfg = RunConfig(input_path=src, out_dir=tmp_path / "out", source_script="kannada", jobs=1)
    pipeline.run(cfg)
    again, _ = pipeline.run(cfg)
    assert (1, "normalize") in again.skipped
    monkeypatch.setattr(normalize, "NORMALIZE_VERSION", NORMALIZE_VERSION + "-next")
    bumped, _ = pipeline.run(cfg)
    assert (1, "normalize") in bumped.ran


# ---------------------------------------------------------------- marks from outside Devanagari


def test_a_foreign_mark_keeps_its_place_after_the_anusvara():
    # p0064 read 1 prints ಶುಭಂ̱: anusvara, then U+0331. Moving U+0331 before the anusvara would leave the
    # anusvara with no base in the target font (a dotted circle in the PDF).
    assert normalize_text("शुभं̱ पा̱प्मानं̱") == "शुभं̱ पा̱प्मानं̱"
    lines = normalize_page(transliterate.page_to_devanagari(Page.load(FIXTURES / "p0064.read1.json"), "kannada"))
    assert "शुभं̱" in lines.block("b3").lines[6]


def test_a_foreign_mark_keeps_its_order_among_svaras():
    # Same rank as svaras, so the order the reader gave is kept either way round. (U+0952 and U+0331 share
    # combining class 220, so NFC leaves their order alone and only the mark ranking decides it.)
    for marks in ("\u0331\u0952", "\u0952\u0331"):
        assert normalize_text("क" + marks) == "क" + marks
