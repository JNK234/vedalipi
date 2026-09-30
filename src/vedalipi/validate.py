# ABOUTME: Validate stage: cross-checks normalized text against the page image and flags suspect lines.
# ABOUTME: Image-based svara mark counting per line produces "svara_count_mismatch" flags in flags.json.

from __future__ import annotations

from pathlib import Path

from .schema import FlagReport, Page


def validate_page(page: Page, image: Path | None) -> FlagReport:
    """Check a normalized page and return flags per block id.

    With an image: for each sa line, locate its region (block bbox split into lines), count stroke
    marks above the headline and below the baseline, and compare with counts of U+0951+U+1CDA and
    U+0952 in the text; a mismatch adds "svara_count_mismatch". Without an image (text input),
    only text-level checks run (e.g. invalid mark sequences).
    """
    raise NotImplementedError

