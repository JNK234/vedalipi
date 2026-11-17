# ABOUTME: Resumable stage runner: per-page stages registered by name, cached under <out>/work/pages/pNNNN/.
# ABOUTME: Skips a stage when its outputs exist and its input hash is unchanged; builds master.json and renders scripts.

from __future__ import annotations

import fcntl
import hashlib
import sys
import threading
import traceback
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .schema import (
    VALIDATOR_FLAGS,
    Correction,
    FlagReport,
    Manifest,
    Master,
    Page,
    PageOrigin,
    StageRecord,
    atomic_write_text,
)

SOURCE_PNG = "source.png"
PRE_PNG = "pre.png"
OCR_JSON = "ocr.json"
OCR2_JSON = "ocr2.json"  # independent second read (--cross-read)
NORM_JSON = "norm.json"
FLAGS_JSON = "flags.json"
DEVA = "devanagari"  # the master's script


@dataclass
class RunConfig:
    input_path: Path
    out_dir: Path
    script: str = "kannada"
    pages: str | None = None
    jobs: int = 4
    backend: str = "claude-cli"
    model: str | None = "claude-opus-5-5"
    force_stages: frozenset[str] = frozenset()
    # Reuse these stages' existing outputs even though their settings changed (e.g. keep OCR read with an
    # older prompt instead of paying to re-read the book). Their outputs must exist and have been done.
    keep_stages: frozenset[str] = frozenset()
    dpi: int = 300
    upscale: float = 1.5
    side_by_side: bool = False
    cross_read: bool = False  # read every page twice and flag lines the reads disagree on
    # Script the book is printed in. Its Sanskrit is converted to Devanagari at normalize, so the master
    # stays Devanagari whatever the source; other-language text keeps its own script.
    source_script: str = "devanagari"
    # Languages printed in the book (ISO 639-1); empty means Sanskrit only. Tells OCR how to tag blocks.
    languages: tuple[str, ...] = ()
    # For .txt/.json input: an independent second reading of the same book, compared like --cross-read.
    second_read: Path | None = None

    @property
    def work_dir(self) -> Path:
        return self.out_dir / "work"

    @property
    def manifest_path(self) -> Path:
        return self.work_dir / "manifest.json"

    @property
    def master_path(self) -> Path:
        return self.out_dir / "master.json"


@dataclass
class PageJob:
    """What a stage function sees: one page, its cache directory, the run config and shared run state."""

    page: int
    page_dir: Path
    cfg: RunConfig
    shared: dict = field(default_factory=dict)

    def path(self, name: str) -> Path:
        return self.page_dir / name


# A stage function does the work for one page and returns its cost in USD (or None for free stages).
StageFn = Callable[[PageJob], float | None]


@dataclass
class Stage:
    name: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    run: StageFn
    # Extra material that should invalidate the cache (config values, backend fingerprint, source doc hash).
    fingerprint: Callable[[PageJob], str] = lambda job: ""


def page_dir(work_dir: Path, page: int) -> Path:
    return work_dir / "pages" / f"p{page:04d}"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def sha256_input(path: Path) -> str:
    """Hash a file, or every file (name + bytes, sorted) in a directory."""
    path = Path(path)
    if path.is_file():
        return sha256_file(path)
    h = hashlib.sha256()
    for p in sorted(q for q in path.rglob("*") if q.is_file()):
        h.update(str(p.relative_to(path)).encode())
        h.update(sha256_file(p).encode())
    return h.hexdigest()


def stage_input_hash(stage: Stage, job: PageJob) -> str:
    h = hashlib.sha256()
    h.update(stage.name.encode())
    h.update(stage.fingerprint(job).encode())
    for name in stage.inputs:
        h.update(name.encode())
        h.update(sha256_file(job.path(name)).encode())
    return h.hexdigest()


def parse_pages(spec: str | None, num_pages: int) -> list[int]:
    """Parse "1-5,8,10-" into sorted 1-based page numbers within 1..num_pages."""
    if not spec:
        return list(range(1, num_pages + 1))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            start = int(lo) if lo else 1
            end = int(hi) if hi else num_pages
        else:
            start = end = int(part)
        if start < 1 or end > num_pages or start > end:
            raise ValueError(f"page range {part!r} outside 1-{num_pages}")
        pages.update(range(start, end + 1))
    return sorted(pages)


class StageRunner:
    """Runs a chain of per-page stages over selected pages with bounded concurrency.

    Each page flows through the whole chain in one worker, so at most `jobs` pages are in memory.
    The manifest is saved after every stage so an interrupted run resumes where it stopped.
    """

    def __init__(self, stages: list[Stage], manifest: Manifest, cfg: RunConfig, shared: dict | None = None):
        names = [s.name for s in stages]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate stage names: {names}")
        unknown = set(cfg.force_stages) - set(names)
        if unknown:
            raise ValueError(f"--force-stage {sorted(unknown)} not in {names}")
        unknown = set(cfg.keep_stages) - set(names)
        if unknown:
            raise ValueError(f"--keep-stage {sorted(unknown)} not in {names}")
        if cfg.force_stages & cfg.keep_stages:
            raise ValueError(f"a stage cannot be both forced and kept: {sorted(cfg.force_stages & cfg.keep_stages)}")
        self.stages = stages
        self.manifest = manifest
        self.cfg = cfg
        self.shared = shared if shared is not None else {}
        self._lock = threading.Lock()
        self.ran: list[tuple[int, str]] = []
        self.skipped: list[tuple[int, str]] = []
        self.failed: list[tuple[int, str, str]] = []
        self.completed: list[int] = []  # pages whose whole stage chain is done (ran or cached)

    def _save(self) -> None:
        self.manifest.save(self.cfg.manifest_path)

    def run_page(self, page: int) -> bool:
        job = PageJob(page, page_dir(self.cfg.work_dir, page), self.cfg, self.shared)
        job.page_dir.mkdir(parents=True, exist_ok=True)
        for stage in self.stages:
            missing = [n for n in stage.inputs if not job.path(n).exists()]
            if missing:
                self._fail(page, stage, f"missing inputs {missing}")
                return False
            h = stage_input_hash(stage, job)
            with self._lock:
                rec = self.manifest.record(page, stage.name)
            cached = (
                rec is not None
                and rec.status == "done"
                and (rec.input_hash == h or stage.name in self.cfg.keep_stages)
                and all(job.path(n).exists() for n in stage.outputs)
                and stage.name not in self.cfg.force_stages
            )
            if cached:
                with self._lock:
                    self.skipped.append((page, stage.name))
                continue
            spent = self._spent(page, stage.name)
            # Mark the stage pending before touching its outputs: a crash mid-write must never leave a
            # "done" record next to a half-written file.
            with self._lock:
                self.manifest.set_record(page, stage.name, StageRecord(status="pending", input_hash=h, spent_usd=spent))
                self._save()
            try:
                cost = stage.run(job) or 0.0
            except Exception as e:  # one bad page must not stop the book
                cost = float(getattr(e, "cost_usd", 0.0) or 0.0)  # billed calls before the failure still count
                self._fail(page, stage, f"{type(e).__name__}: {e}", h, traceback.format_exc(), spent + cost)
                return False
            absent = [n for n in stage.outputs if not job.path(n).exists()]
            if absent:
                self._fail(page, stage, f"stage wrote no {absent}", h, spent=spent + cost)
                return False
            with self._lock:
                rec = StageRecord(status="done", input_hash=h, cost_usd=cost, spent_usd=spent + cost)
                self.manifest.set_record(page, stage.name, rec)
                self.ran.append((page, stage.name))
                self._save()
        with self._lock:
            self.completed.append(page)
        return True

    def _spent(self, page: int, stage: str) -> float:
        with self._lock:
            rec = self.manifest.record(page, stage)
        return max(rec.spent_usd, rec.cost_usd) if rec else 0.0

    def _fail(self, page: int, stage: Stage, msg: str, h: str = "", tb: str = "", spent: float | None = None) -> None:
        if spent is None:
            spent = self._spent(page, stage.name)
        with self._lock:
            self.manifest.set_record(page, stage.name, StageRecord(status="failed", input_hash=h, error=msg, spent_usd=spent))
            self.failed.append((page, stage.name, msg))
            self._save()
        print(f"[p{page:04d}] {stage.name} failed: {msg}", file=sys.stderr)
        if tb:
            print(tb, file=sys.stderr)

    def run(self, pages: Iterable[int]) -> None:
        pages = list(pages)
        if self.cfg.jobs <= 1:
            for p in pages:
                self.run_page(p)
            return
        with ThreadPoolExecutor(max_workers=self.cfg.jobs) as pool:
            for fut in as_completed([pool.submit(self.run_page, p) for p in pages]):
                fut.result()


# ---------------------------------------------------------------- default stages


def _ingest(job: PageJob) -> None:
    from . import ingest

    ingest.extract_page(job.cfg.input_path, job.page, job.path(SOURCE_PNG), dpi=job.cfg.dpi)


def _ingest_text(job: PageJob) -> None:
    page: Page = job.shared["text_pages"][job.page - 1]
    page.model_copy(update={"page": job.page}).save(job.path(OCR_JSON))


def _ingest_second(job: PageJob) -> None:
    """The page from the second reading of a text book; an empty page when that reading has fewer pages,
    so every line of this page shows up as read only once."""
    pages: list[Page] = job.shared["second_pages"]
    page = pages[job.page - 1] if job.page <= len(pages) else Page(page=job.page)
    page.model_copy(update={"page": job.page}).save(job.path(OCR2_JSON))


def _preprocess(job: PageJob) -> None:
    from . import preprocess

    preprocess.preprocess_page(job.path(SOURCE_PNG), job.path(PRE_PNG), upscale=job.cfg.upscale)


def _ocr(job: PageJob) -> float:
    result = job.shared["backend"].read_page(job.path(PRE_PNG), job.page)
    page = result.page.model_copy(update={"page": job.page, "source_image": str(job.path(PRE_PNG))})
    page.save(job.path(OCR_JSON))
    return result.cost_usd


def _crossread(job: PageJob) -> float:
    result = job.shared["cross_backend"].read_page(job.path(PRE_PNG), job.page)
    result.page.model_copy(update={"page": job.page}).save(job.path(OCR2_JSON))
    return result.cost_usd


def cross_backend(name: str, model: str | None, source_script: str = "devanagari", languages: tuple[str, ...] = ()):
    """The second reader: the same backend and prompt, with the page cut into one more band so the two
    reads see different image crops and do not share one band's slip."""
    from .ocr import get_backend

    backend = get_backend(name, model=model, source_script=source_script, languages=languages)
    if hasattr(backend, "bands"):
        backend.bands += 1
    return backend


def to_master_form(page: Page, source_script: str) -> Page:
    """An OCR read as the master holds it: Sanskrit converted from the book's script to Devanagari, then
    normalized. Every read of a page goes through here, so two reads are compared in the same form."""
    from . import normalize

    if source_script != DEVA:
        from . import transliterate

        page = transliterate.page_to_devanagari(page, source_script)
    return normalize.normalize_page(page)


def _normalize(job: PageJob) -> None:
    to_master_form(Page.load(job.path(OCR_JSON)), job.cfg.source_script).save(job.path(NORM_JSON))


def _validate(job: PageJob) -> None:
    from . import validate

    image, second = job.path(PRE_PNG), job.path(OCR2_JSON)
    # The image svara count finds Devanagari headlines; other scripts have none to find.
    use_image = image.exists() and job.cfg.source_script == DEVA
    # The second read is raw OCR in the book's script; norm.json is Devanagari. Compare like with like.
    other = to_master_form(Page.load(second), job.cfg.source_script) if second.exists() else None
    report = validate.validate_page(Page.load(job.path(NORM_JSON)), image if use_image else None, other)
    report.save(job.path(FLAGS_JSON))


def _validate_fingerprint(job: PageJob) -> str:
    from .normalize import NORMALIZE_VERSION
    from .validate import VALIDATE_VERSION

    # The page image and a second read are read too, so their content and the checker version all count;
    # the second read is normalized here, so the normalize version counts as well.
    image, second = job.path(PRE_PNG), job.path(OCR2_JSON)
    src = "" if job.cfg.source_script == DEVA else f":src={job.cfg.source_script}"
    return (
        f"v{VALIDATE_VERSION}:n{NORMALIZE_VERSION}:image={sha256_file(image) if image.exists() else 'none'}"
        f":second={sha256_file(second) if second.exists() else 'none'}{src}"
    )


def _normalize_fingerprint(job: PageJob) -> str:
    from .normalize import NORMALIZE_VERSION

    # Devanagari books keep their original fingerprint; another source script adds its conversion.
    return f"v{NORMALIZE_VERSION}" + ("" if job.cfg.source_script == DEVA else f":src={job.cfg.source_script}")


def default_stages(input_kind: str, *, cross_read: bool = False, second_read: bool = False) -> list[Stage]:
    """Stage chain for an input kind: image inputs go through OCR, text inputs start at ocr.json.
    With cross_read, image inputs get a second independent OCR read that validate compares against;
    with second_read, text inputs take their second read from the --second-read file."""
    doc = lambda job: job.shared["input_hash"] + f":{job.page}"  # noqa: E731
    tail = [
        Stage("normalize", (OCR_JSON,), (NORM_JSON,), _normalize, _normalize_fingerprint),
        Stage("validate", (NORM_JSON,), (FLAGS_JSON,), _validate, _validate_fingerprint),
    ]
    if input_kind in ("text", "json"):
        second = [Stage("crossread", (), (OCR2_JSON,), _ingest_second,
                        lambda job: job.shared["second_hash"] + f":{job.page}")] if second_read else []
        return [Stage("ingest", (), (OCR_JSON,), _ingest_text, doc), *second, *tail]
    return [
        Stage("ingest", (), (SOURCE_PNG,), _ingest, lambda job: doc(job) + f":dpi={job.cfg.dpi}"),
        Stage("preprocess", (SOURCE_PNG,), (PRE_PNG,), _preprocess, lambda job: f"upscale={job.cfg.upscale}"),
        Stage("ocr", (PRE_PNG,), (OCR_JSON,), _ocr, lambda job: job.shared["backend"].fingerprint()),
        *([Stage("crossread", (PRE_PNG,), (OCR2_JSON,), _crossread,
                 lambda job: job.shared["cross_backend"].fingerprint())] if cross_read else []),
        *tail,
    ]


# ---------------------------------------------------------------- master, corrections, render


@contextmanager
def out_lock(out_dir: Path) -> Iterator[None]:
    """Exclusive lock on an output directory: two processes sharing --out would overwrite each other's
    manifest, master and render build directory."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / ".vedalipi.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"another vedalipi process is using {out_dir}") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def build_master(
    cfg: RunConfig, pages: Iterable[int], *, title: str = "", num_pages: int | None = None,
    errors: list[tuple[int, str, str]] | None = None,
) -> Master:
    """Merge norm.json + flags.json for each page into master.json.

    Only pass pages whose whole stage chain completed in this run. A page already in the master is
    rebuilt only when its norm.json or flags.json changed. Its hand corrections (master.corrections) are
    then applied again when its OCR text is the one they were made on (normalize or validate changed),
    and dropped with a warning, saved to corrections.dropped.json, when the OCR text changed. Pages beyond
    `num_pages` (left over from a longer earlier input) are dropped. A page whose cache files cannot be
    read is skipped and reported in `errors`, never fatal.
    """
    master = Master.load(cfg.master_path) if cfg.master_path.exists() else Master(title=title)
    master.source = str(cfg.input_path)
    if num_pages is not None:
        prune_pages(master, num_pages)
    dropped: list[dict] = []
    for p in pages:
        d = page_dir(cfg.work_dir, p)
        norm, flags = d / NORM_JSON, d / FLAGS_JSON
        if not (norm.exists() and flags.exists()):
            continue
        try:
            _merge_page(master, p, norm, flags, _origin(cfg, p), dropped)
        except (ValidationError, ValueError, OSError) as e:
            msg = f"unreadable page cache, page left out of master: {type(e).__name__}: {str(e)[:200]}"
            print(f"[p{p:04d}] {msg}", file=sys.stderr)
            if errors is not None:
                errors.append((p, "master", msg))
    if dropped:
        import json

        path = cfg.out_dir / "corrections.dropped.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        atomic_write_text(path, json.dumps(old + dropped, ensure_ascii=False, indent=1) + "\n")
    master.save(cfg.master_path)
    return master


def _origin(cfg: RunConfig, p: int) -> PageOrigin:
    job = PageJob(p, page_dir(cfg.work_dir, p), cfg)
    ocr = job.path(OCR_JSON)
    return PageOrigin(
        ocr=sha256_file(ocr) if ocr.exists() else "",
        normalize_settings=_normalize_fingerprint(job),
        validate_settings=_validate_fingerprint(job),
    )


# Names for the parts of a stage's settings string ("v3:src=kannada"), for saying what changed.
_SETTING_NAMES = {"v": "version", "n": "normalize version", "image": "page image", "second": "second read",
                  "src": "source script"}


def _what_changed(stage: str, old: str, new: str) -> str:
    """ "normalize changed (version 2 -> 3)" from two settings strings; hashes are only said to change."""
    def parts(s: str) -> dict[str, str]:
        out = {}
        for item in s.split(":") if s else []:
            key, _, val = item.partition("=")
            if not val:  # "v3", "n2": a letter and a number
                key, val = item[:1], item[1:]
            out[key] = val
        return out

    a, b = parts(old), parts(new)
    diffs = []
    for key in dict.fromkeys([*a, *b]):
        if a.get(key) != b.get(key):
            name = _SETTING_NAMES.get(key, key)
            x, y = a.get(key, "devanagari" if key == "src" else "none"), b.get(key, "devanagari" if key == "src" else "none")
            diffs.append(f"{name} changed" if max(len(x), len(y)) > 20 else f"{name} {x} -> {y}")
    if not old:
        return f"{stage} output changed"
    return f"{stage} changed ({', '.join(diffs)})" if diffs else f"{stage} output changed"


def _reapply(master: Master, p: int, mine: list[Correction], dropped: list[dict], why: str) -> int:
    """Apply page p's recorded corrections again, in order; one that no longer fits is dropped. Returns
    how many were applied."""
    kept = 0
    for c in mine:
        try:
            _apply_correction(master, c)
            kept += 1
        except (KeyError, ValueError) as e:
            master.corrections = [x for x in master.corrections if x is not c]
            dropped.append({"reason": f"{why}; no longer fits: {e}", "correction": c.model_dump(exclude_none=True)})
            print(f"[p{p:04d}] correction for {c.block_id} no longer fits ({e}); dropped (saved in "
                  f"corrections.dropped.json)", file=sys.stderr)
    return kept


def _plural(n: int) -> str:
    return f"{n} correction{'s' if n != 1 else ''}"


def _merge_page(master: Master, p: int, norm: Path, flags: Path, origin: PageOrigin, dropped: list[dict]) -> None:
    h, fh = sha256_file(norm), sha256_file(flags)
    before = master.origins.get(p)
    exists = any(pg.page == p for pg in master.pages)
    mine = [c for c in master.corrections if c.page == p]
    if master.norm_hashes.get(p) == h and exists:
        if master.flag_hashes.get(p) != fh:
            # Same text, new validation: swap the validator's flags, keep everything else, and apply the
            # page's corrections again (a proofreader's cleared flags stay cleared).
            report = FlagReport.load(flags)
            if mine:
                # Corrections are replayed below, and a line insertion or removal is not idempotent: start
                # from the normalized page, not the already corrected one.
                page = Page.load(norm)
            else:
                page = master.page(p)
                for b in page.blocks:
                    b.flags = [f for f in b.flags if f not in VALIDATOR_FLAGS]
            master.upsert_page(report.apply_to(page))
            master.flag_hashes[p] = fh
            if mine:
                why = _what_changed("validate", before.validate_settings if before else "", origin.validate_settings)
                n = _reapply(master, p, mine, dropped, why)
                print(f"[p{p:04d}] {why}: flags refreshed, {_plural(n)} re-applied", file=sys.stderr)
        master.origins[p] = origin
        return
    report, page = FlagReport.load(flags), Page.load(norm)
    master.upsert_page(report.apply_to(page))
    same_ocr = before is not None and before.ocr != "" and before.ocr == origin.ocr
    if exists and mine:
        if same_ocr:
            why = _what_changed("normalize", before.normalize_settings, origin.normalize_settings)
            n = _reapply(master, p, mine, dropped, why)
            print(f"[p{p:04d}] {why}; OCR text unchanged: page rebuilt, {_plural(n)} re-applied", file=sys.stderr)
        else:
            master.corrections = [c for c in master.corrections if c.page != p]
            dropped.extend({"reason": "OCR text changed", "correction": c.model_dump(exclude_none=True)} for c in mine)
            print(f"[p{p:04d}] OCR text changed: page rebuilt, {_plural(len(mine))} dropped "
                  f"(saved in corrections.dropped.json)", file=sys.stderr)
    elif exists and before is None and p in master.norm_hashes:
        # A master made before corrections were recorded: hand edits to this page cannot be told apart.
        print(f"[p{p:04d}] page rebuilt; this master predates recorded corrections, so hand edits made to "
              f"this page before, if any, are lost", file=sys.stderr)
    master.norm_hashes[p] = h
    master.flag_hashes[p] = fh
    master.origins[p] = origin


def prune_pages(master: Master, num_pages: int) -> None:
    """Drop pages the current input does not have (a shorter replacement input must not keep ghost pages)."""
    gone = [pg.page for pg in master.pages if pg.page > num_pages]
    if gone:
        print(f"dropping pages {gone} from master: input has only {num_pages} pages", file=sys.stderr)
    master.pages = [pg for pg in master.pages if pg.page <= num_pages]
    master.norm_hashes = {k: v for k, v in master.norm_hashes.items() if k <= num_pages}
    master.flag_hashes = {k: v for k, v in master.flag_hashes.items() if k <= num_pages}


def apply_corrections(master: Master, corrections: list[Correction], *, record: bool = True) -> Master:
    """Apply hand edits to a copy of master; fields left as None are unchanged. With `record`, the
    corrections are added to master.corrections so later rebuilds of their pages can apply them again."""
    out = master.model_copy(deep=True)
    for c in corrections:
        _apply_correction(out, c)
    if record:
        out.corrections = [*out.corrections, *corrections]
    return Master.model_validate(out.model_dump())


def _apply_correction(master: Master, c: Correction) -> None:
    """Apply one correction to master in place; KeyError when its page or block is gone, ValueError when
    a line it edits is not there. Sanskrit text in a correction is normalized exactly like OCR text, so a
    corrected line follows the same rules as every other line of the master."""
    from .normalize import normalize_text

    block = master.page(c.page).block(c.block_id)
    sa = normalize_text if block.is_sanskrit else (lambda t: t)
    if c.bbox is not None:
        block.bbox = c.bbox
    if c.type is not None:
        block.type = c.type
        if c.type == "image":
            # A drawing carries no typeset text of its own: only its labels, if any.
            block.lines, block.rows = [], None
    if c.labels is not None:
        block.labels = [lab.model_copy(update={"text": normalize_text(lab.text)}) if lab.is_sanskrit else lab
                        for lab in c.labels]
    if c.lines is not None:
        block.lines = [sa(line) for line in c.lines]
    if c.set_lines:
        lines = list(block.lines)
        for n in sorted(c.set_lines, reverse=True):  # from the bottom, so a split line moves no line above it
            if not 1 <= n <= len(lines):
                raise ValueError(f"{c.block_id} has no line {n}")
            text = c.set_lines[n]
            lines[n - 1 : n] = [sa(t) for t in text.split("\n")] if text != "" else []
        block.lines = lines
    if c.rows is not None:
        block.rows = [[sa(cell) for cell in row] for row in c.rows]
    if c.flags is not None:
        block.flags = c.flags
    if c.clear_flags:
        block.flags = [f for f in block.flags if f not in c.clear_flags]
    if c.level is not None:
        block.level = c.level
    if c.align is not None:
        block.align = c.align


def diagnostics_path(out_dir: Path, script: str) -> Path:
    """Where render writes the transliteration diagnostics (round-trip failures) for one script."""
    return Path(out_dir) / "work" / f"transliterate-{script}.json"


def pdf_path(
    out_dir: Path, master: Master, script: str, *, side_by_side: bool = False, translation: str | None = None
) -> Path:
    """Where render writes the PDF of one script: <out>/<source stem>.<script>[.<lang>][.side-by-side].pdf."""
    stem = Path(master.source).stem or "book"
    lang = f".{translation}" if translation else ""
    return Path(out_dir) / f"{stem}.{script}{lang}{'.side-by-side' if side_by_side else ''}.pdf"


def render_from_master(
    out_dir: Path, script: str, *, side_by_side: bool = False, translation: str | None = None,
    all_meanings: bool = False,
) -> Path:
    """Transliterate master.json into `script` and render <out>/<stem>.<script>.pdf. Never touches OCR.

    With `translation` (a language code), the edition of <out>/translation.<lang>.json: other-language text
    replaced by its translation, Sanskrit in `script` with its cue before it and its meaning under it. A
    mantra's meaning prints where it first appears; `all_meanings` prints it at every repeat too."""
    with out_lock(out_dir):
        return _render(Path(out_dir), script, side_by_side=side_by_side, translation=translation,
                       all_meanings=all_meanings)


def _render(
    out_dir: Path, script: str, *, side_by_side: bool = False, translation: str | None = None,
    all_meanings: bool = False,
) -> Path:
    import json

    from . import transliterate
    from .render import render_pdf

    master = Master.load(out_dir / "master.json")
    diagnostics: list[dict] = []
    target = transliterate.transliterate_master(master, script, diagnostics=diagnostics)
    meanings: dict = {}
    if translation:
        from . import translate
        from .schema import Translation

        tpath = translate.translation_path(out_dir, translation)
        if not tpath.exists():
            raise FileNotFoundError(f"no {tpath.name}; run `vedalipi translate {out_dir} --to {translation}` first")
        if side_by_side:
            raise ValueError("a translated edition has no side-by-side layout; render it without --side-by-side")
        tr = Translation.load(tpath)
        target, meanings, missing, stale = translate.apply_translation(master, target, tr)
        for p in stale:
            print(f"[p{p:04d}] master text changed since it was translated; printed untranslated "
                  f"(run `vedalipi translate {out_dir} --to {translation}`)", file=sys.stderr)
        for p, bid in missing:
            if p not in stale:
                print(f"[p{p:04d}] {bid}: no translation; printed untranslated", file=sys.stderr)
        if not all_meanings:
            meanings = translate.first_meanings(master, meanings)
    atomic_write_text(diagnostics_path(out_dir, script), json.dumps(diagnostics, ensure_ascii=False, indent=1) + "\n")
    for d in diagnostics:
        print(f"[p{d['page']:04d}] {d['block']}: round trip failed: {d['text']!r} -> {d['back']!r}", file=sys.stderr)
    out_pdf = pdf_path(out_dir, master, script, side_by_side=side_by_side, translation=translation)
    render_pdf(
        master, target, out_pdf, script=script, work_dir=out_dir / "work", side_by_side=side_by_side,
        meanings=meanings, meaning_lang=translation or "en", translated=bool(translation),
        build_name=f"{script}-{translation}" if translation else None,
    )
    return out_pdf


def run(cfg: RunConfig) -> tuple[StageRunner, Path | None]:
    """Full run: ingest -> ... -> validate for selected pages, build master, render the target script."""
    with out_lock(cfg.out_dir):
        return _run(cfg)


def _run(cfg: RunConfig) -> tuple[StageRunner, Path | None]:
    from . import ingest

    kind = ingest.input_kind(cfg.input_path)
    if cfg.second_read is not None and kind not in ("text", "json"):
        raise ValueError("--second-read is for .txt/.json input; for a scan use --cross-read")
    input_hash = sha256_input(cfg.input_path)
    shared: dict = {"input_hash": input_hash}
    if kind in ("text", "json"):
        shared["text_pages"] = ingest.load_text_pages(cfg.input_path)
        num_pages = len(shared["text_pages"])
        if cfg.second_read is not None:
            shared["second_pages"] = ingest.load_text_pages(cfg.second_read)
            shared["second_hash"] = sha256_input(cfg.second_read)
            if len(shared["second_pages"]) != num_pages:
                print(f"second read has {len(shared['second_pages'])} pages, the book {num_pages}; pages it lacks "
                      "are read only once", file=sys.stderr)
    else:
        from .ocr import get_backend

        num_pages = ingest.page_count(cfg.input_path)
        shared["backend"] = get_backend(
            cfg.backend, model=cfg.model, source_script=cfg.source_script, languages=cfg.languages
        )
        if cfg.cross_read:
            shared["cross_backend"] = cross_backend(cfg.backend, cfg.model, cfg.source_script, cfg.languages)

    manifest = Manifest.load(cfg.manifest_path) if cfg.manifest_path.exists() else None
    if manifest is None or manifest.input_hash != input_hash:
        if manifest is not None:
            print("input changed since last run; stages will re-run where page inputs differ", file=sys.stderr)
        manifest = Manifest(
            input_path=str(cfg.input_path),
            input_hash=input_hash,
            input_kind=kind,
            num_pages=num_pages,
            # Records of pages the new input does not have are dropped (their spend is history, not cache).
            pages={p: recs for p, recs in (manifest.pages if manifest else {}).items() if p <= num_pages},
        )
    manifest.source_script = cfg.source_script
    manifest.save(cfg.manifest_path)

    pages = parse_pages(cfg.pages, num_pages)
    stages = default_stages(kind, cross_read=cfg.cross_read, second_read=cfg.second_read is not None)
    runner = StageRunner(stages, manifest, cfg, shared)
    runner.run(pages)
    build_master(cfg, sorted(runner.completed), title=cfg.input_path.stem, num_pages=num_pages, errors=runner.failed)
    if runner.failed:
        return runner, None
    return runner, _render(cfg.out_dir, cfg.script, side_by_side=cfg.side_by_side)


def _stray_lines(master: Master) -> list[str]:
    """Every source letter left unconverted and every non-Vedic mark in the master's Sanskrit, read from the
    text itself (so hand corrections show), one line per string and character, with totals per character."""
    from .validate import page_strays

    found = [(pg.page, s) for pg in master.pages for s in page_strays(pg)]
    if not found:
        return []
    out = ["stray characters in Sanskrit text (kept as read; proofread against the scan):"]
    totals: dict[tuple[str, str], int] = {}
    for page, s in found:
        out.append(f"  p{page}:{s.note()}")
        totals[(s.flag, s.name)] = totals.get((s.flag, s.name), 0) + s.count
    for (flag, name), n in sorted(totals.items()):
        out.append(f"  total: {flag} {name} x{n}")
    return out


def _single_read_lines(out_dir: Path, master: Master) -> list[str]:
    """Lines only one of the two reads has, from each master page's flags.json notes."""
    out: list[str] = []
    for pg in master.pages:
        flags = page_dir(Path(out_dir) / "work", pg.page) / FLAGS_JSON
        if not flags.exists():
            continue
        try:
            notes = FlagReport.load(flags).notes
        except (ValidationError, ValueError, OSError):
            continue
        out += [f"  p{pg.page}:{n}" for n in notes if ": single_read: " in n]
    if out:
        out.insert(0, "lines only one read has (no second read checks them; proofread against the scan):")
    return out


def report(out_dir: Path) -> str:
    """Human-readable status: stage counts, failures, OCR cost, flagged blocks and round-trip failures."""
    import json

    lines: list[str] = []
    mpath = out_dir / "work" / "manifest.json"
    if mpath.exists():
        m = Manifest.load(mpath)
        lines.append(f"input: {m.input_path} ({m.input_kind}, {m.num_pages} pages)")
        counts: dict[str, dict[str, int]] = {}
        for stages in m.pages.values():
            for name, rec in stages.items():
                counts.setdefault(name, {}).setdefault(rec.status, 0)
                counts[name][rec.status] += 1
        for name, c in counts.items():
            lines.append(f"  {name}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
        for page, stages in sorted(m.pages.items()):
            for name, rec in stages.items():
                if rec.status == "failed":
                    lines.append(f"  FAILED p{page:04d} {name}: {rec.error}")
        # Translation records sit in the same manifest under translate-<lang>; their cost is reported apart.
        tr_stages = sorted({n for st in m.pages.values() for n in st if n.startswith("translate-")})
        cost = lambda pred, f: sum(f(r) for st in m.pages.values() for n, r in st.items() if pred(n))  # noqa: E731
        is_tr = lambda n: n.startswith("translate-")  # noqa: E731
        cached, spent = (lambda r: r.cost_usd), (lambda r: max(r.spent_usd, r.cost_usd))
        lines.append(f"OCR cost: ${cost(lambda n: not is_tr(n), cached):.2f} for the cached outputs, "
                     f"${cost(lambda n: not is_tr(n), spent):.2f} spent in total")
        for name in tr_stages:
            lines.append(f"{name} cost: ${cost(lambda n, name=name: n == name, cached):.2f} for the cached outputs, "
                         f"${cost(lambda n, name=name: n == name, spent):.2f} spent in total")
    master_path = out_dir / "master.json"
    if master_path.exists():
        master = Master.load(master_path)
        flagged: dict[str, list[str]] = {}
        for pg in master.pages:
            for b in pg.blocks:
                for f in b.flags:
                    flagged.setdefault(f, []).append(f"p{pg.page}:{b.id}")
        lines.append(f"master: {len(master.pages)} pages")
        for f, where in sorted(flagged.items()):
            lines.append(f"  {f}: {len(where)} ({', '.join(where[:10])}{' ...' if len(where) > 10 else ''})")
        lines += _stray_lines(master)
        lines += _single_read_lines(out_dir, master)
    for diag in sorted((out_dir / "work").glob("transliterate-*.json")):
        items = json.loads(diag.read_text(encoding="utf-8"))
        script = diag.stem.removeprefix("transliterate-")
        where = [f"p{d['page']}:{d['block']}" for d in items]
        lines.append(f"roundtrip_fail ({script}): {len(items)}" + (f" ({', '.join(where[:10])}{' ...' if len(where) > 10 else ''})" if items else ""))
    return "\n".join(lines) if lines else f"nothing found in {out_dir}"
