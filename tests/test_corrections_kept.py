# ABOUTME: Tests that hand corrections survive a re-run when only normalize or validate changed (they are re-applied
# ABOUTME: and the real cause is printed), and are dropped with a warning and saved only when the OCR text changed.

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from vedalipi import normalize, validate
from vedalipi.cli import main
from vedalipi.schema import Master

FIXTURES = Path(__file__).parent / "fixtures" / "kannada_ocr"
MACRON, ANUDATTA = "\u0331", "\u0952"


@pytest.fixture
def book(tmp_path) -> Path:
    src = tmp_path / "book.kn.txt"
    shutil.copy(FIXTURES / "book.kn.txt", src)
    return src


def run(book: Path, out: Path) -> int:
    return main(["run", str(book), "--source-script", "kannada", "--script", "telugu", "--out", str(out)])


def correct_p3_line6(out: Path, tmp_path: Path, **fields) -> str:
    """Apply a correction to page 3 (line 6: the Latin macrons read as anudattas); returns the corrected line."""
    lines = list(Master.load(out / "master.json").page(3).blocks[0].lines)
    lines[5] = lines[5].replace(MACRON, ANUDATTA)
    fix = tmp_path / "fix.json"
    fix.write_text(json.dumps([{"page": 3, "block_id": "b3-1", "lines": lines, "flags": [], **fields}],
                              ensure_ascii=False), encoding="utf-8")
    assert main(["apply-corrections", str(out), str(fix)]) == 0
    return lines[5]


def test_older_normalize_then_current_keeps_the_correction(book, tmp_path, monkeypatch, capsys):
    # The upgrade of step 2: the book was built before normalize wrote ꣳ, then normalize changed.
    out = tmp_path / "out"
    monkeypatch.setattr(normalize, "_GUM", re.compile("(?!)"))
    monkeypatch.setattr(normalize, "NORMALIZE_VERSION", "2-old")
    assert run(book, out) == 0
    fixed = correct_p3_line6(out, tmp_path)
    monkeypatch.undo()
    capsys.readouterr()
    assert run(book, out) == 0
    err = capsys.readouterr().err
    page = Master.load(out / "master.json").page(3)
    # The correction is kept, and its text follows the current normalize like every other line (ꣳ).
    assert page.blocks[0].lines[5] == normalize.normalize_text(fixed) and "ꣳ" in page.blocks[0].lines[5]
    assert page.blocks[0].flags == []
    assert "[p0003] normalize changed (version 2-old -> 3); OCR text unchanged: page rebuilt, 1 correction re-applied" in err
    assert "dropped" not in err and "OCR changed" not in err
    assert "[p0001]" not in err and "[p0002]" not in err  # pages without corrections: nothing to say


def test_a_corrected_line_keeps_its_edit_while_other_lines_get_the_new_normalize(book, tmp_path, monkeypatch):
    # Line corrections (from the proof todo file) re-apply onto the re-normalized page: unedited lines follow
    # the new normalize (here: ꣳ appears), the edited line keeps the proofreader's text.
    out = tmp_path / "out"
    monkeypatch.setattr(normalize, "_GUM", re.compile("(?!)"))
    monkeypatch.setattr(normalize, "NORMALIZE_VERSION", "2-old")
    assert run(book, out) == 0
    old = Master.load(out / "master.json").page(3).blocks[0].lines
    edit = old[5].replace(MACRON, ANUDATTA)
    fix = tmp_path / "fix.json"
    fix.write_text(json.dumps([{"page": 3, "block_id": "b3-1", "set_lines": {"6": edit},
                                "clear_flags": ["non_vedic_mark"]}], ensure_ascii=False), encoding="utf-8")
    assert main(["apply-corrections", str(out), str(fix)]) == 0
    monkeypatch.undo()
    assert run(book, out) == 0
    block = Master.load(out / "master.json").page(3).blocks[0]
    assert block.lines[5] == normalize.normalize_text(edit) and MACRON not in block.lines[5]
    assert "ꣳ" in block.lines[0] and "ꣳ" not in old[0]  # line 1 was re-normalized
    assert "non_vedic_mark" not in block.flags


def test_a_plain_normalize_version_bump_keeps_corrections(book, tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    assert run(book, out) == 0
    fixed = correct_p3_line6(out, tmp_path)
    monkeypatch.setattr(normalize, "NORMALIZE_VERSION", "4")
    capsys.readouterr()
    assert run(book, out) == 0
    assert Master.load(out / "master.json").page(3).blocks[0].lines[5] == fixed
    assert "dropped" not in capsys.readouterr().err


def test_a_validate_change_keeps_cleared_flags(book, tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    assert run(book, out) == 0
    correct_p3_line6(out, tmp_path)
    # A new validator: a new version whose output differs (one more note per page). It flags the page's OCR
    # text again; the proofreader's cleared flags must stay cleared.
    monkeypatch.setattr(validate, "VALIDATE_VERSION", "99")
    real = validate.validate_page

    def newer(page, image, second=None):
        report = real(page, image, second)
        report.notes.append("checked by the newer validator")
        return report

    monkeypatch.setattr(validate, "validate_page", newer)
    capsys.readouterr()
    assert run(book, out) == 0
    assert Master.load(out / "master.json").page(3).blocks[0].flags == []
    assert "[p0003] validate changed (version 6 -> 99): flags refreshed, 1 correction re-applied" in capsys.readouterr().err


def test_changed_ocr_text_drops_corrections_with_a_warning_and_saves_them(book, tmp_path, capsys):
    out = tmp_path / "out"
    assert run(book, out) == 0
    correct_p3_line6(out, tmp_path)
    pages = book.read_text(encoding="utf-8").split("\f")
    pages[2] = pages[2].replace("ಯಜಾಮ", "ಯಜಾಮಹೇ", 1)  # page 3 read again, differently
    book.write_text("\f".join(pages), encoding="utf-8")
    capsys.readouterr()
    assert run(book, out) == 0
    err = capsys.readouterr().err
    assert "[p0003] OCR text changed: page rebuilt, 1 correction dropped (saved in corrections.dropped.json)" in err
    assert "[p0001]" not in err and "[p0002]" not in err
    master = Master.load(out / "master.json")
    assert MACRON in master.page(3).blocks[0].lines[5] and not master.corrections
    saved = json.loads((out / "corrections.dropped.json").read_text(encoding="utf-8"))
    assert saved[0]["reason"] == "OCR text changed" and saved[0]["correction"]["page"] == 3


# ---------------------------------------------------------------- corrected text follows normalize


def test_corrected_text_is_normalized_like_every_other_line(tmp_path):
    from vedalipi import pipeline
    from vedalipi.normalize import normalize_text
    from vedalipi.schema import Block, Correction, Label, Page

    master = Master(pages=[Page(page=1, blocks=[
        Block(id="v", type="verse", lines=["क", "ख"]),
        Block(id="t", type="table", rows=[["क"]]),
        Block(id="e", type="prose", lang="en", lines=["x"]),
        Block(id="i", type="image", bbox=[0, 0, 1, 1]),
    ])])
    typed = "अहिग्ं श\u200dृणु शुभं\u0331"  # old ꣳ spelling, a ZWJ, a foreign mark after the anusvara
    want = normalize_text(typed)
    assert want == "अहिꣳ शृणु शुभं\u0331" and want != typed
    fixed = pipeline.apply_corrections(master, [
        Correction(page=1, block_id="v", set_lines={2: typed}),
        Correction(page=1, block_id="t", rows=[[typed]]),
        Correction(page=1, block_id="e", lines=["श\u200dृ"]),  # not Sanskrit: left as typed
        Correction(page=1, block_id="i", labels=[Label(text=typed, lang="sa", bbox=[0, 0, 1, 1])]),
    ])
    page = fixed.page(1)
    assert page.block("v").lines == ["क", want]
    assert page.block("t").rows == [[want]]
    assert page.block("e").lines == ["श\u200dृ"]
    assert page.block("i").labels[0].text == want
    full = pipeline.apply_corrections(master, [Correction(page=1, block_id="v", lines=[typed, "ग्ग्ं॑"])])
    assert full.page(1).block("v").lines == [want, "ꣳ॑"]


def test_a_todo_edit_typed_the_old_way_is_normalized_and_stays_checked(tmp_path):
    # The proofreader types ग्ं; the master gets ꣳ, and proof run again still knows the line is checked.
    from vedalipi.normalize import normalize_text

    src = tmp_path / "a.txt"
    src.write_text("ಅಹಿ॑ಗ್ಂ ನಮಃ\nಸೋಮಃ\n", encoding="utf-8")
    out = tmp_path / "out"
    assert main(["run", str(src), "--source-script", "kannada", "--out", str(out)]) == 0
    master = Master.load(out / "master.json")
    master.pages[0].blocks[0].flags = ["low_confidence"]
    master.save(out / "master.json")
    assert main(["proof", str(out)]) == 1
    todo = out / "corrections.todo.json"
    data = json.loads(todo.read_text(encoding="utf-8"))
    data["entries"][0]["text"], data["entries"][0]["checked"] = "अहि॒ग्ं नमः", True
    todo.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert main(["apply-corrections", str(out), str(todo)]) == 0
    lines = Master.load(out / "master.json").pages[0].blocks[0].lines
    assert lines[0] == normalize_text("अहि॒ग्ं नमः") == "अहि॒ꣳ नमः"
    # Line 2 is not checked yet, so the block keeps its flag and proof lists both lines again; line 1 keeps
    # the proofreader's check although the master now holds the normalized form of what was typed.
    assert main(["proof", str(out)]) == 1
    again = {e["line"]: e for e in json.loads(todo.read_text(encoding="utf-8"))["entries"]}
    assert again[1]["checked"] is True and again[2]["checked"] is False


def test_validate_change_does_not_replay_a_line_insertion_twice(book, tmp_path):
    # A set_lines edit that inserts a line is not idempotent: replaying it on the already corrected page
    # would insert the line again. A validate-only rebuild must start from the normalized page.
    out = tmp_path / "out"
    assert run(book, out) == 0
    before = list(Master.load(out / "master.json").page(3).blocks[0].lines)
    fix = tmp_path / "fix.json"
    fix.write_text(json.dumps([{"page": 3, "block_id": "b3-1", "set_lines": {"1": before[0] + "\nनमः"}}],
                              ensure_ascii=False), encoding="utf-8")
    assert main(["apply-corrections", str(out), str(fix)]) == 0
    corrected = list(Master.load(out / "master.json").page(3).blocks[0].lines)
    assert corrected == [before[0], "नमः", *before[1:]]
    # Adding a second read changes only validation (new flags); the text and its normalize are the same.
    second = tmp_path / "book2.kn.txt"
    shutil.copy(FIXTURES / "book2.kn.txt", second)
    assert main(["run", str(book), "--second-read", str(second), "--source-script", "kannada", "--script",
                 "telugu", "--out", str(out)]) == 0
    assert Master.load(out / "master.json").page(3).blocks[0].lines == corrected
