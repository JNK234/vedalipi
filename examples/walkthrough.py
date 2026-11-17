# ABOUTME: Walkthrough of the Kannada/Telugu fixes on fixed example lines and the real fixture reads, step by step.
# ABOUTME: Offline and deterministic: no OCR, no model calls. Run: uv run python examples/walkthrough.py [--keep DIR]

from __future__ import annotations

import argparse
import contextlib
import io
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path

from vedalipi import pipeline, transliterate
from vedalipi import verify as vf
from vedalipi.cli import main as cli
from vedalipi.normalize import normalize_page, normalize_text
from vedalipi.pipeline import RunConfig, to_master_form
from vedalipi.schema import Block, Correction, Master, Page
from vedalipi.validate import validate_page

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "kannada_ocr"

# The seeded examples: fixed lines, each built to show one change. Real OCR lines come from the fixtures.
EXAMPLES = {
    "latin_mark": ("kannada", "ಬಹು̱ಧಾ ವಿರೂ॑ಪಮ್"),  # p0064 read 1, line 6: U+0331 read for an anudatta
    "unknown_letter": ("telugu", "ౘను॒ నమః"),  # Telugu TSA, which the converter does not know
    "gum_doubled": ("kannada", "ಪರಿ॒ಷದ್ಯಗ್ಗ್ಂ॑ ಸ್ತು॒ವಂತಃ।"),  # doubled spelling of ꣳ, svara after it
    "gum_single": ("telugu", "అహి॑గ్ం"),  # single spelling, Telugu
    "ga_anusvara": ("kannada", "ಗಂ॑ತು"),  # p0005: ga + anusvara, not ꣳ
    "zwj": ("devanagari", "श\u200dृणु"),  # ZWJ between a letter and its vowel sign
    "mark_order": ("kannada", "ಶುಭಂ̱"),  # p0064 read 1, line 7: anusvara, then U+0331
}


def title(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


NAMES = {
    "\u0951": "svarita", "\u0952": "anudatta", "\u1cda": "double-svarita", "\ua8f3": "ꣳ",
    "\u200d": "ZWJ", "\u200c": "ZWNJ", "\u094d": "virama", "\u0ccd": "virama", "\u0c4d": "virama",
    "\u0902": "anusvara", "\u0c82": "anusvara", "\u0c02": "anusvara",
}


def marks(text: str, devanagari: bool = False) -> str:
    """The signs this walkthrough is about, in order, so invisible changes show: svaras, ꣳ, joiners, virama,
    anusvara, any Latin combining mark, and (in text meant to be Devanagari) any letter of another script."""
    out = []
    for ch in text:
        if ch in NAMES:
            out.append(NAMES[ch])
        elif 0x0300 <= ord(ch) <= 0x036F:
            out.append(f"U+{ord(ch):04X} {unicodedata.name(ch).lower()}")
        elif devanagari and 0x0980 <= ord(ch) <= 0x0DFF:
            out.append(f"{ch} U+{ord(ch):04X} {unicodedata.name(ch).lower()}")
    return ", ".join(out) or "-"


def step(label: str, text: str, devanagari: bool = False) -> None:
    print(f"  {label:<22} {text}")
    print(f"  {'':<22} [{marks(text, devanagari)}]")


def one_line_page(script: str, line: str) -> Page:
    return Page(page=1, blocks=[Block(id="b1", type="verse", lines=[line])])


def quiet(fn, *args, stderr: bool = False):
    """Run a CLI call, capturing what it prints; returns (exit code, output), or with `stderr`
    (exit code, output, messages)."""
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        code = fn(*args)
    return (code, buf_out.getvalue(), buf_err.getvalue()) if stderr else (code, buf_out.getvalue())


# ---------------------------------------------------------------- 1. conversion and text checks


def part1() -> None:
    title("1. Source script -> Devanagari master: stray characters are flagged, never dropped or replaced")
    for name in ("latin_mark", "unknown_letter"):
        script, line = EXAMPLES[name]
        print(f"\n[{name}] book printed in {script}")
        step("input (as read)", line)
        converted = transliterate.page_to_devanagari(one_line_page(script, line), script)
        step("converted", converted.blocks[0].lines[0], True)
        norm = normalize_page(converted)
        step("normalized (master)", norm.blocks[0].lines[0], True)
        report = validate_page(norm, None)
        print(f"  {'validate flags':<22} {report.block_flags}")
        for n in report.notes:
            print(f"  {'note':<22} {n}")


# ---------------------------------------------------------------- 2. normalize: ꣳ, joiners, mark order


def part2() -> None:
    title("2. Normalize: one ꣳ for ga+virama+anusvara, joiners removed, marks kept in reading order")
    for name in ("gum_doubled", "gum_single", "ga_anusvara", "zwj", "mark_order"):
        script, line = EXAMPLES[name]
        print(f"\n[{name}] book printed in {script}")
        step("input (as read)", line)
        page = one_line_page(script, line)
        converted = transliterate.page_to_devanagari(page, script) if script != "devanagari" else page
        step("converted", converted.blocks[0].lines[0], True)
        if name == "zwj":
            flags = validate_page(converted, None).block_flags
            print(f"  {'validate, before':<22} {flags}   <- the ZWJ makes a valid akshara look invalid")
        out = normalize_text(converted.blocks[0].lines[0])
        step("normalized (master)", out, True)
        if name == "zwj":
            print(f"  {'validate, after':<22} {validate_page(normalize_page(converted), None).block_flags}")
        if name in ("gum_doubled", "mark_order"):
            for target in ("telugu", "kannada"):
                step(f"output ({target})", transliterate.transliterate_text(out, target))
        if name == "mark_order":
            print(f"  {'dotted circle (te)':<22} {_dotted_circle(transliterate.transliterate_text(out, 'telugu'))}")
    print("\n  Real fixture reads: ꣳ in the master per file (expected.json)")
    import json

    expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))["files"]
    for name, want in sorted(expected.items()):
        page = to_master_form(Page.load(FIXTURES / name), "kannada")
        got = sum(line.count("\ua8f3") for b in page.blocks for line in b.lines)
        print(f"  {name:<20} ꣳ {got}  expected {want['expected_A8F3_after_normalize']}  "
              f"{'ok' if got == want['expected_A8F3_after_normalize'] else 'MISMATCH'}")


def _dotted_circle(text: str) -> str:
    """Whether the derived Telugu render font shapes `text` with a dotted circle (a mark without a base)."""
    try:
        import uharfbuzz as hb
    except ImportError:
        return "(uharfbuzz not installed)"
    from vedalipi.render import font_dirs

    font = hb.Font(hb.Face(hb.Blob.from_file_path(str(Path(font_dirs()[1]) / "VedalipiSansTelugu-Regular.ttf"))))
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf, {})
    return "yes" if font.get_nominal_glyph(0x25CC) in [g.codepoint for g in buf.glyph_infos] else "no"


# ---------------------------------------------------------------- 3. cross-read


def part3(work: Path) -> None:
    title("3. Cross-read: the second read goes through the same conversion before the compare")
    for name in ("p0005", "p0010"):
        first, second = (Page.load(FIXTURES / f"{name}.read{r}.json") for r in (1, 2))
        print(f"\n[{name}] read 1: {sum(len(b.lines) for b in first.blocks)} lines, "
              f"read 2: {sum(len(b.lines) for b in second.blocks)} lines (Kannada)")
        old = validate_page(to_master_form(first, "kannada"), None, second)
        print(f"  {'old (read 2 raw)':<22} reader_disagreement lines: "
              f"{sum('second read differs' in n for n in old.notes)}")
        cfg = RunConfig(input_path=FIXTURES / f"{name}.read1.json", out_dir=work / f"cross-{name}",
                        source_script="kannada", jobs=1)
        job = pipeline.PageJob(first.page, pipeline.page_dir(cfg.work_dir, first.page), cfg)
        job.page_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / f"{name}.read1.json", job.path(pipeline.OCR_JSON))
        shutil.copy(FIXTURES / f"{name}.read2.json", job.path(pipeline.OCR2_JSON))
        pipeline._normalize(job)
        pipeline._validate(job)
        report = pipeline.FlagReport.load(job.path(pipeline.FLAGS_JSON))
        print(f"  {'now (stages)':<22} flags: {report.block_flags}")
        for n in report.notes:
            if "second read differs" in n or "single_read" in n:
                print(f"    {n}")


# ---------------------------------------------------------------- 4. a whole book


def part4(work: Path) -> Path:
    title("4. A Kannada .txt book -> Devanagari master -> Telugu PDF: run, judge, report, verify")
    book = FIXTURES / "book.kn.txt"
    out = work / "t6"
    print(f"  input: {book.relative_to(ROOT)} (read 1 of p0005, p0010, p0064; one page each)")
    for cmd in (["run", str(book), "--source-script", "kannada", "--script", "telugu", "--out", str(out)],
                ["judge", str(out), "--script", "telugu"], ["report", str(out)], ["verify", str(out)]):
        code, text = quiet(cli, cmd)
        print(f"\n$ vedalipi {' '.join(cmd[:1] + [Path(c).name if '/' in c else c for c in cmd[1:]])}   (exit {code})")
        print("\n".join("  " + ln for ln in text.rstrip().splitlines()))
    master = Master.load(out / "master.json")
    line = master.page(3).blocks[0].lines[0]
    print("\n  One line through the book (page 3, line 1):")
    step("input (Kannada)", book.read_text(encoding="utf-8").split("\f")[2].splitlines()[0])
    step("master (Devanagari)", line, True)
    step("output (Telugu)", transliterate.transliterate_text(line, "telugu"))
    return out


# ---------------------------------------------------------------- 5. what verify catches


def part5(out: Path) -> None:
    title("5. What verify catches: a seeded fault, and a proofread page")
    print("\n[seeded fault] a converter that drops the double svarita on the way to Telugu")
    real = transliterate._convert
    transliterate._convert = lambda t, s, d: real(t, s, d).replace("\u1cda", "") if d == "telugu" else real(t, s, d)
    try:
        report = vf.verify(out, "telugu")
    finally:
        transliterate._convert = real
    print("\n".join("  " + ln for ln in vf.summary(report).splitlines()))

    print("\n[seeded fault] a Telugu stand-in for ꣳ that prints nothing. The round trip compares the")
    print("  raw conversion, which still has ꣳ, so only the source->output count can see this loss.")
    saved = transliterate.DEFAULT_A8F3["telugu"]
    transliterate.DEFAULT_A8F3["telugu"] = ""
    try:
        report = vf.verify(out, "telugu")
    finally:
        transliterate.DEFAULT_A8F3["telugu"] = saved
    print("\n".join("  " + ln for ln in vf.summary(report).splitlines()))

    print("\n[proofread] page 3: a proofreader replaces each Latin macron with the anudatta seen on the scan")
    fixed_dir = out.parent / "t6-proofread"
    shutil.copytree(out, fixed_dir)
    master = Master.load(fixed_dir / "master.json")
    block = master.page(3).blocks[0]
    lines = [ln.replace("\u0331", "\u0952") for ln in block.lines]
    fixed = pipeline.apply_corrections(master, [Correction(page=3, block_id=block.id, lines=lines, flags=[])])
    fixed.save(fixed_dir / "master.json")
    code, text = quiet(cli, ["verify", str(fixed_dir)])
    print(f"$ vedalipi verify t6-proofread   (exit {code})")
    print("\n".join("  " + ln for ln in text.rstrip().splitlines()))


# ---------------------------------------------------------------- 6. the proofreading cycle


def part6(work: Path) -> None:
    title("6. Proofreading cycle: run with a second read, verify, proof, edit, apply, verify, re-normalize")
    import json
    import re

    from vedalipi import normalize

    book, second, out = FIXTURES / "book.kn.txt", FIXTURES / "book2.kn.txt", work / "cycle"

    def show(cmd: list[str], top: int | None = None) -> None:
        code, text = quiet(cli, cmd)
        shown = [Path(c).name if "/" in c else c for c in cmd]
        print(f"\n$ vedalipi {' '.join(shown)}   (exit {code})")
        body = text.rstrip().splitlines()
        print("\n".join("  " + ln for ln in (body if top is None else body[:top])))
        if top is not None and len(body) > top:
            print(f"  ... ({len(body) - top} more lines)")

    show(["run", str(book), "--second-read", str(second), "--source-script", "kannada", "--script", "telugu",
          "--out", str(out)])
    show(["verify", str(out)])
    show(["proof", str(out), "--top", "2"])

    print("\n[edit] the proofreader (simulated here: there is no scan) checks pages 1 and 3 in corrections.todo.json:")
    print("  p1 line 1: क॑विं -> क॒विं (the printed page has क॒विं); p3: each Latin macron -> anudatta; p2 left for later")
    todo = out / "corrections.todo.json"
    data = json.loads(todo.read_text(encoding="utf-8"))
    for e in data["entries"]:
        if e["page"] in (1, 3):
            e["text"] = e["text"].replace("क॑विं", "क॒विं").replace("\u0331", "\u0952")
            e["checked"] = True
    todo.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for e in data["entries"]:
        if e["text"] != e["master"] and e["page"] == 1:
            step(f"{e['id']} was", e["master"])
            step(f"{e['id']} now", e["text"])

    show(["apply-corrections", str(out), str(todo)])
    show(["verify", str(out)])
    show(["proof", str(out), "--top", "1"])

    print("\n[re-normalize] a newer normalize (version 4) that also drops the comma OCR reads after a final")
    print("  consonant: same OCR text, a different normalized page 3. The corrections are applied again.")
    real_fn, real_version = normalize.normalize_text, normalize.NORMALIZE_VERSION
    normalize.normalize_text = lambda t: re.sub(" {2,}", " ", real_fn(t).replace("्,", "्"))
    normalize.NORMALIZE_VERSION = "4"
    try:
        code, _, err = quiet(cli, ["run", str(book), "--second-read", str(second), "--source-script", "kannada",
                                   "--script", "telugu", "--out", str(out)], stderr=True)
        print(f"$ vedalipi run ... (normalize version 4)   (exit {code})")
        print("\n".join("  " + ln for ln in err.strip().splitlines()))
        master = Master.load(out / "master.json")
        step("p1 line 1 (edited)", master.page(1).blocks[0].lines[0], True)
        step("p3 line 13 (unedited)", master.page(3).blocks[0].lines[12], True)
        print(f"  {'corrections recorded':<22} {len(master.corrections)}")
        show(["verify", str(out)])
    finally:
        normalize.normalize_text, normalize.NORMALIZE_VERSION = real_fn, real_version


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep", metavar="DIR", help="write the outputs (PDF, master.json, work/) here and keep them")
    a = ap.parse_args(argv)
    if a.keep:
        work = Path(a.keep).resolve()
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        ctx = contextlib.nullcontext(work)
    else:
        ctx = tempfile.TemporaryDirectory(prefix="vedalipi-walkthrough-")
    with ctx as w:
        work = Path(w)
        part1()
        part2()
        part3(work)
        out = part4(work)
        part5(out)
        part6(work)
        if a.keep:
            print(f"\noutputs kept in {work} (PDF: {out / 'book.kn.telugu.pdf'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
