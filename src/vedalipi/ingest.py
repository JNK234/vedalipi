# ABOUTME: Ingest stage: identifies the input kind (PDF, image folder, Devanagari .txt/.json) and yields source pages.
# ABOUTME: Image inputs become one source.png per page; text inputs become Page objects that skip OCR entirely.

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from .schema import Page

InputKind = Literal["pdf", "images", "text", "json"]

# Image file extensions accepted in a folder input.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def _natural_key(p: Path) -> tuple:
    """Sort key that treats digit runs as numbers (page2 < page10)."""
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name))


def _image_files(path: Path) -> list[Path]:
    files = [p for p in path.iterdir() if p.suffix.lower() in IMAGE_EXTS]
    if not files:
        raise ValueError(f"no image files ({sorted(IMAGE_EXTS)}) in {path}")
    return sorted(files, key=_natural_key)


def input_kind(path: Path) -> InputKind:
    """Classify the input: a .pdf file, a directory of page images, a Devanagari .txt, or a page-contract .json."""
    path = Path(path)
    if path.is_dir():
        return "images"
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".txt":
        return "text"
    if suffix == ".json":
        return "json"
    raise ValueError(f"unsupported input {path}: expected a pdf, image folder, .txt or .json")


def page_count(path: Path) -> int:
    """Number of source pages for a pdf or image-folder input (images sorted by natural filename order)."""
    path = Path(path)
    if input_kind(path) == "pdf":
        import pymupdf

        with pymupdf.open(path) as doc:
            return doc.page_count
    return len(_image_files(path))


def _write_png(data: bytes, dst: Path) -> None:
    """Re-encode arbitrary image bytes (e.g. JPEG from a pdf) as a PNG at dst."""
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(data)) as im:
        im.save(dst, format="PNG")


def extract_page(path: Path, page: int, dst: Path, *, dpi: int = 300) -> None:
    """Write 1-based source page `page` of a pdf/images input to dst as PNG.

    PDFs with one embedded raster per page should export that raster at native resolution
    rather than re-rasterize; otherwise render at `dpi`. Must not hold other pages in memory.
    """
    path = Path(path)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    kind = input_kind(path)
    if kind == "images":
        files = _image_files(path)
        if not 1 <= page <= len(files):
            raise ValueError(f"page {page} outside 1-{len(files)}")
        _write_png(files[page - 1].read_bytes(), dst)
        return

    import pymupdf

    with pymupdf.open(path) as doc:
        if not 1 <= page <= doc.page_count:
            raise ValueError(f"page {page} outside 1-{doc.page_count}")
        p = doc[page - 1]
        # Native path: the page is exactly one raster covering it (a plain scan). Only for
        # unrotated pages so the extracted image matches what the reader displays.
        images = p.get_images(full=True)
        rects = p.get_image_rects(images[0][0]) if p.rotation == 0 and len(images) == 1 else []
        if len(rects) == 1 and rects[0].width >= p.rect.width - 1 and rects[0].height >= p.rect.height - 1:
            raw = doc.extract_image(images[0][0])
            _write_png(raw["image"], dst)
            return
        pix = p.get_pixmap(dpi=dpi)
        pix.save(dst)


def _pages_from_text(text: str) -> list[Page]:
    """Split raw text into pages: form-feed breaks, else runs of blank lines."""
    chunks = text.split("\f") if "\f" in text else re.split(r"\n[ \t]*\n[ \t]*\n*", text)
    chunks = [c.strip("\n") for c in chunks if c.strip()]
    if not chunks:
        raise ValueError("input text is empty")
    return [
        Page(page=i + 1, blocks=[{"id": f"b{i + 1}-1", "type": "prose", "lang": "sa",
                                  "lines": chunk.splitlines(), "align": "center"}])
        for i, chunk in enumerate(chunks)
    ]


def _pages_from_json(data: object) -> list[Page]:
    if isinstance(data, list):
        raw_pages = data
    elif isinstance(data, dict) and "pages" in data:
        raw_pages = data["pages"]  # a master.json
    elif isinstance(data, dict):
        raw_pages = [data]  # a single page
    else:
        raise ValueError("json input must be a page, a list of pages, or a master")
    if not raw_pages:
        raise ValueError("json input has no pages")
    pages = [Page.model_validate(p) for p in raw_pages]
    pages.sort(key=lambda p: p.page)
    return pages


def load_text_pages(path: Path) -> list[Page]:
    """Load a Devanagari .txt (form-feed or blank-line page breaks) or a page-contract .json into Pages.

    A .json may be a single page, a list of pages, or a master.json. Returned pages have lang="sa"
    blocks with no bbox and an empty source_image.
    """
    path = Path(path)
    kind = input_kind(path)
    if kind == "text":
        return _pages_from_text(path.read_text(encoding="utf-8"))
    if kind == "json":
        return _pages_from_json(json.loads(path.read_text(encoding="utf-8")))
    raise ValueError(f"{path}: load_text_pages needs a .txt or .json input, got {kind}")
