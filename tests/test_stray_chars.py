# ABOUTME: Tests that Sanskrit converted from a source script never keeps a source letter or a non-Vedic mark silently.
# ABOUTME: Uses the real Kannada OCR fixtures (p0064 read1 has 26 U+0331) plus small Telugu/Kannada/Devanagari cases.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vedalipi import pipeline, transliterate
from vedalipi.cli import main
from vedalipi.normalize import normalize_page
from vedalipi.pipeline import RunConfig
from vedalipi.schema import Block, Label, Master, Page
from vedalipi.validate import validate_page

FIXTURES = Path(__file__).parent / "fixtures" / "kannada_ocr"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
MACRON_BELOW = "\u0331"


def converted(page: Page, script: str) -> Page:
    """What the normalize stage writes for a book printed in `script`."""
    return normalize_page(transliterate.page_to_devanagari(page, script))


def flags_of(page: Page) -> dict[str, list[str]]:
    return validate_page(page, None).block_flags


# ---------------------------------------------------------------- the real Kannada page


def test_p0064_read1_u0331_is_flagged_and_every_mark_is_reported():
    page = converted(Page.load(FIXTURES / "p0064.read1.json"), "kannada")
    want = EXPECTED["files"]["p0064.read1.json"]["u0331"]
    # Not replaced: the marks are still in the converted text, exactly as OCR gave them...
    assert sum(line.count(MACRON_BELOW) for line in page.block("b3").lines) == want == 26
    report = validate_page(page, None)
    # ...and not kept silently: the block is flagged and the notes account for every one of them.
    assert "non_vedic_mark" in report.block_flags["b3"]
    notes = [n for n in report.notes if "U+0331" in n]
    assert sum(int(n.split(" x")[1].split()[0]) for n in notes) == want
    assert any(n.startswith("b3 line 6: non_vedic_mark U+0331 COMBINING MACRON BELOW x3") for n in notes)


@pytest.mark.parametrize("name", ["p0005.read1", "p0005.read2", "p0010.read1", "p0010.read2", "p0064.read2"])
def test_clean_kannada_reads_get_no_stray_flags(name):
    page = converted(Page.load(FIXTURES / f"{name}.json"), "kannada")
    flagged = {f for fs in flags_of(page).values() for f in fs}
    assert not flagged & {"non_vedic_mark", "unconverted_letter"}


# ---------------------------------------------------------------- letters the conversion does not know


@pytest.mark.parametrize("script, line, char", [
    ("telugu", "ౘను॒ నమః", "ౘ"),  # Telugu TSA: vidyut leaves it in the Devanagari text
    ("telugu", "ౙ॒ర నమః", "ౙ"),
    ("kannada", "ತೇ॒ೝ ನಮಃ", "ೝ"),  # Kannada nakaara pollu
])
def test_unconverted_source_letter_is_flagged(script, line, char):
    page = converted(Page(page=1, blocks=[Block(id="v", type="verse", lines=[line])]), script)
    assert char in page.block("v").lines[0]  # still there, not dropped or guessed at
    report = validate_page(page, None)
    assert report.block_flags == {"v": ["unconverted_letter"]}
    assert any(f"U+{ord(char):04X}" in n for n in report.notes)


def test_cells_labels_and_unaccented_block_types_are_checked_too():
    page = converted(Page(page=1, blocks=[
        Block(id="h", type="running_header", lines=["ౘ"]),
        Block(id="t", type="table", rows=[["నమః", "ౘ"]]),
        Block(id="i", type="image", bbox=[0, 0, 1, 1], labels=[Label(text="ౘ", lang="sa", bbox=[0, 0, 1, 1])]),
        Block(id="te", type="prose", lang="te", lines=["ౘ"]),  # Telugu text keeps its own script: fine
    ]), "telugu")
    assert flags_of(page) == {"h": ["unconverted_letter"], "t": ["unconverted_letter"], "i": ["unconverted_letter"]}


def test_non_vedic_mark_in_a_devanagari_book_is_flagged_too():
    page = Page(page=1, blocks=[Block(id="v", type="verse", lines=["बहु̱धा वि॒रू॑पम्", "ꣳ᳚ शान्तिः॥"])])
    assert flags_of(page) == {"v": ["non_vedic_mark"]}


@pytest.mark.parametrize("flag", ["non_vedic_mark", "unconverted_letter"])
def test_a_revalidation_that_no_longer_finds_it_removes_the_flag(tmp_path, flag):
    # The flags belong to validate: a page validated again without the finding loses them in the master.
    from vedalipi.schema import FlagReport

    cfg = RunConfig(input_path=tmp_path / "in.json", out_dir=tmp_path / "out")
    d = pipeline.page_dir(cfg.work_dir, 1)
    d.mkdir(parents=True)
    Page(page=1, blocks=[Block(id="b1", type="verse", lines=["क"])]).save(d / pipeline.NORM_JSON)
    FlagReport(page=1, block_flags={"b1": [flag, "low_confidence"]}).save(d / pipeline.FLAGS_JSON)
    assert pipeline.build_master(cfg, [1]).page(1).block("b1").flags == [flag, "low_confidence"]
    FlagReport(page=1, block_flags={"b1": ["low_confidence"]}).save(d / pipeline.FLAGS_JSON)
    assert pipeline.build_master(cfg, [1]).page(1).block("b1").flags == ["low_confidence"]


# ---------------------------------------------------------------- whole run and report


def test_run_and_report_on_p0064_read1(tmp_path, capsys):
    cfg = RunConfig(input_path=FIXTURES / "p0064.read1.json", out_dir=tmp_path / "out", jobs=1,
                    source_script="kannada", languages=("kn", "sa"))
    runner = pipeline.StageRunner(pipeline.default_stages("json"), _manifest(cfg), cfg,
                                  {"input_hash": "x", "text_pages": [Page.load(FIXTURES / "p0064.read1.json")]})
    runner.run([1])
    pipeline.build_master(cfg, [1])
    master = Master.load(cfg.master_path)
    assert "non_vedic_mark" in master.page(1).block("b3").flags
    assert sum(line.count(MACRON_BELOW) for line in master.page(1).block("b3").lines) == 26

    assert main(["report", str(cfg.out_dir)]) == 0
    out = capsys.readouterr().out
    assert "non_vedic_mark: 1 (p1:b3)" in out
    assert "p1:b3 line 6: non_vedic_mark U+0331 COMBINING MACRON BELOW x3" in out
    assert "total: non_vedic_mark U+0331 COMBINING MACRON BELOW x26" in out


def _manifest(cfg: RunConfig):
    from vedalipi.schema import Manifest

    return Manifest(input_path=str(cfg.input_path), input_hash="x", input_kind="json", num_pages=1)
