# ABOUTME: Tests for `vedalipi verify`: PASS/CHECK per page with reasons, exit code, and the source -> output
# ABOUTME: svara count (॑ ॒ ᳚ and ꣳ per Sanskrit line). Uses small text books and the Kannada fixture; no OCR.

from __future__ import annotations

from pathlib import Path

import pytest

from vedalipi import pipeline, transliterate
from vedalipi import verify as vf
from vedalipi.cli import main
from vedalipi.pipeline import RunConfig
from vedalipi.schema import (
    Correction,
    Manifest,
    Master,
    StageRecord,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kannada_ocr"


def text_book(tmp_path: Path, pages: list[list[str]], script: str = "devanagari", target: str = "kannada") -> Path:
    """Run a .txt book (one list of lines per page, written in `script`) and render `target`; returns the out dir."""
    src = tmp_path / "book.txt"
    src.write_text("\f".join("\n".join(p) for p in pages) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    cfg = RunConfig(input_path=src, out_dir=out, script=target, source_script=script, jobs=1)
    runner, pdf = pipeline.run(cfg)
    assert not runner.failed and pdf is not None
    return out


CLEAN = [["अ॒ग्निमी॑ळे पु॒रोहि॑तं", "य॒ज्ञस्य॑ दे॒वमृ॒त्विज॑म्"], ["शान्तिः॑ ॥ इ॒दꣳ सर्वं᳚"]]


def test_clean_book_passes_with_exit_0(tmp_path, capsys):
    out = text_book(tmp_path, CLEAN)
    assert main(["verify", str(out), "--script", "kannada"]) == 0
    text = capsys.readouterr().out
    assert "p1    PASS" in text and "p2    PASS" in text and "2 pages: 2 PASS, 0 CHECK" in text


def test_page_counts_source_and_output(tmp_path):
    out = text_book(tmp_path, CLEAN)
    report = vf.verify(out, "kannada")
    for res, lines in zip(report.pages, CLEAN):
        text = "".join(lines)
        want = (text.count("\u0951"), text.count("\u0952"), text.count("\u1cda"), text.count("\ua8f3"))
        assert res.source == res.output == want  # ॑ ॒ ᳚ ꣳ; in Kannada ꣳ prints as its stand-in, still one
    assert report.pages[1].output[3] == 1


@pytest.mark.parametrize("flag", ["non_vedic_mark", "unconverted_letter", "reader_disagreement", "single_read",
                                  "invalid_sequence", "svara_count_mismatch", "low_confidence"])
def test_each_flag_in_the_master_makes_the_page_check(tmp_path, flag, capsys):
    out = text_book(tmp_path, CLEAN)
    master = Master.load(out / "master.json")
    master.page(2).blocks[0].flags = [flag, "ambiguous_svara_span"]
    master.save(out / "master.json")
    assert main(["verify", str(out), "--script", "kannada"]) == 1
    text = capsys.readouterr().out
    assert "p1    PASS" in text and "p2    CHECK" in text and f"{flag}: b2-1" in text
    assert "ambiguous_svara_span" not in text  # a hint set by OCR, not one of verify's reasons


def test_a_stray_character_in_the_text_is_a_check_even_without_a_flag(tmp_path):
    # A proofreader may clear flags; a Latin mark still in the text still prints, so verify looks at the text.
    out = text_book(tmp_path, CLEAN)
    master = pipeline.apply_corrections(Master.load(out / "master.json"),
                                        [Correction(page=1, block_id="b1-1", lines=["बहु̱धा", "ౘ"], flags=[])])
    master.save(out / "master.json")
    page = vf.verify(out, "kannada").pages[0]
    assert page.status == "CHECK" and set(page.reasons) == {"non_vedic_mark", "unconverted_letter"}


def test_lost_svara_in_the_output_is_caught(tmp_path, monkeypatch):
    out = text_book(tmp_path, CLEAN)
    real = transliterate._convert

    def drops_double_svarita(text, src, dst):  # a converter that loses ᳚ on the way to Kannada
        got = real(text, src, dst)
        return got.replace("\u1cda", "") if dst == "kannada" else got

    monkeypatch.setattr(transliterate, "_convert", drops_double_svarita)
    report = vf.verify(out, "kannada")
    p2 = report.pages[1]
    assert p2.status == "CHECK" and p2.output[2] == 0 and p2.source[2] == 1
    assert any("b2-1 line 1: ᳚ 1 -> 0" in d for d in p2.reasons["output_svara_mismatch"])
    assert "roundtrip_fail" in p2.reasons  # the round trip sees the same loss


def test_lost_a8f3_in_the_output_is_caught(tmp_path, monkeypatch):
    out = text_book(tmp_path, CLEAN)
    monkeypatch.setitem(transliterate.DEFAULT_A8F3, "kannada", "")  # a stand-in that prints nothing
    p2 = vf.verify(out, "kannada").pages[1]
    assert p2.output[3] == 0 and any("ꣳ 1 -> 0" in d for d in p2.reasons["output_svara_mismatch"])


def test_source_is_the_text_as_read_in_the_book_script(tmp_path):
    # A Kannada book: the source count is taken from the Kannada text, the gum spelling counted as one ꣳ.
    lines = ["ಪರಿ॒ಷ॑ದ್ಯಗ್ಗ್ಂ ಸ್ತು॒ವಂತಃ। ಅಹಿ॑ಗ್ಂ ರಕ್ಷಂ॒ತಿ", "ಗಂ॑ತು"]
    out = text_book(tmp_path, [lines], script="kannada", target="telugu")
    page = vf.verify(out, "telugu").pages[0]
    text = "".join(lines)
    # One doubled and one single spelling of ꣳ; ಗಂ (ga with anusvara) is not one.
    gum = text.count("ಗ್ಗ್ಂ") + text.replace("ಗ್ಗ್ಂ", "").count("ಗ್ಂ")
    want = (text.count("\u0951"), text.count("\u0952"), text.count("\u1cda"), gum)
    assert gum == 2 and page.source_from == "source text" and page.source == page.output == want
    assert page.status == "PASS"


def test_a_corrected_block_is_counted_from_the_master(tmp_path):
    out = text_book(tmp_path, CLEAN)
    fixed = pipeline.apply_corrections(Master.load(out / "master.json"),
                                       [Correction(page=1, block_id="b1-1", lines=["अ॒ग्निमी॑ळे"])])
    fixed.save(out / "master.json")
    page = vf.verify(out, "kannada").pages[0]
    assert page.source == page.output == (1, 1, 0, 0) and page.status == "PASS"
    assert page.source_from == "master (corrected)"


def test_a_page_whose_stage_failed_is_a_check(tmp_path, capsys):
    out = text_book(tmp_path, CLEAN)
    m = Manifest.load(out / "work" / "manifest.json")
    m.set_record(2, "normalize", StageRecord(status="failed", error="ValueError: boom"))
    m.save(out / "work" / "manifest.json")
    master = Master.load(out / "master.json")
    master.pages = [p for p in master.pages if p.page != 2]
    master.save(out / "master.json")
    assert main(["verify", str(out), "--script", "kannada"]) == 1
    assert "p2    CHECK" in capsys.readouterr().out


def test_script_defaults_to_the_last_rendered_one(tmp_path):
    out = text_book(tmp_path, CLEAN, target="telugu")
    assert vf.verify(out).script == "telugu"


def test_run_records_the_source_script(tmp_path):
    out = text_book(tmp_path, [["ಅಹಿ॑ಗ್ಂ"]], script="kannada")
    assert Manifest.load(out / "work" / "manifest.json").source_script == "kannada"


def test_verify_writes_nothing(tmp_path):
    out = text_book(tmp_path, CLEAN)
    before = {p: p.stat().st_mtime_ns for p in out.rglob("*") if p.is_file()}
    vf.verify(out, "kannada")
    assert {p: p.stat().st_mtime_ns for p in out.rglob("*") if p.is_file()} == before


def test_roman_target_counts_svaras_through_the_latin_marks(tmp_path):
    out = text_book(tmp_path, CLEAN, target="iast")
    page = vf.verify(out, "iast").pages[0]
    assert page.source[:3] == page.output[:3] == (4, 5, 0) and page.status == "PASS"


@pytest.mark.parametrize("text,script", [("अहिग्॑ं", "devanagari"), ("ಅಹಿಗ್॑ಂ", "kannada"), ("అహిగ్గ్॒ం", "telugu")])
def test_gum_spelling_with_a_svara_before_the_anusvara_counts_once(text, script):
    # normalize turns these into ꣳ with the svara after it; the source count must see the same one ꣳ.
    from vedalipi.normalize import normalize_text
    from vedalipi.transliterate import _convert
    from vedalipi.verify import count
    assert normalize_text(_convert(text, script, "devanagari")).count("ꣳ") == 1
    assert count(text, script)[3] == 1
