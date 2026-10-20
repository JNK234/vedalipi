# ABOUTME: Data contract for vedalipi: page/block JSON, per-page flag reports, the run manifest and the Devanagari master.
# ABOUTME: Pydantic models with atomic JSON load/save helpers; every stage reads and writes these shapes on disk.

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BlockType = Literal[
    "running_header",
    "heading",
    "verse",
    "prose",
    "list_item",
    "table",
    "fill_blank",
    "page_number",
    "image",
    "foreign",
    "separator",
]
Align = Literal["left", "center", "right"]

# Flags known to the pipeline. Blocks may carry other strings; these are the ones stages emit.
FLAG_TRUNCATED_RIGHT = "truncated_right"
FLAG_LOW_CONFIDENCE = "low_confidence"
FLAG_SVARA_COUNT_MISMATCH = "svara_count_mismatch"
FLAG_ROUNDTRIP_FAIL = "roundtrip_fail"
FLAG_INVALID_SEQUENCE = "invalid_sequence"
FLAG_AMBIGUOUS_SVARA_SPAN = "ambiguous_svara_span"  # set by the OCR model: one printed mark spans two aksharas
FLAG_READER_DISAGREEMENT = "reader_disagreement"  # an independent second OCR read of the line differs
# Flags owned by validate.py: replaced wholesale whenever a page is re-validated.
VALIDATOR_FLAGS = (FLAG_SVARA_COUNT_MISMATCH, FLAG_INVALID_SEQUENCE, FLAG_READER_DISAGREEMENT)

StageStatus = Literal["pending", "done", "failed"]

M = TypeVar("M", bound=BaseModel)


def atomic_write_text(path: Path, text: str) -> None:
    """Write text to path via a temp file + rename so readers never see a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class _JsonModel(BaseModel):
    """Base with load/save helpers. Unknown keys are rejected so contract drift is caught early."""

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def load(cls: type[M], path: Path) -> M:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        atomic_write_text(Path(path), self.model_dump_json(indent=2) + "\n")

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


def _check_bbox_value(v: list[float] | None) -> list[float] | None:
    if v is None:
        return v
    if len(v) != 4:
        raise ValueError("bbox must be [x0, y0, x1, y1]")
    x0, y0, x1, y1 = v
    if not all(0.0 <= c <= 1.0 for c in v) or x0 > x1 or y0 > y1:
        raise ValueError(f"bbox must be normalized 0-1 with x0<=x1, y0<=y1: {v}")
    return v


class Label(_JsonModel):
    """A text label printed inside a drawing (an image block): shown in the target script over the drawing.

    bbox is in normalized page coordinates, like Block.bbox, and covers exactly the label's printed ink.
    """

    text: str
    lang: str = "sa"
    bbox: list[float]

    @field_validator("bbox")
    @classmethod
    def _check_bbox(cls, v: list[float]) -> list[float]:
        return _check_bbox_value(v)

    @property
    def is_sanskrit(self) -> bool:
        return self.lang == "sa"


class Block(_JsonModel):
    """One layout unit on a page. Only lang == "sa" text is transliterated."""

    id: str
    type: BlockType
    lang: str = "sa"
    lines: list[str] = Field(default_factory=list)
    rows: list[list[str]] | None = None
    level: int | None = None
    align: Align = "left"
    bbox: list[float] | None = None
    flags: list[str] = Field(default_factory=list)
    # Text labels drawn inside an image block; left out of the JSON when absent so older files stay as they are.
    labels: list[Label] | None = Field(default=None, exclude_if=lambda v: v is None)

    @field_validator("bbox")
    @classmethod
    def _check_bbox(cls, v: list[float] | None) -> list[float] | None:
        return _check_bbox_value(v)

    @model_validator(mode="after")
    def _check_shape(self) -> Block:
        if self.type == "table" and self.rows is None:
            raise ValueError(f"table block {self.id} needs rows")
        if self.type != "table" and self.rows is not None:
            raise ValueError(f"only table blocks may have rows ({self.id} is {self.type})")
        if self.type == "image" and self.bbox is None:
            raise ValueError(f"image block {self.id} needs a bbox")
        if self.labels is not None:
            if self.type != "image":
                raise ValueError(f"only image blocks may have labels ({self.id} is {self.type})")
            x0, y0, x1, y1 = self.bbox
            for lab in self.labels:
                lx0, ly0, lx1, ly1 = lab.bbox
                if lx0 < x0 or ly0 < y0 or lx1 > x1 or ly1 > y1:
                    raise ValueError(f"label {lab.text!r} bbox {lab.bbox} lies outside image block {self.id} bbox {self.bbox}")
        return self

    @property
    def is_sanskrit(self) -> bool:
        return self.lang == "sa"

    def add_flag(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)


class Page(_JsonModel):
    """One source page: the unit written to ocr.json and norm.json."""

    page: int
    source_image: str = ""
    blocks: list[Block] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> Page:
        ids = [b.id for b in self.blocks]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"page {self.page}: duplicate block ids {sorted(dupes)}")
        return self

    def block(self, block_id: str) -> Block:
        for b in self.blocks:
            if b.id == block_id:
                return b
        raise KeyError(f"page {self.page}: no block {block_id!r}")


class FlagReport(_JsonModel):
    """validate.py output (flags.json): flags per block id plus free-form notes for the report."""

    page: int
    block_flags: dict[str, list[str]] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    def apply_to(self, page: Page) -> Page:
        """Return a copy of page with these flags merged into its blocks."""
        out = page.model_copy(deep=True)
        for b in out.blocks:
            for f in self.block_flags.get(b.id, []):
                b.add_flag(f)
        return out


class StageRecord(_JsonModel):
    status: StageStatus = "pending"
    input_hash: str = ""
    cost_usd: float = 0.0  # cost of the run that produced the cached output
    spent_usd: float = 0.0  # everything this page/stage has ever cost, failed and superseded runs included
    error: str | None = None
    updated_at: str = ""


class Manifest(_JsonModel):
    """work/manifest.json: input identity and per-page, per-stage status."""

    input_path: str
    input_hash: str
    input_kind: str
    num_pages: int
    pages: dict[int, dict[str, StageRecord]] = Field(default_factory=dict)

    def record(self, page: int, stage: str) -> StageRecord | None:
        return self.pages.get(page, {}).get(stage)

    def set_record(self, page: int, stage: str, rec: StageRecord) -> None:
        rec.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.pages.setdefault(page, {})[stage] = rec

    def total_cost(self) -> float:
        """Cost of the outputs currently cached."""
        return sum(r.cost_usd for stages in self.pages.values() for r in stages.values())

    def total_spent(self) -> float:
        """Everything spent, including failed attempts and outputs later redone."""
        return sum(max(r.spent_usd, r.cost_usd) for stages in self.pages.values() for r in stages.values())


class Master(_JsonModel):
    """<out>/master.json: the Devanagari source of truth for every rendered script.

    norm_hashes maps page -> hash of the norm.json it was built from, so a rebuild only
    replaces pages whose OCR actually changed and hand corrections survive re-runs.
    flag_hashes does the same for flags.json: a re-validated page gets fresh validator flags
    while its (possibly corrected) text is kept.
    """

    title: str = ""
    source: str = ""
    pages: list[Page] = Field(default_factory=list)
    norm_hashes: dict[int, str] = Field(default_factory=dict)
    flag_hashes: dict[int, str] = Field(default_factory=dict)

    def page(self, number: int) -> Page:
        for p in self.pages:
            if p.page == number:
                return p
        raise KeyError(f"no page {number} in master")

    def upsert_page(self, page: Page) -> None:
        for i, p in enumerate(self.pages):
            if p.page == page.page:
                self.pages[i] = page
                return
        self.pages.append(page)
        self.pages.sort(key=lambda p: p.page)


class Correction(_JsonModel):
    """One hand edit to master.json: replace lines and/or rows of a block, set its flags, or fix its
    heading level or alignment. Fields left out stay as they are.

    Setting type "image" turns the block into a drawing: its lines and rows are cleared, and it keeps its
    bbox unless one is given. labels sets the text labels drawn inside an image block."""

    page: int
    block_id: str
    type: BlockType | None = None
    bbox: list[float] | None = None
    labels: list[Label] | None = None
    lines: list[str] | None = None
    rows: list[list[str]] | None = None
    flags: list[str] | None = None
    level: int | None = None
    align: Align | None = None

    @field_validator("bbox")
    @classmethod
    def _check_bbox(cls, v: list[float] | None) -> list[float] | None:
        return _check_bbox_value(v)


class BlockTranslation(_JsonModel):
    """One block in the target language: its lines (or table rows, or label texts), plus an optional
    word-by-word gloss [[word, meaning], ...] for a Sanskrit block."""

    lines: list[str] = Field(default_factory=list)
    rows: list[list[str]] | None = None
    labels: list[str] | None = None
    gloss: list[list[str]] | None = None


class PageTranslation(_JsonModel):
    """A page's translation. `key` hashes the page text and translate settings it was made from: a page is
    re-translated only when its key changes, so hand edits to `blocks` survive re-runs."""

    key: str
    blocks: dict[str, BlockTranslation] = Field(default_factory=dict)


class Translation(_JsonModel):
    """<out>/translation.<lang>.json: the master translated into `lang` (ISO 639-1).

    `mantra_script` is the vidyut scheme Sanskrit is shown in next to its meaning (e.g. "iast")."""

    lang: str
    mantra_script: str
    pages: dict[int, PageTranslation] = Field(default_factory=dict)


def load_json(path: Path) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))
