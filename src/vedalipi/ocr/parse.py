# ABOUTME: Turns raw LLM OCR answers into contract Blocks: pulls the JSON out of chatter, repairs small syntax slips,
# ABOUTME: and coerces loose fields (string lines, bad bboxes, stray keys) so one sloppy block never sinks a page.

from __future__ import annotations

import json
import re
import typing

from ..schema import FLAG_LOW_CONFIDENCE, Block, BlockType

BLOCK_TYPES = set(typing.get_args(BlockType))
ALIGNS = {"left", "center", "right"}
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```\s*$")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


class OcrParseError(ValueError):
    """The model's answer held no usable JSON block list."""


def extract_json(raw: str) -> object:
    """Parse the first JSON object/array in raw, tolerating fences, surrounding prose and trailing commas."""
    text = _FENCE.sub("", raw.strip())
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise OcrParseError("no JSON object in answer")
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    if end <= start:
        raise OcrParseError("unterminated JSON in answer")
    candidate = text[start : end + 1]
    decoder = json.JSONDecoder(strict=False)
    err: Exception | None = None
    # Unmodified text first (the decoder stops after the first complete value, so trailing junk is fine);
    # the trailing-comma repair can touch string contents, so it is only a last resort.
    for attempt in (candidate, text[start:], _TRAILING_COMMA.sub(r"\1", candidate)):
        try:
            return decoder.raw_decode(attempt)[0]
        except json.JSONDecodeError as e:
            err = err or e
    raise OcrParseError(f"invalid JSON: {err}")


def _as_lines(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return value.splitlines() or [value]
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def _as_rows(value: object) -> list[list[str]] | None:
    if not isinstance(value, list):
        return None
    rows = []
    for row in value:
        if isinstance(row, list):
            rows.append(["" if c is None else str(c) for c in row])
        elif isinstance(row, dict):
            rows.append([str(c) for c in row.values()])
        elif row is not None:
            rows.append([str(row)])
    return rows


def _as_bbox(value: object) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in value)
    except (TypeError, ValueError):
        return None
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def coerce_block(raw: dict, block_id: str) -> Block:
    """Build a valid Block from one loosely-shaped model block; odd values are fixed and flagged, not rejected."""
    flags = [str(f) for f in _as_lines(raw.get("flags"))]
    btype = str(raw.get("type") or "prose").strip().lower().replace("-", "_").replace(" ", "_")
    if btype not in BLOCK_TYPES:
        flags.append(f"unknown_type:{btype}")
        btype = "prose"
    lang = str(raw.get("lang") or "sa").strip().lower() or "sa"
    lines = _as_lines(raw.get("lines", raw.get("text")))
    rows = _as_rows(raw.get("rows"))
    bbox = _as_bbox(raw.get("bbox"))
    align = str(raw.get("align") or "left").lower()
    if align not in ALIGNS:
        align = "left"
    level = raw.get("level")
    try:
        level = int(level) if level is not None else None
    except (TypeError, ValueError):
        level = None

    if rows is not None and btype != "table":
        btype = "table"
    if btype == "table":
        if not rows:
            rows = [[c.strip() for c in ln.split("|")] for ln in lines]
        lines = []
    else:
        rows = None
    if btype == "heading" and level is None:
        level = 2
    if btype != "heading":
        level = None
    if btype == "image" and bbox is None:
        bbox = [0.0, 0.0, 1.0, 1.0]
        flags.append(FLAG_LOW_CONFIDENCE)
    return Block(
        id=block_id, type=btype, lang=lang, lines=lines, rows=rows, level=level,
        align=align, bbox=bbox, flags=list(dict.fromkeys(flags)),
    )


def parse_blocks(raw: str, id_prefix: str = "b") -> list[Block]:
    """Parse one model answer into Blocks with ids f"{id_prefix}{n}". Raises OcrParseError if unusable."""
    data = extract_json(raw)
    if isinstance(data, dict):
        data = data.get("blocks")
    if not isinstance(data, list):
        raise OcrParseError('answer JSON has no "blocks" list')
    blocks = []
    for item in data:
        if isinstance(item, dict):
            blocks.append(coerce_block(item, f"{id_prefix}{len(blocks) + 1}"))
    if data and not blocks:
        raise OcrParseError("no block objects in blocks list")
    return blocks
