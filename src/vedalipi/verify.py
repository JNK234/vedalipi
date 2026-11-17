# ABOUTME: `vedalipi verify`: one PASS or CHECK per page, with reasons, from the master's flags and fresh text checks.
# ABOUTME: Includes a source -> output svara count (॑ ॒ ᳚ and ꣳ) per Sanskrit line. Read-only: writes nothing.

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import transliterate
from .normalize import A8F3
from .pipeline import NORM_JSON, OCR_JSON, page_dir
from .schema import (
    FLAG_INVALID_SEQUENCE,
    FLAG_LOW_CONFIDENCE,
    FLAG_NON_VEDIC_MARK,
    FLAG_READER_DISAGREEMENT,
    FLAG_ROUNDTRIP_FAIL,
    FLAG_SINGLE_READ,
    FLAG_SVARA_COUNT_MISMATCH,
    FLAG_UNCONVERTED_LETTER,
    Manifest,
    Master,
    Page,
)
from .validate import page_strays

DEVA = transliterate.DEVA
OUTPUT_SVARA_MISMATCH = "output_svara_mismatch"
STAGE_FAILED = "stage_failed"

# Master flags that make a page CHECK. A proofreader who has checked a block against the scan clears them
# with a correction. low_confidence is one: the OCR model itself was unsure there.
CHECK_FLAGS = (
    FLAG_NON_VEDIC_MARK, FLAG_UNCONVERTED_LETTER, FLAG_READER_DISAGREEMENT, FLAG_SINGLE_READ,
    FLAG_INVALID_SEQUENCE, FLAG_SVARA_COUNT_MISMATCH, FLAG_LOW_CONFIDENCE,
)
# Reasons in the order they are printed.
REASONS = (*CHECK_FLAGS, FLAG_ROUNDTRIP_FAIL, OUTPUT_SVARA_MISMATCH, STAGE_FAILED)

# What the svara check counts, in this order: svarita, anudatta, double svarita, and ꣳ.
MARKS = ("\u0951", "\u0952", "\u1cda")
LABELS = ("॑", "॒", "᳚", "ꣳ")
Counts = tuple[int, int, int, int]
_JOINERS = str.maketrans("", "", "\u200c\u200d")


@dataclass
class PageResult:
    page: int
    reasons: dict[str, list[str]] = field(default_factory=dict)  # reason -> details (blocks, lines)
    source: Counts = (0, 0, 0, 0)
    output: Counts = (0, 0, 0, 0)
    source_from: str = ""  # "source text", "master", "master (corrected)", or a mix

    @property
    def status(self) -> str:
        return "CHECK" if self.reasons else "PASS"

    def add(self, reason: str, detail: str) -> None:
        items = self.reasons.setdefault(reason, [])
        if detail not in items:
            items.append(detail)


@dataclass
class VerifyReport:
    out_dir: Path
    script: str
    source_script: str
    pages: list[PageResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(p.status == "PASS" for p in self.pages)


# ---------------------------------------------------------------- counting


def _gum_pattern(script: str) -> re.Pattern:
    """Every way `script` writes ꣳ: the sign itself, the spellings of ग्ग्ं and ग्ं, and the render stand-in.
    Longest first, so the doubled spelling counts once."""
    forms = {A8F3, transliterate._convert("ग्ग्ं", DEVA, script), transliterate._convert("ग्ं", DEVA, script)}
    if transliterate.DEFAULT_A8F3.get(script):
        forms.add(transliterate.DEFAULT_A8F3[script])
    # A reader can put a svara between the virama and the anusvara (ग्॑ं); normalize still makes that one ꣳ.
    svaras = "[" + "".join(MARKS) + "]*"
    return re.compile("|".join(re.escape(f[:-1]) + svaras + re.escape(f[-1]) if len(f) > 1 else re.escape(f)
                               for f in sorted(forms, key=len, reverse=True)))


def count(text: str, script: str, *, output: bool = False) -> Counts:
    """(॑, ॒, ᳚, ꣳ) in one string written in `script`. A roman output is read back to Devanagari first: its
    svaras are Latin marks, and the same Latin mark can also be part of a letter. Roman scripts write ꣳ and
    candrabindu alike, so ꣳ is not counted there (-1)."""
    text = text.translate(_JOINERS)
    if transliterate.is_roman(script):
        deva = transliterate.roundtrip_back(text, script) if output else transliterate._convert(text, script, DEVA)
        return (*(deva.count(m) for m in MARKS), -1)
    return (*(text.count(m) for m in MARKS), len(_gum_pattern(script).findall(text)))


def _strings(page: Page) -> dict[tuple, str]:
    """Every Sanskrit string of a page by position: block lines, table cells and image labels."""
    out: dict[tuple, str] = {}
    for b in page.blocks:
        if b.is_sanskrit:
            for i, line in enumerate(b.lines, 1):
                out[(b.id, f"line {i}")] = line
            for r, row in enumerate(b.rows or [], 1):
                for c, cell in enumerate(row, 1):
                    out[(b.id, f"row {r} cell {c}")] = cell
        for k, label in enumerate(b.labels or [], 1):
            if label.is_sanskrit:
                out[(b.id, f"label {k}")] = label.text
    return out


def _load(path: Path) -> Page | None:
    try:
        return Page.load(path) if path.exists() else None
    except (ValueError, OSError):
        return None


def _sources(master_page: Page, work: Path, source_script: str) -> tuple[dict[tuple, tuple[str, str]], str]:
    """The text each Sanskrit string of the page was made from, with its script.

    A block still as the OCR/text input gave it (its master text equals norm.json) is counted from ocr.json,
    in the book's own script. A block corrected by hand, or a page without its work files, is counted from
    the master: the correction is the text the book should have."""
    d = page_dir(work, master_page.page)
    ocr, norm = _load(d / OCR_JSON), _load(d / NORM_JSON)
    have = _strings(master_page)
    ocr_s, norm_s = (_strings(ocr) if ocr else {}), (_strings(norm) if norm else {})
    out: dict[tuple, tuple[str, str]] = {}
    kinds: set[str] = set()
    for bid in dict.fromkeys(k[0] for k in have):
        keys = [k for k in have if k[0] == bid]
        as_read = all(k in ocr_s and norm_s.get(k) == have[k] for k in keys) and all(
            k in have for k in norm_s if k[0] == bid)
        for k in keys:
            out[k] = (ocr_s[k], source_script) if as_read else (have[k], DEVA)
        kinds.add("source text" if as_read else ("master" if ocr is None or norm is None else "master (corrected)"))
    return out, " + ".join(sorted(kinds)) or "no Sanskrit text"


# ---------------------------------------------------------------- the check


def _default_script(out_dir: Path) -> str:
    """The script rendered last (its transliterate-<script>.json is newest), else kannada."""
    done = sorted((Path(out_dir) / "work").glob("transliterate-*.json"), key=lambda p: p.stat().st_mtime)
    return done[-1].stem.removeprefix("transliterate-") if done else "kannada"


def verify(out_dir: Path, script: str | None = None) -> VerifyReport:
    """Check every page of DIR/master.json for `script` without writing anything.

    A page is CHECK when a block carries one of CHECK_FLAGS; when its text holds a non-Vedic mark or an
    unconverted letter (whatever its flags say: it would print); when a Sanskrit string fails the round
    trip; when a Sanskrit string's count of ॑ ॒ ᳚ or ꣳ differs between its source and the target output;
    or when one of its stages failed. Otherwise it is PASS.
    """
    out_dir = Path(out_dir)
    script = script or _default_script(out_dir)
    master = Master.load(out_dir / "master.json")
    work = out_dir / "work"
    mpath = work / "manifest.json"
    manifest = Manifest.load(mpath) if mpath.exists() else None
    source_script = manifest.source_script if manifest else DEVA
    report = VerifyReport(out_dir, script, source_script)

    diagnostics: list[dict] = []
    target = transliterate.transliterate_master(master, script, diagnostics=diagnostics)
    failed_rt = {(d["page"], d["block"]) for d in diagnostics}

    for mp, tp in zip(master.pages, target.pages):
        res = PageResult(mp.page)
        for b in mp.blocks:
            for f in CHECK_FLAGS:
                if f in b.flags:
                    res.add(f, b.id)
            if (mp.page, b.id) in failed_rt:
                res.add(FLAG_ROUNDTRIP_FAIL, b.id)
        for stray in page_strays(mp):
            res.add(stray.flag, stray.block)

        sources, res.source_from = _sources(mp, work, source_script)
        outputs = _strings(tp)
        src_total, out_total = [0, 0, 0, 0], [0, 0, 0, 0]
        for key, (text, text_script) in sources.items():
            s, o = count(text, text_script), count(outputs.get(key, ""), script, output=True)
            if o[3] < 0:  # a roman output: ꣳ is not counted, so it is not compared either
                s = (*s[:3], -1)
            for i in range(4):
                src_total[i] += s[i]
                out_total[i] += o[i]
            diff = [f"{LABELS[i]} {s[i]} -> {o[i]}" for i in range(4) if s[i] != o[i]]
            if diff:
                res.add(OUTPUT_SVARA_MISMATCH, f"{key[0]} {key[1]}: {', '.join(diff)}")
        res.source, res.output = tuple(src_total), tuple(out_total)
        report.pages.append(res)

    if manifest:
        in_master = {p.page for p in master.pages}
        for page, stages in sorted(manifest.pages.items()):
            bad = [f"{name}: {rec.error}" for name, rec in stages.items() if rec.status == "failed"]
            if bad:
                res = next((r for r in report.pages if r.page == page), None)
                if res is None:
                    res = PageResult(page, source_from="not in master")
                    report.pages.append(res)
                for b in bad:
                    res.add(STAGE_FAILED, b)
                if page not in in_master:
                    res.add(STAGE_FAILED, "page is not in master.json")
        report.pages.sort(key=lambda r: r.page)
    return report


def _counts_text(res: PageResult) -> str:
    parts = []
    for i, label in enumerate(LABELS):
        if res.source[i] < 0:
            parts.append(f"{label} -")
        else:
            parts.append(f"{label} {res.source[i]}->{res.output[i]}")
    return "  ".join(parts)


def summary(report: VerifyReport) -> str:
    """Human-readable verdict: one line per page with its svara counts (source->output), then its reasons."""
    lines = [f"verify {report.out_dir} ({report.script}; book printed in {report.source_script})",
             "svara counts per page are source->output for ॑ ॒ ᳚ ꣳ"]
    for res in report.pages:
        lines.append(f"{f'p{res.page}':<5} {res.status:5}  {_counts_text(res)}  [{res.source_from}]")
        for reason in REASONS:
            if reason in res.reasons:
                items = res.reasons[reason]
                shown = ", ".join(items[:6]) + (f", ... ({len(items)} in all)" if len(items) > 6 else "")
                lines.append(f"    {reason}: {shown}")
    n_check = sum(r.status == "CHECK" for r in report.pages)
    lines.append(f"{len(report.pages)} pages: {len(report.pages) - n_check} PASS, {n_check} CHECK")
    if n_check:
        lines.append(f"details: vedalipi report {report.out_dir}")
    return "\n".join(lines)
