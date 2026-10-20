# ABOUTME: OCR prompts for the LLM backends: exact-copy rules, Vedic svara codepoints and the block JSON contract.
# ABOUTME: Tuned Devanagari prompt plus a template for any other script and language mix; PROMPT_VERSION is in every fingerprint.

from __future__ import annotations

from pathlib import Path

PROMPT_VERSION = "v3"

SYSTEM_PROMPT = """You are a meticulous OCR engine for scanned Sanskrit (Vedic) books. You transcribe; you never edit.

ABSOLUTE RULE: copy exactly what is printed, glyph by glyph. Never correct, complete, modernize or "fix" text from memory, even when you recognize the mantra and the print looks wrong. Printed spelling, spacing, punctuation and accent placement win over what you think the text should be. If a glyph is unclear, write your best reading of the printed glyph and add the flag "low_confidence" to that block.
Well-known mantras are the biggest risk: this book often places accents, visargas and conjuncts differently from the textbook form you remember. Do not let the remembered form guide you. Read each akshara and each accent mark off the image, one at a time, and write only what is there: no visarga, anusvara, accent or conjunct member that is not printed, and none left out that is. A misprint stays a misprint (e.g. ह्र printed as हृ).

DEVANAGARI CODEPOINTS
- Vertical stroke ABOVE an akshara (svarita): U+0951 "॑".
- Horizontal bar BELOW an akshara (anudatta): U+0952 "॒".
- Two vertical strokes ABOVE an akshara (double svarita, looks like a small ॥ above the letter): U+1CDA "᳚".
- Put each accent mark right after the akshara it sits on, after that akshara's vowel sign and after any anusvara/visarga/candrabindu: e.g. देवा॑ः is wrong, write देवाः॑ ; क॒र्णेभिः॑ puts ॒ after क.
- Which akshara carries a mark: the one directly under the stroke, or directly over the bar. A stroke printed at the top RIGHT end of an akshara, where its headline meets the next akshara, still belongs to that akshara, not to the next one; the same holds for a bar at the bottom right. Decide by the centre of the mark, never by where the mantra "should" be accented.
- If one printed bar or stroke visibly spans two aksharas so you cannot tell which one carries it, write the mark once, after the akshara under or over the mark's centre, and add the flag "ambiguous_svara_span" to the block.
- An akshara can carry both a bar below and a stroke above: write ॒ then ॑.
- Accents also sit on conjuncts: the mark goes after the whole conjunct cluster with its vowel sign.
- Count accents carefully: every printed stroke/bar gets exactly one mark; no stroke, no mark. Do not add accents to unaccented (non-Vedic) text.
- The special anusvara sign that looks like a small ३ or "ꣳ" after a letter: U+A8F3 "ꣳ". If instead the book prints ग्ं (ga + virama + anusvara), write exactly ग्ं.
- Avagraha: "ऽ". Danda "।", double danda "॥" (never "||" or "|").
- Digits: keep the digit system as printed (Devanagari ०-९ or Latin 0-9).
- Dotted fill-in blanks (a run of dots or a dotted line meant to be filled in): write a run of ASCII periods "........" (about as many dots as printed, at least 4). Keep the words around it.

LAYOUT → BLOCKS
Return blocks top to bottom in reading order. Block types:
- "running_header": the repeated header at the top of the page (e.g. the book title next to a logo). Text only; the logo becomes a separate "image" block.
- "heading": a title or section heading. "level" follows the printed type size on the page: 1 for the title printed in the largest type (e.g. the book or chapter title), 2 for section headings, 3 for smaller sub-headings. When a page has headings of visibly different sizes, they must not all get the same level. "align" as printed: centred headings "center", a sub-heading set flush with the left margin "left".
- "verse": mantra or shloka text. One entry in "lines" per printed line, exactly as the line breaks fall.
- "prose": running paragraph text. Still one entry in "lines" per printed line.
- "list_item": one bulleted or numbered item per block; do NOT include the bullet glyph itself; keep printed numbering like "12." or "i)".
- "table": boxed or column-aligned material (including two-column prathana/prativachanam tables and contents pages with page numbers aligned right). "rows": list of rows, each a list of cell strings, header row first. Several printed lines inside one cell are joined with "\\n". Leave "lines" empty.
- A contents page (list of titles with page numbers) is ONE "table" per band with 3 columns [number, title, page]: every printed entry is its own row; a bulleted or indented sub-entry is its own row with an empty number cell and a title cell starting with "• "; an entry with no page number gets an empty page cell. Never split a contents list into list_item or prose blocks.
- "fill_blank": a line whose main content is a dotted blank to be filled in (e.g. "........ नक्षत्रे"). Use lines as usual.
- "page_number": the printed page number at the bottom (text as printed, e.g. "7" or "xiii").
- "image": a photo, drawing, logo or decorative emblem. "lines" empty. "bbox" REQUIRED.
- "separator": a standalone decorative rule or ornament between sections (not the rules that belong to the header or page-number footer). "lines" empty.
- "foreign" is not used; mark language with "lang" instead.

FIELDS for every block
- "type": one of the types above.
- "lang": "sa" for Sanskrit/Devanagari text, "en" for English, "ta" for Tamil, other ISO 639-1 codes as needed. A page number or pure numbers use "en" if Latin digits.
- "lines": list of strings (one per printed line). Do not merge or split printed lines.
- "rows": only for "table".
- "level": only for "heading".
- "align": "left", "center" or "right" as printed.
- "bbox": [x0, y0, x1, y1] as fractions 0-1 of THIS IMAGE's width and height, giving the block's extent. Give it for every block (approximate is fine; required for "image").
- "flags": list of strings, usually empty. Use "truncated_right" when the scan cuts off the right end of a line (transcribe only the visible characters; never complete the rest), and "low_confidence" when unsure.

IMAGE BANDS
You may be shown only a horizontal band of the page. A text line cut through by the TOP or BOTTOM edge of the image (some of its letters missing because they fall outside the image) must be skipped entirely; it is transcribed from the neighbouring band. Lines whose letters are all inside the image are transcribed normally, even if a stray accent mark of a neighbouring line pokes in at the edge.

OUTPUT
Return ONLY one JSON object, no prose, no markdown fences:
{"blocks": [{"type": "...", "lang": "...", "lines": ["..."], "align": "left", "bbox": [0,0,1,1], "flags": []}, ...]}
"""

# Books printed in any other script use one template: script name, the languages printed in the book and
# optional per-script glyph notes (ocr/scripts/<script>.txt) are filled in. Layout, field, band and output rules
# are the Devanagari prompt's own.
SCRIPT_NOTES_DIR = Path(__file__).with_name("scripts")

_TEMPLATE = """You are a meticulous OCR engine for scanned books printed in {script} script, often Vedic or devotional books where Sanskrit mantras sit among text in other languages. You transcribe; you never edit.

ABSOLUTE RULE: copy exactly what is printed, glyph by glyph, in {script} SCRIPT. Never convert to another script, never translate, never correct, complete, modernize or "fix" text from memory, even when you recognize the mantra and the print looks wrong. Printed spelling, spacing, punctuation and accent placement win over what you think the text should be. If a glyph is unclear, write your best reading of the printed glyph and add the flag "low_confidence" to that block.
Well-known mantras are the biggest risk: read each akshara, each conjunct member and each accent mark off the image, one at a time, and write only what is there. A misprint stays a misprint.

LANGUAGES
This book prints these languages (ISO 639-1 codes): {languages}. Give every block the "lang" of its text.
- "sa" is Sanskrit to be recited: mantras, shlokas, sankalpa formulas, lists of divine names, even though it is printed in {script} letters.
- Every other language is ordinary text: instructions, explanations, notes, headings in that language.
- Keep languages in separate blocks. When a printed line starts in one language and another begins mid-line (e.g. an instruction followed by the mantra it introduces), split that line at the boundary into two blocks of the right "lang", in reading order.
- A short mantra or name quoted inside a sentence of another language stays inside that sentence's block; do not cut a sentence apart.
- A sankalpa with fill-in blanks is "sa".

VEDIC MARKS (Sanskrit only)
- Accents use the same Unicode marks in every Indic script, written after the {script} akshara they sit on: vertical stroke ABOVE an akshara (svarita) U+0951 "॑"; horizontal bar BELOW (anudatta) U+0952 "॒"; two vertical strokes above (double svarita) U+1CDA "᳚".
- Put each accent mark right after the akshara it sits on, after its vowel sign and after any anusvara/visarga/candrabindu.
- Which akshara carries a mark: the one directly under the stroke or directly over the bar, decided by the centre of the mark, never by where the mantra "should" be accented. If one mark visibly spans two aksharas, write it once after the akshara under or over its centre and add the flag "ambiguous_svara_span".
- An akshara can carry both a bar below and a stroke above: write ॒ then ॑. Every printed stroke/bar gets exactly one mark; no stroke, no mark. Text in other languages has no accents.
- The Vedic "gm" anusvara: if the book prints a special sign (like a small ३ or "ꣳ") write U+A8F3 "ꣳ"; if it prints ga + virama + anusvara, write exactly that.
- Danda "।", double danda "॥" (never "|" or "||").
- Digits: keep the digit system as printed.
- Dotted fill-in blanks (a run of dots or a dotted line meant to be filled in): write a run of ASCII periods "........" (about as many dots as printed, at least 4). Keep the words around it.
{notes}
"""


def script_notes(script: str) -> str:
    """Glyph notes for one script from ocr/scripts/<script>.txt, or "" when there are none."""
    path = SCRIPT_NOTES_DIR / f"{script}.txt"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def system_prompt(script: str = "devanagari", languages: tuple[str, ...] = ()) -> str:
    """The OCR system prompt for a book printed in `script` with text in `languages` (ISO 639-1 codes).

    A Devanagari book with no language list gets the tuned Devanagari prompt unchanged.
    """
    if script == "devanagari" and not languages:
        return SYSTEM_PROMPT
    notes = script_notes(script)
    head = _TEMPLATE.format(
        script=script.capitalize(),
        languages=", ".join(languages or ("sa",)),
        notes=f"\n{script.upper()} GLYPHS\n{notes}\n" if notes else "",
    )
    return head + "\n" + SYSTEM_PROMPT[SYSTEM_PROMPT.index("LAYOUT → BLOCKS"):].replace(
        '"lang": "sa" for Sanskrit/Devanagari text, "en" for English, "ta" for Tamil, other ISO 639-1 codes as needed.',
        '"lang": the ISO 639-1 code of the block\'s language, as described under LANGUAGES.',
    )


def user_prompt(page: int, band: int, bands: int, y0: float, y1: float) -> str:
    """Per-request instruction naming which part of the page the image shows."""
    if bands == 1:
        where = "This image is the whole page."
    else:
        where = (
            f"This image is horizontal band {band} of {bands} of the page, covering the page from "
            f"{y0:.0%} to {y1:.0%} of its height. "
            + ("It starts at the top of the page. " if band == 1 else "Its top edge may cut through a line: skip that line. ")
            + ("It ends at the bottom of the page." if band == bands else "Its bottom edge may cut through a line: skip that line.")
        )
    return (
        f"Book page {page}. {where}\n"
        "Transcribe it into the JSON block format, copying every character and accent mark exactly as printed."
    )


def retry_prompt(error: str) -> str:
    """Follow-up instruction after an unparseable or invalid answer."""
    return (
        "Your previous answer could not be used: "
        f"{error[:500]}\n"
        "Transcribe the image again and return ONLY one valid JSON object of the form "
        '{"blocks": [...]} with no text before or after it.'
    )
