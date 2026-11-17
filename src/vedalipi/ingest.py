# ABOUTME: Ingest stage: identifies the input kind (PDF, image folder, .txt/.json text, EPUB) and yields source pages.
# ABOUTME: Image inputs become one source.png per page; text inputs (including EPUB) become Page objects that skip OCR entirely.

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from .schema import Page

InputKind = Literal["pdf", "images", "text", "json", "epub"]
# Inputs that are already text: they start the stage chain at ocr.json and never see OCR.
TEXT_KINDS = ("text", "json", "epub")

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
    """Classify the input: a .pdf file, a directory of page images, a Sanskrit .txt, a page-contract .json,
    or an .epub e-book."""
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
    if suffix == ".epub":
        return "epub"
    raise ValueError(f"unsupported input {path}: expected a pdf, image folder, .txt, .json or .epub")


def page_count(path: Path) -> int:
    """Number of source pages for a pdf or image-folder input (images sorted by natural filename order)."""
    path = Path(path)
    if input_kind(path) == "pdf":
        import pymupdf

        with pymupdf.open(path) as doc:
            return doc.page_count
    return len(_image_files(path))


def _write_png(data: bytes, dst: Path) -> None:
    """Re-encode arbitrary image bytes (e.g. JPEG from a pdf) as a PNG at dst, atomically."""
    from io import BytesIO

    from PIL import Image

    tmp = dst.with_name(f".{dst.name}.tmp")
    with Image.open(BytesIO(data)) as im:
        im.save(tmp, format="PNG")
    tmp.replace(dst)


def _raster_only(p) -> int | None:
    """xref of the page's single image when the page is exactly that raster, drawn upright and unflipped
    over the whole page, with no visible text or vector drawing on top; otherwise None."""
    images = p.get_images(full=True)
    if p.rotation != 0 or len(images) != 1:
        return None
    xref = images[0][0]
    placed = p.get_image_rects(xref, transform=True)
    if len(placed) != 1:
        return None
    rect, m = placed[0]
    # Axis-aligned, positive scale: no rotation, skew or mirroring applied by the image matrix.
    if abs(m.b) > 1e-6 or abs(m.c) > 1e-6 or m.a <= 0 or m.d <= 0:
        return None
    if rect.width < p.rect.width - 1 or rect.height < p.rect.height - 1:
        return None
    # Anything drawn over the scan (a caption, a stamp, a rule) exists only in the rendered page.
    # Invisible text (render mode 3, an OCR layer) changes nothing on screen and is allowed.
    if p.get_drawings() or any(span.get("type") != 3 for span in p.get_texttrace()):
        return None
    return xref


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
        # Native path: the page is exactly one untransformed raster covering it (a plain scan), so the
        # extracted image matches what the reader displays at full resolution. Anything else is rendered.
        xref = _raster_only(p)
        if xref is not None:
            _write_png(doc.extract_image(xref)["image"], dst)
            return
        tmp = dst.with_name(f".{dst.name}.tmp.png")
        p.get_pixmap(dpi=dpi).save(tmp)
        tmp.replace(dst)


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


def load_text_pages(
    path: Path, *, assets_dir: Path | None = None, source_script: str = "devanagari", languages: tuple[str, ...] = ()
) -> list[Page]:
    """Load a Sanskrit .txt (form-feed or blank-line page breaks), a page-contract .json, or an .epub into Pages.

    .txt and .json text stays in the script it is written in; the normalize stage converts its Sanskrit from
    --source-script to the Devanagari master, exactly as it does for OCR output.

    A .json may be a single page, a list of pages, or a master.json; its blocks keep their own lang.
    A .txt page is one lang="sa" block with no bbox and an empty source_image. An .epub is read from its
    spine (see ingest_epub): `source_script` and `languages` decide each block's language, and its images
    are written under `assets_dir` as one canvas per page (dropped when None).
    """
    path = Path(path)
    kind = input_kind(path)
    if kind == "text":
        return _pages_from_text(path.read_text(encoding="utf-8"))
    if kind == "json":
        return _pages_from_json(json.loads(path.read_text(encoding="utf-8")))
    if kind == "epub":
        from .ingest_epub import load_epub_pages

        return load_epub_pages(path, assets_dir=assets_dir, source_script=source_script, languages=languages)
    raise ValueError(f"{path}: load_text_pages needs a .txt, .json or .epub input, got {kind}")


def document_title(path: Path) -> str:
    """The book's own title when the input carries one (an EPUB's dc:title), else the file stem."""
    path = Path(path)
    if input_kind(path) == "epub":
        from .ingest_epub import epub_title

        try:
            return epub_title(path) or path.stem
        except (OSError, ValueError, KeyError):
            return path.stem
    return path.stem
