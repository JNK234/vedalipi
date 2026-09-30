# ABOUTME: Preprocess stage: cleans a scanned page image for OCR (deskew, crop margins, upscale).
# ABOUTME: No binarization; svara strokes are thin and must survive untouched.

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

# A scanner border strip is only trimmed when it is uniformly near-black; caps keep
# the trim away from real page content, since later bboxes are relative to the output.
BORDER_MEAN_MAX = 40.0
BORDER_STD_MAX = 8.0
BORDER_TRIM_FRACTION = 0.03
# Deskew is clamped: a bad angle estimate must not rotate a page away.
SKEW_LIMIT_DEG = 10.0


def _imread_gray(src: Path) -> np.ndarray:
    buf = np.fromfile(src, dtype=np.uint8)
    im = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    if im is None:
        raise ValueError(f"could not read image {src}")
    return im


def _imwrite_gray(dst: Path, im: np.ndarray) -> None:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", im)
    if not ok:
        raise ValueError(f"could not encode PNG for {dst}")
    buf.tofile(dst)


def _flatten_illumination(gray: np.ndarray) -> np.ndarray:
    """Divide by a blurred background estimate so uneven scan lighting becomes uniform."""
    k = max(31, int(min(gray.shape) * 0.05) | 1)
    bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    return cv2.divide(gray, bg, scale=255)


def _skew_angle(gray: np.ndarray) -> float:
    """Estimate the text skew in degrees. The threshold here exists only to locate ink
    pixels; it is never written out, so the no-binarization rule for output holds."""
    thr = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    pts = cv2.findNonZero(thr)
    if pts is None or len(pts) < 100:
        return 0.0
    pts = np.ascontiguousarray(pts[:: max(1, len(pts) // 50_000)], dtype=np.int32)  # subsample; minAreaRect is O(n)
    angle = cv2.minAreaRect(pts)[2]
    # Rect angles wrap around (a full-page rect reads as -90); bring into [-45, 45).
    if angle <= -45:
        angle += 90
    elif angle > 45:
        angle -= 90
    return float(np.clip(angle, -SKEW_LIMIT_DEG, SKEW_LIMIT_DEG))


def _rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.05:
        return gray
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=255)


def _trim_borders(gray: np.ndarray) -> np.ndarray:
    """Crop uniform near-black scanner edges, at most BORDER_TRIM_FRACTION per side."""
    h, w = gray.shape
    top, bottom, left, right = 0, h, 0, w

    def uniform_dark(strip: np.ndarray) -> bool:
        return bool(strip.mean() < BORDER_MEAN_MAX and strip.std() < BORDER_STD_MAX)

    cap = int(min(h, w) * BORDER_TRIM_FRACTION)
    while top < cap and uniform_dark(gray[top, :]):
        top += 1
    while h - bottom < cap and uniform_dark(gray[bottom - 1, :]):
        bottom -= 1
    while left < cap and uniform_dark(gray[:, left]):
        left += 1
    while w - right < cap and uniform_dark(gray[:, right - 1]):
        right -= 1
    return gray[top:bottom, left:right]


def preprocess_page(src: Path, dst: Path, *, upscale: float = 1.5) -> None:
    """Read src (source.png), deskew and trim scanner borders, upscale by `upscale`, write dst (pre.png).

    Grayscale output. Never binarize or denoise aggressively enough to erase svara marks.
    Normalized bboxes produced later are relative to dst, so crops must not remove page content.
    """
    gray = _imread_gray(Path(src))
    gray = _flatten_illumination(gray)
    gray = _rotate(gray, _skew_angle(gray))
    gray = _trim_borders(gray)
    # Mild denoise while small: h low enough that thin svara strokes survive.
    gray = cv2.fastNlMeansDenoising(gray, None, h=7, templateWindowSize=7, searchWindowSize=21)
    if upscale != 1.0:
        gray = cv2.resize(gray, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_LANCZOS4)
    _imwrite_gray(Path(dst), gray)
