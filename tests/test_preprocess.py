# ABOUTME: Tests for preprocess: grayscale output, upscale, border trim, deskew, and that thin strokes survive.

from pathlib import Path

import cv2
import numpy as np

from vedalipi import preprocess
from vedalipi.preprocess import _rotate, _skew_angle, _trim_borders


def write_page(path: Path, h=200, w=300, bg=180, ink=30) -> np.ndarray:
    """A synthetic scan: flat background with one thin horizontal stroke, like a svara mark."""
    im = np.full((h, w), bg, dtype=np.uint8)
    im[h // 2, 50:250] = ink
    cv2.imwrite(str(path), im)
    return im


def test_output_is_grayscale_upscaled_and_stroke_survives(tmp_path):
    src = tmp_path / "source.png"
    im = write_page(src)
    dst = tmp_path / "pre.png"
    preprocess.preprocess_page(src, dst, upscale=1.5)
    out = cv2.imread(str(dst), cv2.IMREAD_GRAYSCALE)
    assert out is not None and out.ndim == 2
    assert out.shape == (int(200 * 1.5), int(300 * 1.5))
    # Thin strokes are ink-dark and everything stays near the background: no binarization.
    mid = out[int(100 * 1.5) - 4 : int(100 * 1.5) + 4, 90:360]
    assert mid.min() < 90
    assert np.percentile(out, 5) > 60


def test_skew_angle_recovers_small_rotation():
    im = np.full((300, 400), 220, dtype=np.uint8)
    cv2.line(im, (30, 150), (370, 150), 20, 3)
    straight = _skew_angle(im)
    rotated = _rotate(im, 3.0)
    est = _skew_angle(rotated)
    assert abs(straight) < 1.0
    assert 1.0 < abs(est) < 10.0


def test_trim_borders_removes_only_uniform_dark_edges():
    im = np.full((100, 100), 200, dtype=np.uint8)
    im[50, 10:90] = 20  # content
    im[:5, :] = 0  # black scanner frame
    im[:, -3:] = 0
    out = _trim_borders(im)
    # Border trim is capped at 3% per side, so 3 of the 5 black rows go.
    assert out.shape == (97, 97)
    assert out[47, 10] == 20


def test_trim_borders_never_touches_content():
    im = np.full((100, 100), 0, dtype=np.uint8)
    im[:, :50] = 200  # half bright, and the dark half is speckled content, not a scanner edge
    im[::10, 50:] = 40
    out = _trim_borders(im)
    assert out.shape == (100, 100)


def test_denoise_does_not_erase_svara_strokes(tmp_path):
    src = tmp_path / "source.png"
    write_page(src, ink=60)  # faint stroke, close to what a light svara stroke looks like
    dst = tmp_path / "pre.png"
    preprocess.preprocess_page(src, dst, upscale=1.0)
    out = cv2.imread(str(dst), cv2.IMREAD_GRAYSCALE)
    assert out[100, 150] < 120
