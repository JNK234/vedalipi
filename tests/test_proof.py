# ABOUTME: Tests for `vedalipi proof` (lines to check, most svara differences first, and corrections.todo.json) and
# ABOUTME: for applying that todo file: checked lines become line corrections that survive a re-normalize.

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from vedalipi import normalize
from vedalipi import proof as pf
from vedalipi import verify as vf
from vedalipi.cli import main
from vedalipi.schema import Master

FIXTURES = Path(__file__).parent / "fixtures" / "kannada_ocr"
MACRON, ANUDATTA = "\u0331", "\u0952"


def run(out: Path, book: Path = FIXTURES / "book.kn.txt", second: Path | None = FIXTURES / "book2.kn.txt") -> int:
    args = ["run", str(book), "--source-script", "kannada", "--script", "telugu", "--out", str(out)]
    return main(args + (["--second-read", str(second)] if second else []))


@pytest.fixture
def book(tmp_path) -> Path:
    out = tmp_path / "out"
    assert run(out) == 0
    return out


def todo(out: Path) -> dict:
    return json.loads((out / "corrections.todo.json").read_text(encoding="utf-8"))


def save_todo(out: Path, data: dict) -> Path:
    path = out / "corrections.todo.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


# ---------------------------------------------------------------- the queue


def test_lines_are_ranked_by_svara_differences(book, capsys):
    assert main(["proof", str(book)]) == 1  # lines to check: exit 1, like verify
    items = pf.proof(book).items
    diffs = [i.svara_differences for i in items]
    assert diffs == sorted(diffs, reverse=True) and diffs[0] > 0
    ganapati = next(i for i in items if (i.page, i.line) == (1, 1))
    assert "reader_disagreement" in ganapati.reasons and "क॑विं / क॒विं" in ganapati.svaras
    assert "क॒विं" in ganapati.second_read
    # As printed (Kannada) and as the master holds it (Devanagari).
    assert ganapati.as_read.startswith("ಓಂ ಗ॒ಣಾನಾಂ᳚") and ganapati.master.startswith("ॐ ग॒णानां᳚")
    out = capsys.readouterr().out
    assert out.index("#1 ") < out.index("#2 ") and "svara differences" in out


def test_a_latin_macron_read_for_an_anudatta_counts_as_a_svara_difference(book):
    items = pf.proof(book).items
    line6 = next(i for i in items if (i.page, i.line) == (3, 6))
    assert {"non_vedic_mark", "reader_disagreement"} <= set(line6.reasons)
    assert line6.svara_differences >= 3  # बहु̱धा / बहु॒धा and the two others on the line


def test_todo_file_has_one_entry_per_line_ready_to_edit(book):
    main(["proof", str(book)])
    data = todo(book)
    entries = data["entries"]
    assert len(entries) == len(pf.proof(book).items) and len({e["id"] for e in entries}) == len(entries)
    first = entries[0]
    assert first["rank"] == 1 and first["text"] == first["master"] and first["checked"] is False
    assert {"page", "block_id", "line", "reasons", "svara_differences", "svaras", "as_read", "second_read"} <= set(first)
    assert "apply-corrections" in data["about"]


def test_a_line_only_the_second_read_has_is_shown_on_the_line_before_it(tmp_path):
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("अ॒ग्निमी॑ळे पु॒रोहि॑तम्\nहोता॑रं रत्न॒धात॑मम्\n", encoding="utf-8")
    second.write_text("अ॒ग्निमी॑ळे पु॒रोहि॑तम्\nय॒ज्ञस्य॑ दे॒वमृ॒त्विज॑म्\nहोता॑रं रत्न॒धात॑मम्\n", encoding="utf-8")
    out = tmp_path / "out"
    assert main(["run", str(first), "--second-read", str(second), "--out", str(out)]) == 0
    (item,) = pf.proof(out).items
    assert (item.line, item.reasons) == (1, ["single_read"])
    assert any("य॒ज्ञस्य॑ दे॒वमृ॒त्विज॑म्" in n for n in item.notes)


def test_low_confidence_lists_every_line_of_its_block(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("अ॒ग्निः\nसोमः॑\n", encoding="utf-8")
    out = tmp_path / "out"
    assert main(["run", str(src), "--out", str(out)]) == 0
    master = Master.load(out / "master.json")
    master.pages[0].blocks[0].flags = ["low_confidence"]
    master.save(out / "master.json")
    assert [(i.line, i.reasons) for i in pf.proof(out).items] == [(1, ["low_confidence"]), (2, ["low_confidence"])]


def test_a_clean_book_has_nothing_to_proofread(tmp_path, capsys):
    src = tmp_path / "a.txt"
    src.write_text("अ॒ग्निः\n", encoding="utf-8")
    out = tmp_path / "out"
    assert main(["run", str(src), "--out", str(out)]) == 0
    assert main(["proof", str(out)]) == 0 and todo(out)["entries"] == []


# ---------------------------------------------------------------- applying the todo file


def check_page(data: dict, page: int, fix=lambda e: e["text"]) -> None:
    for e in data["entries"]:
        if e["page"] == page:
            e["text"], e["checked"] = fix(e), True


def test_checked_lines_are_applied_and_a_fully_checked_block_loses_its_flags(book, capsys):
    main(["proof", str(book)])
    data = todo(book)
    check_page(data, 1, lambda e: e["text"].replace("क॑विं", "क॒विं"))
    capsys.readouterr()
    assert main(["apply-corrections", str(book), str(save_todo(book, data))]) == 0
    out = capsys.readouterr().out
    assert "p1 b1-1: 6 of 6 listed lines checked, 1 edited; flags cleared: reader_disagreement" in out
    master = Master.load(book / "master.json")
    assert "क॒विं" in master.page(1).blocks[0].lines[0] and master.page(1).blocks[0].flags == []
    (c,) = master.corrections
    assert c.set_lines == {1: master.page(1).blocks[0].lines[0]} and c.clear_flags == ["reader_disagreement"]
    assert vf.verify(book, "telugu").pages[0].status == "PASS"


def test_a_partly_checked_block_keeps_its_flags_and_proof_keeps_the_checks(book, capsys):
    main(["proof", str(book)])
    data = todo(book)
    p3 = [e for e in data["entries"] if e["page"] == 3]
    for e in p3[:2]:
        e["text"], e["checked"] = e["text"].replace(MACRON, ANUDATTA), True
    capsys.readouterr()
    main(["apply-corrections", str(book), str(save_todo(book, data))])
    assert f"2 of {len(p3)} listed lines checked" in capsys.readouterr().out
    assert "reader_disagreement" in Master.load(book / "master.json").page(3).blocks[0].flags
    main(["proof", str(book)])
    again = {e["id"]: e for e in todo(book)["entries"]}
    assert all(again[e["id"]]["checked"] for e in p3[:2])  # proof run again: the checks are kept


def test_an_edited_but_unchecked_line_is_not_applied_and_says_so(book, capsys):
    main(["proof", str(book)])
    data = todo(book)
    data["entries"][0]["text"] += " x"
    capsys.readouterr()
    main(["apply-corrections", str(book), str(save_todo(book, data))])
    out = capsys.readouterr().out
    assert "edited but not checked, not applied" in out and not Master.load(book / "master.json").corrections


def test_proof_run_again_keeps_unapplied_edits_and_drops_resolved_lines(book, capsys):
    main(["proof", str(book)])
    data = todo(book)
    data["entries"][0]["text"] += " x"
    check_page(data, 1)
    save_todo(book, data)
    main(["apply-corrections", str(book), str(book / "corrections.todo.json")])
    capsys.readouterr()
    main(["proof", str(book)])
    out = capsys.readouterr().out
    ids = {e["id"] for e in todo(book)["entries"]}
    assert not any(i.startswith("p1:") for i in ids)  # page 1 is proofread
    assert re.search(r"\d+ resolved", out)


def test_todo_corrections_survive_a_normalize_change(book, monkeypatch, capsys):
    main(["proof", str(book)])
    data = todo(book)
    check_page(data, 3, lambda e: e["text"].replace(MACRON, ANUDATTA))
    main(["apply-corrections", str(book), str(save_todo(book, data))])
    before = Master.load(book / "master.json").page(3).blocks[0].lines
    edited = {n for n, line in enumerate(before) if ANUDATTA in line and n in {5, 6, 7, 8, 9, 10, 11}}
    # A newer normalize (version 4) that also drops the comma OCR reads after a final consonant (page 3,
    # lines 13 and 14) and collapses runs of spaces (page 2): the OCR text is the same, the master is not.
    real = normalize.normalize_text
    monkeypatch.setattr(normalize, "NORMALIZE_VERSION", "4")
    monkeypatch.setattr(normalize, "normalize_text", lambda t: re.sub(" {2,}", " ", real(t).replace("्,", "्")))
    capsys.readouterr()
    assert run(book) == 0
    err = capsys.readouterr().err
    assert "[p0003] normalize changed (version 3 -> 4); OCR text unchanged: page rebuilt, 1 correction re-applied" in err
    assert "dropped" not in err
    after = Master.load(book / "master.json").page(3).blocks[0]
    assert all(after.lines[n] == before[n] for n in edited)  # the proofreader's edits
    assert "," in before[12] and "," not in after.lines[12]  # an unedited line follows the new normalize
    assert after.flags == [] and vf.verify(book, "telugu").pages[2].status == "PASS"


def test_a_flagged_table_is_listed_even_though_it_has_no_lines(tmp_path, capsys):
    src = tmp_path / "a.txt"
    src.write_text("अ॒ग्निः\n", encoding="utf-8")
    out = tmp_path / "out"
    assert main(["run", str(src), "--out", str(out)]) == 0
    master = Master.load(out / "master.json")
    from vedalipi.schema import Block

    master.pages[0].blocks.append(Block(id="t1", type="table", rows=[["नमः", "१"]], flags=["low_confidence"]))
    master.save(out / "master.json")
    capsys.readouterr()
    assert main(["proof", str(out)]) == 1
    assert "p1: t1 (table): low_confidence; not in the todo file: correct it with rows or flags" in capsys.readouterr().out
