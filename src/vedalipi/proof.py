# ABOUTME: `vedalipi proof`: every line that needs a check, most svara differences first, with both reads side by side,
# ABOUTME: and corrections.todo.json (one editable entry per line) that `vedalipi apply-corrections` turns into corrections.

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import transliterate
from . import verify as vf
from .normalize import normalize_text
from .pipeline import FLAGS_JSON, OCR2_JSON, OCR_JSON, page_dir, to_master_form
from .schema import (
    FLAG_INVALID_SEQUENCE,
    FLAG_NON_VEDIC_MARK,
    FLAG_READER_DISAGREEMENT,
    FLAG_ROUNDTRIP_FAIL,
    FLAG_SINGLE_READ,
    FLAG_SVARA_COUNT_MISMATCH,
    FLAG_UNCONVERTED_LETTER,
    Correction,
    FlagReport,
    Master,
    Page,
    atomic_write_text,
)
from .validate import compare_reads, page_strays

TODO_NAME = "corrections.todo.json"
ABOUT = (
    "vedalipi proof: one entry per line to check, most svara differences first. Check each line against the "
    "scan. Fix `text` (the Devanagari master line) if it is wrong; to add a line the reader missed, put it after "
    "a newline in `text`; an empty `text` removes the line. Set `checked` to true for every line you have "
    "checked, changed or not. Then run: vedalipi apply-corrections DIR corrections.todo.json. A block loses its "
    "check flags once every listed line of it is checked. Run vedalipi proof DIR again for what is left."
)

# Svara marks, and marks read in their place (a Latin macron below for an anudatta): what a svara
# difference between two reads is made of.
_SVARAS = {"\u0951", "\u0952", "\u1cda"}


def _is_svara(ch: str) -> bool:
    return ch in _SVARAS or 0x0300 <= ord(ch) <= 0x036F


@dataclass
class Item:
    """One line to check."""

    page: int
    block_id: str
    line: int
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    master: str = ""  # the line in the master now (Devanagari)
    as_read: str | None = None  # the line as the first reader gave it, in the book's own script
    second_read: str | None = None  # the second read's line paired with it (Devanagari)
    svaras: list[str] = field(default_factory=list)  # words whose svaras differ: "first / second"
    letters: list[str] = field(default_factory=list)  # words whose letters differ
    svara_differences: int = 0
    order: tuple = ()  # reading order: page, block index, line

    @property
    def id(self) -> str:
        return f"p{self.page}:{self.block_id}:{self.line}"

    def add(self, reason: str, note: str = "") -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        if note and note not in self.notes:
            self.notes.append(note)


@dataclass
class ProofReport:
    out_dir: Path
    script: str
    items: list[Item] = field(default_factory=list)
    pages: dict[int, list[str]] = field(default_factory=dict)  # page-level problems (a failed stage)


# ---------------------------------------------------------------- svara differences between two reads


def _aksharas(word: str) -> list[tuple[str, str]]:
    """(letters, svaras) per base character: a svara belongs to the letter it follows."""
    out: list[list[str]] = []
    for ch in word:
        if out and _is_svara(ch):
            out[-1][1] += ch
        elif out and unicodedata.combining(ch):
            out[-1][0] += ch
        else:
            out.append([ch, ""])
    return [(a, b) for a, b in out]


def _letters(word: str) -> str:
    return "".join(ch for ch in word if not _is_svara(ch))


def svara_diff(first: str, second: str) -> tuple[int, list[str], list[str]]:
    """(aksharas whose svaras differ, words with svara differences, words with letter differences).

    Words are lined up by their letters; in two words with the same letters, each akshara whose svaras
    differ counts once (a mark on the wrong akshara counts twice: missing on one, extra on another)."""
    wa, wb = first.split(), second.split()
    la, lb = [_letters(w) for w in wa], [_letters(w) for w in wb]
    count, svaras, letters = 0, [], []
    for op, i0, i1, j0, j1 in difflib.SequenceMatcher(None, la, lb, autojunk=False).get_opcodes():
        if op == "equal":
            for a, b in zip(wa[i0:i1], wb[j0:j1]):
                if a != b:
                    n = sum(x[1] != y[1] for x, y in zip(_aksharas(a), _aksharas(b)))
                    if n:
                        count += n
                        svaras.append(f"{a} / {b}")
        else:
            letters.append(f"{' '.join(wa[i0:i1]) or '∅'} / {' '.join(wb[j0:j1]) or '∅'}")
    return count, svaras, letters


# ---------------------------------------------------------------- finding the lines


_NOTE_LINE = re.compile(r"^(\S+) line (\d+): (.*)$")
_NOTE_AROUND = re.compile(r"^(\S+) (after|before) line (\d+): single_read: only the second read has: (.*)$")
_IMAGE = re.compile(r"text above/below (\d+)/(\d+) != image (\d+)/(\d+)")


def _note_flag(rest: str) -> str | None:
    """The flag a validate note (after "bX line N: ") is about."""
    if rest.startswith("second read differs"):
        return FLAG_READER_DISAGREEMENT
    if rest.startswith("single_read"):
        return FLAG_SINGLE_READ
    if rest.startswith("invalid sign order"):
        return FLAG_INVALID_SEQUENCE
    if rest.startswith("text above/below") or rest.startswith("orphan combining mark"):
        return FLAG_SVARA_COUNT_MISMATCH
    for f in (FLAG_NON_VEDIC_MARK, FLAG_UNCONVERTED_LETTER):
        if rest.startswith(f):
            return f
    return None


def _load(path: Path) -> Page | None:
    try:
        return Page.load(path) if path.exists() else None
    except (ValueError, OSError):
        return None


def _page_items(mp: Page, tp: Page, work: Path, source_script: str, vres: vf.PageResult,
                roundtrip: set[tuple[str, str]]) -> list[Item]:
    d = page_dir(work, mp.page)
    ocr, second = _load(d / OCR_JSON), _load(d / OCR2_JSON)
    try:
        notes = FlagReport.load(d / FLAGS_JSON).notes if (d / FLAGS_JSON).exists() else []
    except (ValueError, OSError):
        notes = []
    pairs = compare_reads(mp, to_master_form(second, source_script)).pairs if second else {}
    items: dict[tuple[str, int], Item] = {}
    blocks = {b.id: b for b in mp.blocks if b.is_sanskrit and b.lines}
    index = {bid: k for k, bid in enumerate(blocks)}

    def item(bid: str, n: int) -> Item | None:
        b = blocks.get(bid)
        if b is None or not 1 <= n <= len(b.lines):
            return None
        if (bid, n) not in items:
            items[(bid, n)] = Item(mp.page, bid, n, master=b.lines[n - 1], order=(mp.page, index[bid], n))
        return items[(bid, n)]

    # Validator notes name the line; a flag the proofreader cleared is no longer a reason.
    image_diff: dict[tuple[str, int], int] = {}
    for note in notes:
        m = _NOTE_AROUND.match(note)
        if m:
            bid, where, n, text = m[1], m[2], int(m[3]), m[4]
            b = blocks.get(bid)
            it = item(bid, n) if b is not None and FLAG_SINGLE_READ in b.flags else None
            if it:
                it.add(FLAG_SINGLE_READ, f"the second read has a line {where} this one: {text}")
            continue
        m = _NOTE_LINE.match(note)
        if not m:
            continue
        bid, n, rest = m[1], int(m[2]), m[3]
        flag = _note_flag(rest)
        if flag is None or bid not in blocks or flag not in blocks[bid].flags:
            continue
        it = item(bid, n)
        if it:
            it.add(flag, "" if flag == FLAG_READER_DISAGREEMENT else rest)
            g = _IMAGE.search(rest)
            if g:
                a, b, c, e = map(int, g.groups())
                image_diff[(bid, n)] = abs(a - c) + abs(b - e)
    # What the text itself holds, whatever the flags say.
    for stray in page_strays(mp):
        if stray.where.startswith("line "):
            it = item(stray.block, int(stray.where.split()[1]))
            if it:
                it.add(stray.flag, stray.note().split(": ", 1)[1])
    for detail in vres.reasons.get(vf.OUTPUT_SVARA_MISMATCH, []):
        m = re.match(r"^(\S+) line (\d+): (.*)$", detail)
        if m and (it := item(m[1], int(m[2]))):
            it.add(vf.OUTPUT_SVARA_MISMATCH, f"source -> output: {m[3]}")
    for bid, b in blocks.items():
        for n, line in enumerate(b.lines, 1):
            if (bid, line) in roundtrip:
                item(bid, n).add(FLAG_ROUNDTRIP_FAIL, "does not convert back to the same Devanagari")
    # A flag no note placed on a line (low_confidence: the model was unsure somewhere in the block) puts
    # every line of its block up for checking.
    for bid, b in blocks.items():
        for flag in b.flags:
            if flag in vf.CHECK_FLAGS and not any(flag in it.reasons for (k, _), it in items.items() if k == bid):
                for n in range(1, len(b.lines) + 1):
                    item(bid, n).add(flag)
    # Both reads side by side, and the line as printed in the book's script.
    for (bid, n), it in items.items():
        ocr_block = next((b for b in (ocr.blocks if ocr else []) if b.id == bid), None)
        if ocr_block is not None and len(ocr_block.lines) == len(blocks[bid].lines):
            it.as_read = ocr_block.lines[n - 1]
        if (bid, n) in pairs:
            it.second_read = pairs[(bid, n)]
            it.svara_differences, it.svaras, it.letters = svara_diff(it.master, it.second_read)
        it.svara_differences += image_diff.get((bid, n), 0)
    return list(items.values())


def proof(out_dir: Path, script: str | None = None) -> ProofReport:
    """Every line that needs a check, most svara differences first. Reads the output folder only."""
    out_dir = Path(out_dir)
    vres = vf.verify(out_dir, script)
    master = Master.load(out_dir / "master.json")
    diagnostics: list[dict] = []
    target = transliterate.transliterate_master(master, vres.script, diagnostics=diagnostics)
    roundtrip = {(d["block"], d["text"]) for d in diagnostics}
    by_page = {r.page: r for r in vres.pages}
    report = ProofReport(out_dir, vres.script)
    for mp, tp in zip(master.pages, target.pages):
        r = by_page.get(mp.page, vf.PageResult(mp.page))
        report.items += _page_items(mp, tp, out_dir / "work", vres.source_script, r, roundtrip)
    for r in vres.pages:
        if vf.STAGE_FAILED in r.reasons:
            report.pages[r.page] = list(r.reasons[vf.STAGE_FAILED])
    # Flagged blocks without lines (tables, image labels) cannot be edited line by line in the todo file.
    for mp in master.pages:
        for b in mp.blocks:
            flags = [f for f in b.flags if f in vf.CHECK_FLAGS]
            if flags and not b.lines:
                report.pages.setdefault(mp.page, []).append(
                    f"{b.id} ({b.type}): {', '.join(flags)}; not in the todo file: correct it with rows or flags "
                    f"in a corrections file")
    report.items.sort(key=lambda i: (-i.svara_differences, -len(i.letters), -len(i.reasons), i.order))
    return report


# ---------------------------------------------------------------- corrections.todo.json


def _norm(text: str | None) -> str | None:
    """Normalized text, as apply-corrections stores it; each line of a multi-line edit on its own."""
    return None if text is None else "\n".join(normalize_text(t) for t in text.split("\n"))


def _entry(rank: int, it: Item) -> dict:
    return {
        "id": it.id, "rank": rank, "page": it.page, "block_id": it.block_id, "line": it.line,
        "reasons": it.reasons, "svara_differences": it.svara_differences, "svaras": it.svaras,
        "letters": it.letters, "notes": it.notes, "as_read": it.as_read, "master": it.master,
        "second_read": it.second_read, "text": it.master, "checked": False,
    }


def write_todo(report: ProofReport) -> tuple[Path, dict[str, int]]:
    """Write or update OUT/corrections.todo.json. A line already in the file keeps what the proofreader
    wrote (text, checked) while it is still the same line; lines no longer needing a check are removed."""
    path = report.out_dir / TODO_NAME
    old: dict[str, dict] = {}
    if path.exists():
        try:
            old = {e["id"]: e for e in json.loads(path.read_text(encoding="utf-8")).get("entries", [])}
        except (ValueError, KeyError, TypeError):
            old = {}
    entries, kept = [], 0
    for rank, it in enumerate(report.items, 1):
        e = _entry(rank, it)
        prev = old.get(it.id)
        touched = prev is not None and (prev.get("checked") or prev.get("text") != prev.get("master"))
        # After apply-corrections the master holds the normalized form of what was typed.
        if touched and it.master in (prev.get("master"), prev.get("text"), _norm(prev.get("text"))):
            e["text"], e["checked"] = prev["text"], bool(prev.get("checked"))
            kept += 1
        entries.append(e)
    new_ids = {e["id"] for e in entries}
    stats = {"entries": len(entries), "new": sum(e["id"] not in old for e in entries), "kept": kept,
             "resolved": sum(i not in new_ids for i in old)}
    atomic_write_text(path, json.dumps({"about": ABOUT, "entries": entries}, ensure_ascii=False, indent=1) + "\n")
    return path, stats


def todo_corrections(master: Master, data: dict) -> tuple[list[Correction], list[str]]:
    """Corrections from a corrections.todo.json: each checked line whose text changed becomes a line edit,
    and a block whose listed lines are all checked loses its check flags. Returns (corrections, messages)."""
    groups: dict[tuple[int, str], list[dict]] = {}
    for e in data.get("entries", []):
        groups.setdefault((int(e["page"]), str(e["block_id"])), []).append(e)
    corrections, messages = [], []
    for (page, bid), es in groups.items():
        try:
            block = master.page(page).block(bid)
        except KeyError:
            messages.append(f"p{page} {bid}: no such block in master.json; skipped (run vedalipi proof again)")
            continue
        checked = [e for e in es if e.get("checked")]
        for e in es:
            if not e.get("checked") and e.get("text") != e.get("master"):
                messages.append(f"p{page} {bid} line {e['line']}: edited but not checked, not applied")
        set_lines, stale = {}, False
        for e in checked:
            n = int(e["line"])
            now = block.lines[n - 1] if 1 <= n <= len(block.lines) else None
            if now not in (e.get("master"), e.get("text"), _norm(e.get("text"))):
                messages.append(f"p{page} {bid} line {n}: changed since vedalipi proof; skipped (run it again)")
                stale = True
            elif _norm(e["text"]) != now:
                set_lines[n] = e["text"]
        done = bool(checked) and len(checked) == len(es) and not stale
        clear = [f for f in block.flags if f in vf.CHECK_FLAGS] if done else []
        if set_lines or clear:
            corrections.append(Correction(page=page, block_id=bid, set_lines=set_lines or None,
                                          clear_flags=clear or None))
        if checked:
            what = f"flags cleared: {', '.join(clear)}" if clear else (
                "no check flags" if done else "flags stay until every listed line is checked")
            messages.append(f"p{page} {bid}: {len(checked)} of {len(es)} listed lines checked, "
                            f"{len(set_lines)} edited; {what}")
    return corrections, messages


# ---------------------------------------------------------------- printing


def summary(report: ProofReport, stats: dict[str, int] | None = None, top: int | None = None) -> str:
    items = report.items if top is None else report.items[:top]
    pages = sorted({i.page for i in report.items})
    lines = [f"proof {report.out_dir} ({report.script}): {len(report.items)} lines to check on "
             f"{len(pages)} pages, most svara differences first"]
    for rank, it in enumerate(items, 1):
        lines.append(f"#{rank:<3} p{it.page} {it.block_id} line {it.line}   svara differences: {it.svara_differences}"
                     f"   reasons: {', '.join(it.reasons)}")
        if it.as_read is not None and it.as_read != it.master:
            lines.append(f"     as read : {it.as_read}")
        lines.append(f"     master  : {it.master}")
        if it.second_read is not None:
            lines.append(f"     second  : {it.second_read}")
        if it.svaras:
            lines.append(f"     svaras  : {'; '.join(it.svaras)}")
        if it.letters:
            lines.append(f"     letters : {'; '.join(it.letters)}")
        for note in it.notes:
            lines.append(f"     note    : {note}")
    if top is not None and len(report.items) > top:
        lines.append(f"... {len(report.items) - top} more lines in {TODO_NAME}")
    for page, why in sorted(report.pages.items()):
        lines.append(f"p{page}: {'; '.join(why)}")
    if stats is not None:
        lines.append(f"wrote {report.out_dir / TODO_NAME}: {stats['entries']} entries ({stats['new']} new, "
                     f"{stats['kept']} kept from the last proof, {stats['resolved']} resolved)")
    return "\n".join(lines)
