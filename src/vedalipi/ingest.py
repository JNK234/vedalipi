# ABOUTME: Ingest stage: identifies the input kind (PDF, image folder, Devanagari .txt/.json) and yields source pages.
# ABOUTME: Image inputs become one source.png per page; text inputs become Page objects that skip OCR entirely.

from __future__ import annotations

from pathlib import Path
from typing import Literal

from .schema import Page

InputKind = Literal["pdf", "images", "text", "json"]


def input_kind(path: Path) -> InputKind:
    """Classify the input: a .pdf file, a directory of page images, a Devanagari .txt, or a page-contract .json."""
    raise NotImplementedError


def page_count(path: Path) -> int:
    """Number of source pages for a pdf or image-folder input (images sorted by natural filename order)."""
    raise NotImplementedError


def extract_page(path: Path, page: int, dst: Path, *, dpi: int = 300) -> None:
    """Write 1-based source page `page` of a pdf/images input to dst as PNG.

    PDFs with one embedded raster per page should export that raster at native resolution
    rather than re-rasterize; otherwise render at `dpi`. Must not hold other pages in memory.
    """
    raise NotImplementedError


def load_text_pages(path: Path) -> list[Page]:
    """Load a Devanagari .txt (form-feed or blank-line page breaks) or a page-contract .json into Pages.

    A .json may be a single page, a list of pages, or a master.json. Returned pages have lang="sa"
    blocks with no bbox and an empty source_image.
    """
    raise NotImplementedError
