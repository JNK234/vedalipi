# ABOUTME: Tests that --cross-read compares like with like for books in other scripts: the second read goes
# ABOUTME: through the same Devanagari conversion and normalize as the first. Real Kannada reads, no OCR.

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vedalipi import pipeline, transliterate
from vedalipi.pipeline import RunConfig
from vedalipi.schema import Block, FlagReport, Manifest, Page

FIXTURES = Path(__file__).parent / "fixtures" / "kannada_ocr"


def cross_read_page(tmp_path: Path, name: str, script: str = "kannada", fixtures: Path = FIXTURES) -> FlagReport:
    """Put <name>.read1/.read2 in a work folder as ocr.json/ocr2.json and run only normalize and validate."""
    first = Page.load(fixtures / f"{name}.read1.json")
    cfg = RunConfig(input_path=fixtures / f"{name}.read1.json", out_dir=tmp_path / "out", jobs=1,
                    source_script=script, languages=("kn", "sa"))
    d = pipeline.page_dir(cfg.work_dir, first.page)
    d.mkdir(parents=True)
    shutil.copy(fixtures / f"{name}.read1.json", d / pipeline.OCR_JSON)
    shutil.copy(fixtures / f"{name}.read2.json", d / pipeline.OCR2_JSON)
    stages = [s for s in pipeline.default_stages("pdf", cross_read=True) if s.name in ("normalize", "validate")]
    manifest = Manifest(input_path=str(cfg.input_path), input_hash="x", input_kind="pdf", num_pages=first.page)
    runner = pipeline.StageRunner(stages, manifest, cfg, {"input_hash": "x"})
    runner.run([first.page])
    assert not runner.failed and [s for _, s in runner.ran] == ["normalize", "validate"]
    return FlagReport.load(d / pipeline.FLAGS_JSON)


def disagreements(report: FlagReport) -> list[str]:
    return [n for n in report.notes if "second read differs" in n]


def test_kannada_second_read_is_compared_in_devanagari(tmp_path):
    report = cross_read_page(tmp_path, "p0005")
    assert "reader_disagreement" in report.block_flags["b3"]
    notes = disagreements(report)
    # The Ganapati mantra: read 1 has क॑विं, read 2 (and the printed page) क॒विं. Both sides are Devanagari.
    ganapati = [n for n in notes if "क॑विं" in n]
    assert ganapati and all("क॒विं" in n for n in ganapati)
    assert not any(ch in n for n in notes for ch in "ಕವಿಂ")  # no Kannada left in a compared line


@pytest.mark.parametrize("name", ["p0005", "p0010", "p0064"])
def test_every_line_of_the_first_read_is_accounted_for_once(tmp_path, name):
    # Each first-read line is identical in the second read, or read differently, or read only once.
    # The identical lines are counted here directly from the two converted reads, not by the checker.
    report = cross_read_page(tmp_path, name)
    first, second = (pipeline.to_master_form(Page.load(FIXTURES / f"{name}.read{r}.json"), "kannada") for r in (1, 2))
    key = lambda line: "".join(line.split())  # noqa: E731
    lines1 = [key(line) for b in first.blocks if b.is_sanskrit for line in b.lines]
    lines2 = {key(line) for b in second.blocks if b.is_sanskrit for line in b.lines}
    identical = sum(line in lines2 for line in lines1)
    only_first = [n for n in single_reads(report) if "only the first read has" in n]
    assert len(disagreements(report)) + len(only_first) + identical == len(lines1)
    assert disagreements(report) and not any(0x0C80 <= ord(c) <= 0x0CFF for n in report.notes for c in n)


@pytest.mark.parametrize("script", ["devanagari", "telugu"])
def test_same_two_reads_printed_in_another_script(tmp_path, script):
    # The p0005 reads as a Devanagari or a Telugu book would give them: the same disagreement is found.
    fixtures = tmp_path / script
    fixtures.mkdir()
    for read in ("read1", "read2"):
        page = transliterate.page_to_devanagari(Page.load(FIXTURES / f"p0005.{read}.json"), "kannada")
        for b in page.blocks:
            if b.is_sanskrit:
                b.lines = [transliterate.transliterate_text(line, script, a8f3="\ua8f3") for line in b.lines]
        page.save(fixtures / f"p0005.{read}.json")
    report = cross_read_page(tmp_path, "p0005", script=script, fixtures=fixtures)
    assert "reader_disagreement" in report.block_flags["b3"]
    assert any("क॑विं" in n and "क॒विं" in n for n in disagreements(report))


def test_validate_fingerprint_follows_normalize_version(tmp_path, monkeypatch):
    # The second read is normalized inside validate, so a normalize change must re-run validate too.
    from vedalipi import normalize

    cfg = RunConfig(input_path=tmp_path / "in.pdf", out_dir=tmp_path / "out", source_script="kannada")
    job = pipeline.PageJob(1, pipeline.page_dir(cfg.work_dir, 1), cfg)
    before = pipeline._validate_fingerprint(job)
    monkeypatch.setattr(normalize, "NORMALIZE_VERSION", "test")
    assert pipeline._validate_fingerprint(job) != before


# ---------------------------------------------------------------- lines only one read has


def single_reads(report: FlagReport) -> list[str]:
    return [n for n in report.notes if ": single_read: " in n]


def test_p0010_lines_only_one_read_has_are_flagged(tmp_path):
    report = cross_read_page(tmp_path, "p0010")
    # Read 1 has 17 lines, read 2 only 11: read 2 skips read 1's b5 lines 8-13. The other 11 pair one to one
    # (one identical, 10 read differently). The repeated phrases of this mantra must not pair line 8 with
    # the line read 2 has for line 7.
    assert len(disagreements(report)) == 10
    assert single_reads(report) == [f"b5 line {n}: single_read: only the first read has this line" for n in range(8, 14)]
    assert "single_read" in report.block_flags["b5"]


def test_a_line_only_the_second_read_has_is_quoted_on_the_nearest_block():
    from vedalipi.validate import validate_page

    first = Page(page=1, blocks=[Block(id="b1", type="verse", lines=["अ॒ग्निमी॑ळे पु॒रोहि॑तम्", "होता॑रं रत्न॒धात॑मम्"])])
    second = Page(page=1, blocks=[Block(id="x", type="verse", lines=[
        "अ॒ग्निमी॑ळे पु॒रोहि॑तम्", "य॒ज्ञस्य॑ दे॒वमृ॒त्विज॑म्", "होता॑रं रत्न॒धात॑मम्"])])
    report = validate_page(first, None, second)
    assert report.block_flags == {"b1": ["single_read"]}
    assert report.notes == ["b1 after line 1: single_read: only the second read has: य॒ज्ञस्य॑ दे॒वमृ॒त्विज॑म्"]
    # The other way round: the master has a line the second read lacks.
    report = validate_page(second.model_copy(update={"blocks": [second.blocks[0].model_copy(update={"id": "b1"})]}),
                           None, first)
    assert report.notes == ["b1 line 2: single_read: only the first read has this line"]


def test_an_image_checked_page_is_left_as_before(tmp_path):
    import cv2
    import numpy as np

    from vedalipi.validate import validate_page

    img = np.full((300, 1200), 255, np.uint8)
    for x in (100, 500, 900):
        cv2.rectangle(img, (x - 40, 100), (x + 240, 108), 0, -1)
        cv2.rectangle(img, (x, 108), (x + 120, 190), 0, -1)
    cv2.imwrite(str(tmp_path / "line.png"), img)
    first = Page(page=1, blocks=[Block(id="b1", type="verse", lines=["कखग"])])
    second = Page(page=1, blocks=[Block(id="x", type="verse", lines=["कखग", "घङच"])])
    assert validate_page(first, tmp_path / "line.png", second).block_flags == {}
    assert validate_page(first, None, second).block_flags == {"b1": ["single_read"]}


def test_single_read_is_in_the_report_and_leaves_with_a_revalidation(tmp_path):
    cross_read_page(tmp_path, "p0010")
    cfg = RunConfig(input_path=FIXTURES / "p0010.read1.json", out_dir=tmp_path / "out")
    pipeline.build_master(cfg, [10])
    out = pipeline.report(cfg.out_dir)
    assert "single_read: 1 (p10:b5)" in out
    assert "p10:b5 line 8: single_read: only the first read has this line" in out
    # Validated again with a second read that has every line: the flag and its report lines are gone.
    d = pipeline.page_dir(cfg.work_dir, 10)
    (d / pipeline.OCR2_JSON).write_text((d / pipeline.OCR_JSON).read_text(encoding="utf-8"), encoding="utf-8")
    pipeline._validate(pipeline.PageJob(10, d, RunConfig(input_path=cfg.input_path, out_dir=cfg.out_dir,
                                                          source_script="kannada")))
    pipeline.build_master(cfg, [10])
    assert "single_read" not in pipeline.report(cfg.out_dir)
