# vedalipi

vedalipi turns a Sanskrit book into the same book in another Indic script. The input is a scanned PDF, a folder of page images, or Devanagari text. The output is a typeset PDF in Kannada, Telugu or any other script that [vidyut](https://pypi.org/project/vidyut/) supports. The text is transliterated and never edited. Vedic svaras are kept exactly: svarita U+0951, anudatta U+0952, double svarita U+1CDA, and ꣳ U+A8F3.

## Install

```sh
uv sync                 # Python 3.12; installs everything, including the Typst compiler
uv run vedalipi --help
```

The fonts are vendored in `fonts/` (Noto Serif Kannada, Noto Sans Telugu, Noto Serif Devanagari, Noto Sans Tamil, Noto Serif). Rendering never uses system fonts. At render time the Kannada, Telugu and Devanagari fonts are copied (OFL allows modified versions) into a user cache as "Vedalipi" families whose svara marks are heavier and set clear of the letter, so they read easily at book size; shaping is unchanged. The page follows prapatti.com's stotra PDFs: US Letter, 1.1in side margins, 16.74pt body, 1.42 em line pitch for plain verse and about 2 em for svara-marked text.

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

## Correcting the master and re-rendering

OCR is never trusted blindly. Proofread the flagged blocks against the scan, then fix the text in the master:

```sh
cat > fixes.json <<'EOF'
[
  {"page": 9, "block_id": "b12", "lines": ["ध्रु॒वं ते॒ राजा॒ वरु॑णो ध्रु॒वं दे॒वो बृह॒स्पतिः॑ ।", "..."], "flags": []},
  {"page": 7, "block_id": "b6", "level": 1, "align": "center"}
]
EOF
uv run vedalipi apply-corrections out/book fixes.json
uv run vedalipi render out/book --script kannada
```

A correction can replace `lines` (all lines of the block), `rows` (tables), `flags`, `level` (headings) or `align`. Fields you leave out are unchanged. Block ids come from `master.json` or `vedalipi report`.

Corrections survive re-runs as long as that page is not OCR'd again. If a page's OCR changes, its master page is replaced and a warning is printed. A later re-validation can add a validator flag back to a corrected block, because the validator checks the OCR text, not your correction.

Flags:

- `svara_count_mismatch`: the image shows a different number of svara marks on a line than the text has.
- `reader_disagreement`: the two reads differ on a line (`--cross-read`). This catches marks placed on the wrong akshara, which a count cannot see.
- `invalid_sequence`: an impossible sign order, such as two vowel signs on one letter. This is often a misprint in the book, so it is never auto-corrected.
- `ambiguous_svara_span`: one printed mark spans two aksharas.
- `truncated_right`: the scan cuts off the end of a line. Only the visible text is transcribed.
- `low_confidence`: the OCR model was unsure.
- `roundtrip_fail`: the transliteration does not convert back to the same Devanagari. It is reported in `work/transliterate-<script>.json`.

## Adding a script

Every vidyut script name is accepted by `--script`. A script only needs a font: add one line to `SCRIPTS` in `src/vedalipi/render/fonts.py` and put the font file in `fonts/`. Kannada and Telugu replace U+A8F3, which Noto lacks, with ಗ್ಂ and గ్ం (`DEFAULT_A8F3` in `transliterate.py`).

## Tests

```sh
uv run pytest -q
```
