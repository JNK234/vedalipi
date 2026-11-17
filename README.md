# vedalipi

vedalipi turns a Sanskrit book into the same book in another Indic script. The input is a scanned PDF, a folder of page images, or Devanagari text. The output is a typeset PDF in Kannada, Telugu or any other script that [vidyut](https://pypi.org/project/vidyut/) supports. The text is transliterated and never edited. Vedic svaras are kept exactly: svarita U+0951, anudatta U+0952, double svarita U+1CDA, and ꣳ U+A8F3.

## Install

```sh
uv sync                 # Python 3.12; installs everything, including the Typst compiler
uv run vedalipi --help
```

The fonts are vendored in `fonts/` (Noto Serif Kannada, Noto Sans Telugu, Noto Serif Devanagari, Noto Sans Tamil, Noto Serif). Rendering never uses system fonts. At render time the Kannada, Telugu and Devanagari fonts are copied (OFL allows modified versions) into a user cache as "Vedalipi" families whose svara marks are heavier and set clear of the letter, so they read easily at book size; shaping is unchanged. The page follows prapatti.com's stotra PDFs: US Letter, 1.1in side margins, 16.74pt body, 1.42 em line pitch for plain verse and about 2 em for svara-marked text. All body text is set at 16.74pt, and every printed line of a verse or prose block starts a new output line. A line too wide for the page wraps with a hanging indent and is reported during render.

The default OCR backend runs the local `claude` CLI on your Claude subscription. Log in to Claude Code once; no API key is needed.

## Usage

```sh
# OCR the book, build the Devanagari master, render Kannada
uv run vedalipi run book.pdf --script kannada --out out/book --jobs 6

# Another script: rendered from master.json only, with no OCR
uv run vedalipi render out/book --script telugu
uv run vedalipi render out/book --script kannada --side-by-side   # Devanagari next to the target script

# Stage status, OCR cost, flagged blocks, round-trip failures
uv run vedalipi report out/book

# PASS or CHECK per page; then the lines to check, most svara differences first
uv run vedalipi verify out/book
uv run vedalipi proof out/book
```

Useful `run` options:

- `--pages 1-5,9` limits the run to those pages.
- `--force-stage ocr` redoes a stage even when its cache is valid.
- `--keep-stage ocr` reuses existing OCR even though its settings changed, for example after a prompt edit, instead of paying to read the book again.
- `--cross-read` reads every page a second time and flags lines where the two reads disagree. This about doubles the OCR cost.
- `--backend`, `--model`, `--dpi` and `--upscale` set the OCR backend, model and scan settings.

Outputs in `OUT`:

- `master.json`: the Devanagari master, the single source of truth.
- `<book>.<script>.pdf`: the rendered book, with `master.json` attached to the PDF.
- `work/`: the cache (see below).

## Stages and cache

Each page goes through these stages. Each stage writes its output to `OUT/work/pages/pNNNN/`:

| stage | output | what it does |
|---|---|---|
| ingest | `source.png` | Takes a plain scan's raster at native resolution; any other page is rendered at `--dpi`. |
| preprocess | `pre.png` | Flattens lighting, deskews, trims scanner borders, upscales. It never binarizes. |
| ocr | `ocr.json` | A vision model reads the page in overlapping horizontal bands, and the bands are stitched back together. |
| crossread | `ocr2.json` | Only with `--cross-read`: a second, independent read with different band cuts. |
| normalize | `norm.json` | Converts to NFC and canonical mark order (virama and matra, then nasal marks, then svaras). |
| validate | `flags.json` | Counts svara marks on the image, finds impossible sign orders, and compares the two reads. |

After the stages, `master.json` is built from `norm.json` and `flags.json`. Rendering then transliterates the master and typesets it with Typst.

`work/manifest.json` records every stage's input hash, status and cost. A re-run skips a stage when its output exists and the hash of its inputs and settings is unchanged. The settings include the backend, model, prompt version and checker versions. Pages run in parallel (`--jobs`), and only a few page images are held in memory at once. An interrupted run resumes where it stopped. Only one process can use an output directory at a time.

## OCR backends

| `--backend` | needs | notes |
|---|---|---|
| `claude-cli` (default) | a logged-in `claude` CLI | Runs headless and isolated from your MCP servers, plugins and settings. Default model `claude-opus-5-5`. |
| `anthropic-api` | `ANTHROPIC_API_KEY` | Also offers `read_pages_batch()` (Message Batches, half price) with a resumable checkpoint. |
| `gemini-api` | `GEMINI_API_KEY` | Uses `VEDALIPI_GEMINI_PRICE="in,out"` (USD per 1M tokens) for cost. |
| `tesseract` | `brew install tesseract` plus a Devanagari traineddata | A free, offline cross-check. It does no layout analysis. |

Band settings: `VEDALIPI_OCR_BANDS` (default 3), `VEDALIPI_OCR_OVERLAP`, `VEDALIPI_OCR_MAX_WIDTH` and `VEDALIPI_OCR_RETRIES`.

## Cost

These numbers are for the 24-page test book with `claude-opus-5-5`. With `claude-cli` they are the notional costs the CLI reports; the subscription is what you actually pay.

- OCR averaged about $0.19 per page ($0.12 to $0.44; dense svara pages cost the most).
- `--cross-read` added about $0.25 per page.

`vedalipi report` shows two totals. One is the cost of the OCR currently cached. The other is everything spent, including failed attempts and pages that were read again. Rendering is free and takes a few seconds.

## Proofreading cycle

OCR is never trusted blindly. A book is proofread in a cycle, and every step can be run again:

```sh
uv run vedalipi run book.pdf --cross-read --script kannada --out out/book   # text input: --second-read FILE
uv run vedalipi verify out/book           # PASS or CHECK per page, with the reasons
uv run vedalipi proof out/book            # lines to check, most svara differences first
# edit out/book/corrections.todo.json
uv run vedalipi apply-corrections out/book out/book/corrections.todo.json
uv run vedalipi verify out/book           # what is still CHECK
uv run vedalipi render out/book --script kannada
```

1. `verify` gives each page PASS or CHECK with its reasons (see [Books in other scripts](#books-in-other-scripts)). It exits 1 while any page needs a check.
2. `proof` lists every line that needs a check, with the most svara differences between the two reads first. For each line it shows the page, block, line number and reasons, the line as read (in the book's script), the master line, the second read's line, and the words whose svaras differ. It writes `corrections.todo.json` with one entry per line, ready to edit. Running `proof` again keeps what you already wrote there and drops lines that no longer need a check.
3. In `corrections.todo.json`, check each line against the scan. Fix `text` (the Devanagari master line) when it is wrong, and set `checked` to `true` for every line you have checked, changed or not. To add a line the reader missed, put it after a newline in `text`; an empty `text` removes the line.
4. `apply-corrections` applies the checked lines. A line that was edited but not checked is reported and not applied. Once every listed line of a block is checked, the block loses its check flags.

Corrections can also be written by hand:

```sh
cat > fixes.json <<'END'
[
  {"page": 9, "block_id": "b12", "set_lines": {"2": "ध्रु॒वं ते॒ राजा॒ वरु॑णो ध्रु॒वं दे॒वो बृह॒स्पतिः॑ ।"}, "clear_flags": ["reader_disagreement"]},
  {"page": 7, "block_id": "b6", "level": 1, "align": "center"}
]
END
uv run vedalipi apply-corrections out/book fixes.json
```

A correction can edit single lines (`set_lines`, by line number from 1), replace all lines of a block (`lines`), replace `rows` (tables), `flags`, `clear_flags`, `level` (headings) or `align`. Fields you leave out are unchanged. Block ids come from `master.json`, `vedalipi report` or `vedalipi proof`.

Sanskrit text in a correction is normalized exactly like OCR text when it is applied, so a corrected line follows the same rules as every other line.

The master records every correction it applied. When a page is rebuilt from the same OCR text, because normalize or validate changed, its corrections are applied again and the cause is printed. Lines you edited keep your words, normalized by the current rules; the other lines follow the new normalize. When a page's OCR text itself changed, its corrections no longer fit: they are dropped with a warning and saved in `corrections.dropped.json`. A correction that replaces all `lines` fixes every line of the block to your text, so prefer `set_lines` for single lines.

Flags:

- `svara_count_mismatch`: the image shows a different number of svara marks on a line than the text has.
- `reader_disagreement`: the two reads differ on a line (`--cross-read`). This catches marks placed on the wrong akshara, which a count cannot see.
- `invalid_sequence`: an impossible sign order, such as two vowel signs on one letter. This is often a misprint in the book, so it is never auto-corrected.
- `ambiguous_svara_span`: one printed mark spans two aksharas.
- `truncated_right`: the scan cuts off the end of a line. Only the visible text is transcribed.
- `low_confidence`: the OCR model was unsure.
- `unconverted_letter`: a Sanskrit line, cell or label still holds a letter or sign of another Indic script after conversion to Devanagari, for example a rare Telugu or Kannada letter the converter does not know. The character is kept as read. `vedalipi report` lists each one with its page, block, line and words.
- `non_vedic_mark`: a Sanskrit line holds a combining mark that is neither Devanagari nor Vedic, for example U+0331 (a Latin macron below) read for an anudatta bar. The mark is kept as read, never replaced with a guessed svara; `vedalipi report` lists each one with a total per mark.
- `roundtrip_fail`: the transliteration does not convert back to the same Devanagari. It is reported in `work/transliterate-<script>.json`.

## Books in other scripts

A book printed in another Indic script (Kannada, Telugu, or any script vidyut supports) is read in that script, and its Sanskrit becomes the Devanagari master. The same works for a scan and for text:

```sh
uv run vedalipi run book.pdf --source-script telugu --languages te,sa --script kannada --out out/book
uv run vedalipi run book.txt --source-script kannada --script telugu --out out/book
```

What happens:

- `--languages` lists the languages printed in the book (ISO 639-1), and OCR tags every block with one of them. Sanskrit (`sa`) is converted to Devanagari at normalize; text in other languages keeps its own script.
- A `.txt` input is written in the `--source-script` script, and every page of it is Sanskrit. Use a `.json` input (the page format of `master.json`) when a book also has text in other languages.
- The text is transliterated, never edited. Every svara stays on the letter where the reader saw it, even when another edition puts it elsewhere.
- Scripts without the sign ꣳ spell it with ga, virama and anusvara, sometimes with the ga doubled. Both spellings become one ꣳ in the master, and a svara written after the spelling stays on ꣳ. Ga with an ordinary anusvara is not changed.
- Zero-width joiners and non-joiners are removed from Sanskrit text; they only steer how a font joins letters.
- The OCR prompt is built from the script name, the languages and optional glyph notes in `src/vedalipi/ocr/scripts/<script>.txt`. Editing a notes file re-reads the affected pages.

Checks. The image svara count needs Devanagari headlines, so books in other scripts rely on `--cross-read` (for text input, `--second-read FILE`: an independent reading of the same book in the same script). The second read is converted and normalized exactly like the first before the two are compared. These flags point at text to proofread against the scan:

- `unconverted_letter`: a letter or sign of another script is still in the Sanskrit text after conversion, because the converter does not know it.
- `non_vedic_mark`: a combining mark that is not a Vedic svara, for example a Latin macron below read for an anudatta. It is kept as read and prints as that Latin mark, never as a guessed svara.
- `reader_disagreement`: the two reads give the line differently.
- `single_read`: only one of the two reads has the line, so nothing checked it. When the second read has a line the master lacks, the note quotes it.

`vedalipi verify` gives one verdict per page:

```sh
uv run vedalipi verify out/book             # the script rendered last; or --script telugu
```

A page is PASS, or CHECK with its reasons: the flags above, `low_confidence` (the OCR model itself was unsure), `invalid_sequence`, `svara_count_mismatch`, `roundtrip_fail`, a failed stage, and `output_svara_mismatch`. The last one compares, for every Sanskrit line, the number of svarita, anudatta and double svarita marks and of ꣳ in the source text (the text as read, in the book's script; a hand-corrected block is counted from the master) with the number in the target-script output. Each page line shows these counts as source->output. The exit code is 0 when every page passes and 1 otherwise. Clear a block's flags with a correction once it is proofread; a non-Vedic mark or unconverted letter still in the text keeps the page at CHECK. `verify` reads the output folder and writes nothing; `vedalipi report` has the details, and `vedalipi proof` the lines to check (see [Proofreading cycle](#proofreading-cycle)).

## Translated editions

```sh
uv run vedalipi translate out/book --to en --mantra-script iast --gloss key \
    --context "Smarta, Krishna Yajurveda, Apastamba sutra; follow Sayana" --glossary terms.json
uv run vedalipi render out/book --translation en      # <book>.iast.en.pdf
```

`translate` sends each master page to the `claude` CLI and writes `translation.<lang>.json`. Text in other languages is translated. Sanskrit keeps its text in `--mantra-script` (any vidyut scheme), with its meaning, and with `--gloss key` or `all` a word-by-word gloss, set under it. `--context` and `--glossary` (a JSON object of fixed renderings) steer the wording. A page is translated again only when its master text or these settings change, so hand edits to `translation.<lang>.json` survive. Cost is recorded in the manifest under `translate-<lang>`. In roman schemes the svaras print as Latin marks: svarita as a line above (a̍), anudatta as a bar below (a̱) and the double svarita as two lines above (a̎); dandas print as | and ||.

## Adding a script

Every vidyut script name is accepted by `--script`. A script only needs a font: add one line to `SCRIPTS` in `src/vedalipi/render/fonts.py` and put the font file in `fonts/`. Kannada and Telugu replace U+A8F3, which Noto lacks, with ಗ್ಂ and గ్ం (`DEFAULT_A8F3` in `transliterate.py`).

## Tests

```sh
uv run pytest -q
```
