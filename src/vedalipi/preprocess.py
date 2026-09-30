# ABOUTME: Preprocess stage: cleans a scanned page image for OCR (deskew, crop margins, upscale).
# ABOUTME: No binarization; svara strokes are thin and must survive untouched.

from __future__ import annotations

from pathlib import Path


def preprocess_page(src: Path, dst: Path, *, upscale: float = 1.5) -> None:
    """Read src (source.png), deskew and trim scanner borders, upscale by `upscale`, write dst (pre.png).

    Grayscale output. Never binarize or denoise aggressively enough to erase svara marks.
    Normalized bboxes produced later are relative to dst, so crops must not remove page content.
    """
    raise NotImplementedError
