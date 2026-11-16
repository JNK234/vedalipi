# ABOUTME: Every source script to every target script: the three fixture pages as Kannada, Telugu and Devanagari books
# ABOUTME: (made from read 1 by the tool's own converter) through run, judge, verify and proof into four targets.

from __future__ import annotations

import contextlib
import io
import json
import unicodedata
from pathlib import Path

import pytest

from vedalipi import proof as pf
from vedalipi import transliterate
from vedalipi import verify as vf
from vedalipi.cli import main
from vedalipi.pipeline import to_master_form
from vedalipi.schema import Block, Master, Page

FIXTURES = Path(__file__).parent / "fixtures" / "kannada_ocr"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
READ1 = ["p0005.read1.json", "p0010.read1.json", "p0064.read1.json"]
BOOKS = {"kannada": "book.kn.txt", "telugu": "book.te.txt", "devanagari": "book.sa.txt"}
TARGETS = ["kannada", "telugu", "devanagari", "iast"]
MACRON = "\u0331"


def master_lines(name: str) -> list[str]:
    return [line for b in to_master_form(Page.load(FIXTURES / name), "kannada").blocks for line in b.lines]


def test_the_telugu_and_devanagari_books_are_read1_converted_by_the_tool():
    # The fixture books are exactly what the converter makes of read 1: they cannot drift from it.
    deva = ["\n".join(master_lines(name)) for name in READ1]
    telugu = ["\n".join(transliterate.transliterate_text(line, "telugu") for line in page.split("\n")) for page in deva]
    assert (FIXTURES / "book.sa.txt").read_text(encoding="utf-8") == "\f".join(deva) + "\n"
    assert (FIXTURES / "book.te.txt").read_text(encoding="utf-8") == "\f".join(telugu) + "\n"


def quiet(args: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main(args)
    return code, out.getvalue()


@pytest.fixture(scope="module")
def books(tmp_path_factory) -> dict[tuple[str, str], Path]:
    done = {}
    for source, book in BOOKS.items():
        for target in TARGETS:
            out = tmp_path_factory.mktemp(f"{source}-{target}")
            code, _ = quiet(["run", str(FIXTURES / book), "--source-script", source, "--script", target, "--out", str(out)])
            assert code == 0, (source, target)
            done[(source, target)] = out
    return done


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("source", BOOKS)
def test_every_source_to_every_target(books, source, target):
    out = books[(source, target)]
    master = Master.load(out / "master.json")
    # One master, whatever script the book was printed in: the Devanagari of read 1.
    assert [[line for b in p.blocks for line in b.lines] for p in master.pages] == [master_lines(n) for n in READ1]
    assert quiet(["judge", str(out), "--script", target])[0] == 0
    report = vf.verify(out, target)
    for res, name in zip(report.pages, READ1):
        want = EXPECTED["files"][name]
        stray = want["u0331"] if target == "iast" else 0  # IAST prints a stray macron as its anudatta
        assert res.source[:3] == (want["u0951"], want["u0952"], want["u1cda"])
        assert res.output[:3] == (want["u0951"], want["u0952"] + stray, want["u1cda"])
        if res.source[3] >= 0:  # ꣳ is counted in Indic targets (roman scripts write it like candrabindu)
            assert res.source[3] == res.output[3] == want["expected_A8F3_after_normalize"]
    # Only page 3 needs a check, and only for the Latin macrons OCR read there: nothing else is wrong.
    assert [r.status for r in report.pages] == ["PASS", "PASS", "CHECK"]
    assert set(report.pages[2].reasons) == {"non_vedic_mark"} | (
        {"roundtrip_fail", "output_svara_mismatch"} if target == "iast" else set())
    macron_lines = [n for n, line in enumerate(master_lines(READ1[2]), 1) if MACRON in line]
    assert sorted(i.line for i in pf.proof(out, target).items) == macron_lines


def test_in_iast_a_stray_macron_looks_like_an_anudatta_and_verify_says_so(books):
    # IAST writes the anudatta as U+0331 itself, so a stray U+0331 prints as an anudatta there. verify
    # must not let that pass: the output has more anudattas than the source.
    res = vf.verify(books[("kannada", "iast")], "iast").pages[2]
    source_macrons = sum(line.count(MACRON) for line in master_lines(READ1[2]))
    assert res.output[1] - res.source[1] == source_macrons == EXPECTED["files"]["p0064.read1.json"]["u0331"]


@pytest.mark.parametrize("line", ["अहि॑ꣳ", "श्छ॒न्दा॒ꣳस्यापो॒", "अश्वा॒ꣳ॒ अन्वे॑तु", "अन्न॒ꣳ॒ रक्षं॑तौ"])
def test_iast_a8f3_next_to_or_under_a_svara_round_trips(line):
    # ꣳ prints as m̐ in IAST; a svara before it or on it must read back to the same Devanagari.
    master = Master(pages=[Page(page=1, blocks=[Block(id="b", type="verse", lines=[line])])])
    diagnostics: list[dict] = []
    out = transliterate.transliterate_master(master, "iast", diagnostics=diagnostics)
    assert diagnostics == []
    assert "m\u0310" in unicodedata.normalize("NFD", out.pages[0].blocks[0].lines[0]).replace("\u0331", "").replace("\u030d", "")
