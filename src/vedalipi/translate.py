# ABOUTME: Translate stage: turns master.json into <out>/translation.<lang>.json, one claude-cli call per page.
# ABOUTME: Any target language; Sanskrit gets a meaning (and optional gloss), other text a full translation.

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import transliterate
from .ocr.parse import extract_json
from .schema import (
    Block,
    BlockTranslation,
    Manifest,
    Master,
    Page,
    PageTranslation,
    StageRecord,
    Translation,
)

# Part of every page key: bump when the prompt or the payload format changes so pages are re-translated.
TRANSLATE_PROMPT_VERSION = "t3"

GLOSS_MODES = ("none", "key", "all")

# Blocks with no text of their own to translate.
SKIP_TYPES = ("image", "separator", "page_number")

SYSTEM_PROMPT = """You translate one page of a book into the language with ISO 639-1 code "{to}". The book is often a manual of Vedic ritual: Sanskrit mantras with instructions and explanations in another language.

INPUT: a JSON object with "blocks": each has "id", "type" (heading, prose, verse, list_item, table, fill_blank, running_header, image), "lang" (ISO 639-1 code of its text; "sa" is Sanskrit, given in Devanagari) and "lines" (one string per printed line), or "rows" for a table, or "labels" for text printed inside a drawing. "context_before" (when present) is the end of the previous page, for continuity only: do not translate it.

RULES
- Translate faithfully and completely. Never summarize, skip, soften or add content. Keep every instruction exact: which hand, how much water, how many times, facing which direction.
- Non-Sanskrit blocks: translate into natural, clear "{to}". You may re-break lines: give one entry in "lines" per sentence or natural phrase. A sentence cut off at the end of the page stays cut off.
- Sanskrit blocks (lang "sa"): give the MEANING of the mantra in natural "{to}", as one or a few sentences, in "lines". Do not transliterate it; the Sanskrit itself is printed separately. Follow the traditional commentary of the tradition named under BOOK CONTEXT; when it names none, follow the standard traditional (e.g. Sayana) reading. A bare list of divine names (e.g. "केशवाय नमः") becomes one salutation per name and line, in "{to}".
- CUE: every Sanskrit block also gets "cue": ONE short line in "{to}" that is printed just before the mantra and tells the reader what to do while reciting it (the action, the hand, the water, the direction, how many times), ending with a colon. Take it only from the book's own instructions around the mantra on this page or in context_before, whether they come before or after it. Never invent a ritual action the book does not give; when the book gives none, write a plain cue such as "Recite:" in "{to}". Keep it under about twelve words.
- Sanskrit quoted inside a non-Sanskrit sentence (a mantra's first words, a deity's name) is not translated: write it in {mantra_script} letters with diacritics, e.g. "saying oṃ bhūḥ".
- Ritual terms with no exact equivalent (achamana, sankalpa, arghya, tarpana, ...): keep the term in {mantra_script} letters and add a short meaning in parentheses the first time it appears on the page, unless the GLOSSARY gives a rendering; GLOSSARY renderings always win.
- Tables: return "rows" with exactly the same number of rows and cells per row; translate every cell, keep numbers.
- Labels: return "labels" with exactly one string per input label, in order.
- Fill-in blanks (runs of dots "....."): keep the run of dots where it is and translate the words around it.
- Text already in "{to}": copy it unchanged.
{gloss_rule}
OUTPUT: ONLY one JSON object, no prose, no markdown fences:
{{"blocks": [{{"id": "...", "lines": ["..."]}}, {{"id": "...", "cue": "...:", "lines": ["..."]}}, {{"id": "...", "rows": [["..."]]}}, {{"id": "...", "labels": ["..."]}}, ...]}}
Give exactly one entry for every input block id, in input order.
"""

GLOSS_RULES = {
    "none": "",
    "key": (
        '- GLOSS: for the few central mantras of the rite (e.g. the Gayatri), add "gloss": [[word, meaning], ...]'
        " with each Sanskrit word in {mantra_script} letters and its meaning. Other blocks get no gloss.\n"
    ),
    "all": (
        '- GLOSS: for every Sanskrit block, add "gloss": [[word, meaning], ...] with each Sanskrit word in'
        " {mantra_script} letters and its meaning.\n"
    ),
}


class TranslateError(ValueError):
    """The model's answer did not cover the page's blocks in the required shape."""


@dataclass
class TranslateConfig:
    out_dir: Path
    to: str = "en"  # ISO 639-1 code of the target language
    mantra_script: str = "iast"  # vidyut scheme Sanskrit is shown in next to its meaning
    gloss: str = "key"
    context: str = ""  # free text: tradition, commentary to follow, who the book is for
    glossary: dict[str, str] = field(default_factory=dict)  # term -> rendering in the target language
    model: str = "claude-opus-5-5"
    pages: list[int] | None = None  # None: every page of the master
    jobs: int = 4
    force: bool = False  # re-translate pages even when their key is unchanged
    retries: int = 2

    @property
    def path(self) -> Path:
        return translation_path(self.out_dir, self.to)


def translation_path(out_dir: Path, lang: str) -> Path:
    return Path(out_dir) / f"translation.{lang}.json"


def system_prompt(cfg: TranslateConfig) -> str:
    gloss_rule = GLOSS_RULES[cfg.gloss].format(mantra_script=cfg.mantra_script.upper())
    return SYSTEM_PROMPT.format(to=cfg.to, mantra_script=cfg.mantra_script.upper(), gloss_rule=gloss_rule)


def page_payload(page: Page, before: Page | None = None) -> dict:
    """The JSON the model sees for one page: every block with text, plus the previous page's last text block."""
    blocks = []
    for b in page.blocks:
        item = block_item(b)
        if item is not None:
            blocks.append(item)
    payload: dict = {"blocks": blocks}
    if before is not None:
        tail = [item for b in before.blocks if (item := block_item(b)) is not None and "lines" in item]
        if tail:
            payload["context_before"] = tail[-1]["lines"]
    return payload


def block_item(b: Block) -> dict | None:
    """One block as the model sees it, or None when it has nothing to translate.

    Sanskrit labels, tables and running headers are shown transliterated, with no meaning: they are not sent."""
    if b.type == "image":
        labels = [lab.text for lab in b.labels or [] if not lab.is_sanskrit]
        if not labels:
            return None
        return {"id": b.id, "type": "image", "lang": b.lang, "labels": labels}
    if b.type in SKIP_TYPES or (b.type == "running_header" and b.is_sanskrit):
        return None  # a Sanskrit running header is printed transliterated in the page header, with no meaning
    if b.rows is not None:
        if b.is_sanskrit:
            return None
        return {"id": b.id, "type": b.type, "lang": b.lang, "rows": b.rows}
    if not any(line.strip() for line in b.lines):
        return None
    return {"id": b.id, "type": b.type, "lang": b.lang, "lines": b.lines}


def user_prompt(cfg: TranslateConfig, title: str, page: int, payload: dict) -> str:
    parts = [f"BOOK: {title or 'untitled'}, page {page}."]
    if cfg.context.strip():
        parts.append(f"BOOK CONTEXT: {cfg.context.strip()}")
    if cfg.glossary:
        parts.append("GLOSSARY:\n" + "\n".join(f"- {k}: {v}" for k, v in sorted(cfg.glossary.items())))
    parts.append("PAGE:\n" + json.dumps(payload, ensure_ascii=False, indent=1))
    return "\n\n".join(parts)


def source_hash(payload: dict) -> str:
    """Hash of a page's own text. The previous page's tail (context_before) is a hint, not part of it, so an
    edit on one page never re-translates the next."""
    return hashlib.sha256(json.dumps(payload["blocks"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def page_key(cfg: TranslateConfig, title: str, payload: dict) -> str:
    """Hash of the page text and every setting that shapes its translation; a changed key means the page is
    re-translated."""
    h = hashlib.sha256()
    for part in (TRANSLATE_PROMPT_VERSION, cfg.model, cfg.to, cfg.mantra_script, cfg.gloss, cfg.context, title):
        h.update(part.encode())
        h.update(b"\0")
    h.update(json.dumps(cfg.glossary, ensure_ascii=False, sort_keys=True).encode())
    h.update(source_hash(payload).encode())
    return h.hexdigest()


def parse_answer(raw: str, payload: dict, *, gloss: str) -> dict[str, BlockTranslation]:
    """Check the model's answer against the page's blocks and turn it into BlockTranslations."""
    data = extract_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
        raise TranslateError('answer must be a JSON object {"blocks": [...]}')
    got: dict[str, dict] = {}
    for item in data["blocks"]:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            got[item["id"]] = item
    out: dict[str, BlockTranslation] = {}
    for src in payload["blocks"]:
        bid = src["id"]
        item = got.get(bid)
        if item is None:
            raise TranslateError(f"block {bid!r} is missing from the answer")
        if "rows" in src:
            rows = item.get("rows")
            if not (isinstance(rows, list) and len(rows) == len(src["rows"])
                    and all(isinstance(r, list) and len(r) == len(s) for r, s in zip(rows, src["rows"]))):
                raise TranslateError(f"block {bid!r}: rows must have the same shape as the input table")
            out[bid] = BlockTranslation(rows=[[str(c) for c in r] for r in rows])
        elif "labels" in src:
            labels = item.get("labels")
            if not (isinstance(labels, list) and len(labels) == len(src["labels"])):
                raise TranslateError(f"block {bid!r}: give exactly {len(src['labels'])} labels")
            out[bid] = BlockTranslation(labels=[str(x) for x in labels])
        else:
            lines = item.get("lines")
            if isinstance(lines, str):
                lines = [lines]
            if not (isinstance(lines, list) and any(str(x).strip() for x in lines)):
                raise TranslateError(f"block {bid!r}: give a non-empty list of lines")
            bt = BlockTranslation(lines=[str(x) for x in lines])
            if src["lang"] == "sa":
                cue = item.get("cue")
                if not (isinstance(cue, str) and cue.strip()):
                    raise TranslateError(f"block {bid!r}: a Sanskrit block needs a one-line \"cue\"")
                bt.cue = " ".join(cue.split())
            g = item.get("gloss")
            if gloss != "none" and src["lang"] == "sa" and isinstance(g, list):
                pairs = [[str(p[0]), str(p[1])] for p in g if isinstance(p, list) and len(p) == 2]
                bt.gloss = pairs or None
            out[bid] = bt
    return out


def _backend(model: str):
    """The claude CLI, the one model runner vedalipi uses for translation."""
    from .ocr.claude_cli import ClaudeCliBackend

    return ClaudeCliBackend(model=model)


def translate_page(backend, cfg: TranslateConfig, title: str, page: int, payload: dict) -> tuple[dict[str, BlockTranslation], float]:
    """Ask for one page, retrying with the error as feedback while the answer does not fit the page."""
    system, prompt = system_prompt(cfg), user_prompt(cfg, title, page, payload)
    cost, ask, last = 0.0, prompt, ""
    for attempt in range(cfg.retries + 1):
        try:
            ans = backend.ask_text(system, ask)
        except Exception as e:
            e.cost_usd = float(getattr(e, "cost_usd", 0.0) or 0.0) + cost  # type: ignore[attr-defined]
            raise
        cost += ans.cost_usd
        try:
            return parse_answer(ans.text, payload, gloss=cfg.gloss), cost
        except (TranslateError, ValueError) as e:
            last = str(e)
            print(f"[p{page:04d}] translate: unusable answer (attempt {attempt + 1}): {last}", file=sys.stderr)
            ask = (f"{prompt}\n\nYour previous answer could not be used: {last[:500]}\n"
                   'Translate the page again and return ONLY the JSON object {"blocks": [...]}.')
    err = TranslateError(f"page {page}: no usable translation after {cfg.retries + 1} attempts: {last}")
    err.cost_usd = cost  # type: ignore[attr-defined]
    raise err


def translate_master(cfg: TranslateConfig, *, backend=None) -> tuple[Translation, list[tuple[int, str]]]:
    """Translate every selected master page whose key changed (or all with cfg.force) and save the file.

    Pages whose key is unchanged keep their stored translation, hand edits included. Returns the
    translation and the pages that failed as (page, error). Cost is recorded in the manifest.
    """
    if cfg.gloss not in GLOSS_MODES:
        raise ValueError(f"gloss must be one of {GLOSS_MODES}, not {cfg.gloss!r}")
    master = Master.load(cfg.out_dir / "master.json")
    tr = Translation.load(cfg.path) if cfg.path.exists() else Translation(lang=cfg.to, mantra_script=cfg.mantra_script)
    if tr.mantra_script != cfg.mantra_script:
        if tr.pages and cfg.pages is not None:
            # The whole file shares one mantra script: re-translating some pages would leave a mix.
            raise ValueError(f"{cfg.path.name} is in {tr.mantra_script}; change the mantra script for all pages "
                             "(no --pages), or keep it")
        print(f"mantra script changed {tr.mantra_script} -> {cfg.mantra_script}; pages will be re-translated", file=sys.stderr)
        tr.mantra_script = cfg.mantra_script
    wanted = set(cfg.pages) if cfg.pages is not None else {p.page for p in master.pages}
    by_number = {p.page: p for p in master.pages}
    todo: list[tuple[int, dict, str, str]] = []
    for p in sorted(wanted & set(by_number)):
        payload = page_payload(by_number[p], by_number.get(p - 1))
        key = page_key(cfg, master.title, payload)
        old = tr.pages.get(p)
        src = source_hash(payload)
        if not payload["blocks"]:
            tr.pages[p] = PageTranslation(key=key, source=src)
        elif cfg.force or old is None or old.key != key:
            if old is not None and old.key != key:
                print(f"[p{p:04d}] page text or settings changed; re-translating (hand edits to it are dropped)", file=sys.stderr)
            todo.append((p, payload, key, src))
    backend = backend or _backend(cfg.model)
    mpath = cfg.out_dir / "work" / "manifest.json"
    manifest = Manifest.load(mpath) if mpath.exists() else None
    stage = f"translate-{cfg.to}"
    failed: list[tuple[int, str]] = []

    def one(p: int, payload: dict) -> tuple[dict[str, BlockTranslation], float]:
        return translate_page(backend, cfg, master.title, p, payload)

    with ThreadPoolExecutor(max_workers=max(1, cfg.jobs)) as pool:
        futures = {pool.submit(one, p, payload): (p, key, src) for p, payload, key, src in todo}
        for fut in as_completed(futures):
            p, key, src = futures[fut]
            now = datetime.now(UTC).isoformat(timespec="seconds")
            try:
                blocks, cost = fut.result()
            except Exception as e:  # one bad page must not stop the book
                cost = float(getattr(e, "cost_usd", 0.0) or 0.0)
                failed.append((p, f"{type(e).__name__}: {e}"))
                print(f"[p{p:04d}] translate failed: {e}", file=sys.stderr)
                if manifest is not None:
                    prev = manifest.record(p, stage)
                    manifest.set_record(p, stage, StageRecord(
                        status="failed", input_hash=key, spent_usd=(prev.spent_usd if prev else 0.0) + cost,
                        error=str(e)[:500], updated_at=now))
                continue
            tr.pages[p] = PageTranslation(key=key, source=src, blocks=blocks)
            if manifest is not None:
                prev = manifest.record(p, stage)
                manifest.set_record(p, stage, StageRecord(
                    status="done", input_hash=key, cost_usd=cost,
                    spent_usd=(prev.spent_usd if prev else 0.0) + cost, updated_at=now))
            # Saved after every page so an interrupted run keeps what it paid for.
            tr.save(cfg.path)
            if manifest is not None:
                manifest.save(mpath)
    tr.pages = {p: tr.pages[p] for p in sorted(tr.pages) if p in by_number}
    tr.save(cfg.path)
    if manifest is not None:
        manifest.save(mpath)
    return tr, failed


def stale_pages(master: Master, tr: Translation, cfg: TranslateConfig) -> list[int]:
    """Master pages with no translation, or whose text or settings changed since they were translated."""
    by_number = {p.page: p for p in master.pages}
    out = []
    for p, page in by_number.items():
        payload = page_payload(page, by_number.get(p - 1))
        if payload["blocks"] and (p not in tr.pages or tr.pages[p].key != page_key(cfg, master.title, payload)):
            out.append(p)
    return sorted(out)


def _roman_dandas(text: str) -> str:
    return text.replace("॥", "||").replace("।", "|")


def apply_translation(
    master: Master, target: Master, tr: Translation
) -> tuple[Master, dict[tuple[int, str], BlockTranslation], list[tuple[int, str]], list[int]]:
    """Lay a translation over `target` (the Devanagari `master` transliterated into the mantra script).

    Non-Sanskrit blocks and labels are replaced by their translation and take the translation's language;
    Sanskrit stays as transliterated and its meaning is returned as {(page, block id): BlockTranslation}.
    A page whose master text changed since it was translated is stale: none of its stored translation is
    used, because block ids are positions and would pair text with the wrong blocks. Returns the edition,
    the meanings, the (page, block id) of blocks printed untranslated, and the stale pages.
    """
    out = target.model_copy(deep=True)
    by_number = {p.page: p for p in master.pages}
    if transliterate.is_roman(tr.mantra_script):
        # The model sometimes copies Devanagari dandas into a quoted roman mantra; roman text writes | and ||.
        tr = tr.model_copy(deep=True)
        for pt in tr.pages.values():
            for bt in pt.blocks.values():
                bt.lines = [_roman_dandas(x) for x in bt.lines]
                bt.rows = [[_roman_dandas(c) for c in r] for r in bt.rows] if bt.rows is not None else None
                bt.labels = [_roman_dandas(x) for x in bt.labels] if bt.labels is not None else None
                bt.gloss = [[_roman_dandas(x) for x in g] for g in bt.gloss] if bt.gloss is not None else None
    meanings: dict[tuple[int, str], BlockTranslation] = {}
    missing: list[tuple[int, str]] = []
    stale: list[int] = []
    for page in out.pages:
        pt = tr.pages.get(page.page)
        src = by_number.get(page.page)
        if pt is not None and src is not None and pt.source != source_hash(page_payload(src)):
            stale.append(page.page)
            pt = None
        for b in page.blocks:
            if block_item(b) is None:
                continue
            bt = pt.blocks.get(b.id) if pt is not None else None
            if bt is None:
                missing.append((page.page, b.id))
                continue
            if b.type == "image":
                others = [lab for lab in b.labels or [] if not lab.is_sanskrit]
                for label, text in zip(others, bt.labels or []):
                    label.text, label.lang = text, tr.lang
            elif b.is_sanskrit:
                meanings[(page.page, b.id)] = bt
            elif b.rows is not None and bt.rows is not None:
                b.rows, b.lang = bt.rows, tr.lang
            else:
                b.lines, b.lang = bt.lines, tr.lang
    return out, meanings, missing, stale


# Svara marks: where two reads of one mantra most often differ, so they do not tell mantras apart.
_SVARAS = re.compile("[\u0951\u0952\u1CD0-\u1CFF\uA8E0-\uA8F1]")


def mantra_key(block: Block) -> str:
    """What makes two mantras the same for repeat detection: their letters and vowel signs. Spacing,
    dandas, digits, punctuation, symbols and svara marks are left out."""
    text = _SVARAS.sub("", unicodedata.normalize("NFC", "".join(block.lines)))
    return "".join(c for c in text if unicodedata.category(c)[0] in "LM")


def first_meanings(
    master: Master, meanings: dict[tuple[int, str], BlockTranslation]
) -> dict[tuple[int, str], BlockTranslation]:
    """Meanings with every repeat of a mantra reduced to its cue: the meaning and gloss print only where the
    mantra first appears in the book (reading order), so a mantra recited again does not repeat them."""
    out: dict[tuple[int, str], BlockTranslation] = {}
    seen: set[str] = set()
    for page in master.pages:
        for b in page.blocks:
            bt = meanings.get((page.page, b.id))
            if bt is None:
                continue
            key = mantra_key(b)
            if key and key in seen:
                bt = bt.model_copy(update={"lines": [], "gloss": None})
            seen.add(key)
            out[(page.page, b.id)] = bt
    return out
