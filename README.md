# vedalipi

vedalipi turns a Sanskrit book into the same book in another Indic script. The input is a scanned PDF, a folder of page images, Devanagari text, or an EPUB e-book. The output is a typeset PDF, or a reflowable EPUB for an iPad, iPhone or any e-reader, in Kannada, Telugu or any other script that [vidyut](https://pypi.org/project/vidyut/) supports. The text is transliterated and never edited. Vedic svaras are kept exactly: svarita U+0951, anudatta U+0952, double svarita U+1CDA, and ꣳ U+A8F3.

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

# An e-book for iPad, iPhone and other readers (or both PDF and EPUB at once)
uv run vedalipi render out/book --script kannada --format epub
uv run vedalipi run book.pdf --script telugu --out out/book --format both

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
- `<book>.<script>.epub`: the same book as an e-book (`--format epub` or `both`), with `master.json` inside the package.
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

`work/manifest.json` records every stage's input hash, status and cost. A re-run skips a stage when its output exists and the hash of its inputs and settings is unchanged. The settings include the backend, model, prompt version and checker versions. Pages run in parallel (`--jobs`), and only a few page images are held in memory at once. An interrupted run resumes where it stopped. Only one process can use an output directory at a time. Text inputs (.txt, .json, .epub) start the chain at `ocr.json` and never run OCR.

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

A correction can replace `lines` (all lines of the block), `rows` (tables), `flags`, `level` (headings), `align` or `lang` (e.g. `"lang": "sa"` for a block an EPUB import took for Kannada). Fields you leave out are unchanged. Block ids come from `master.json` or `vedalipi report`.

Corrections survive re-runs as long as that page is not OCR'd again. If a page's OCR changes, its master page is replaced and a warning is printed. A later re-validation can add a validator flag back to a corrected block, because the validator checks the OCR text, not your correction.

Flags:

- `svara_count_mismatch`: the image shows a different number of svara marks on a line than the text has.
- `reader_disagreement`: the two reads differ on a line (`--cross-read`). This catches marks placed on the wrong akshara, which a count cannot see.
- `invalid_sequence`: an impossible sign order, such as two vowel signs on one letter. This is often a misprint in the book, so it is never auto-corrected.
- `ambiguous_svara_span`: one printed mark spans two aksharas.
- `truncated_right`: the scan cuts off the end of a line. Only the visible text is transcribed.
- `low_confidence`: the OCR model was unsure.
- `roundtrip_fail`: the transliteration does not convert back to the same Devanagari. It is reported in `work/transliterate-<script>.json`.

## Books in other scripts

A book printed in another Indic script is read in that script and its Sanskrit becomes the Devanagari master:

```sh
uv run vedalipi run book.pdf --source-script telugu --languages te,sa --script kannada --out out/book
```

`--languages` lists the languages printed in the book (ISO 639-1). OCR tags every block with one of them. Sanskrit (`sa`) is converted to Devanagari at normalize; other text keeps its script. The OCR prompt is built from the script name, the languages and optional glyph notes in `src/vedalipi/ocr/scripts/<script>.txt`. Editing a notes file re-reads the affected pages. The image svara count needs Devanagari headlines, so other scripts rely on `--cross-read` for svara checks.

## EPUB as input

An e-book needs no OCR: its text is read straight from the EPUB's spine documents into the same page contract, and the run continues at normalize. Everything after that (master, corrections, translation, rendering to PDF or EPUB in any script) works as for a scan.

```sh
uv run vedalipi run book.epub --script kannada --out out/book                      # a Devanagari e-book
uv run vedalipi run book.epub --source-script telugu --languages te,sa --script devanagari --out out/book
```

How the EPUB is read:

- Headings (`h1`–`h6`, with their level), paragraphs, lists (ordered lists keep their numbers), tables, rules and images become blocks. A paragraph with `<br>` breaks, or a `<div>` holding nothing but one `<p>` per line, is a verse with those printed lines; a long prose paragraph is cut into lines at dandas. Runs of dots make a `fill_blank`.
- Pages: where the publisher kept the printed page breaks (`epub:type="pagebreak"`) they are used, and the printed number becomes the page's `page_number` block. Otherwise each spine document is a page, cut when it grows past about 60 blocks (at a heading once past 40), so one page stays a sensible unit for `--pages`, corrections and translation.
- Language comes from each block's script, not from the file's `lang` attributes (publishers often tag a whole Devanagari book `lang="en"`). Devanagari is Sanskrit. Text in the `--source-script` is Sanskrit too, unless `--languages` lists that script's own language as well (`--languages kn,sa` for a Kannada book with Kannada instructions): then a block with dandas, svaras or visarga-final words is Sanskrit and the rest is Kannada. A `lang` attribute in the file is honoured only when it names one of the `--languages`. Anything the guess gets wrong is one `lang` correction away.
- Images are written to `work/assets/` as one canvas per page and cropped into the output like the drawings of a scan.
- An EPUB that vedalipi wrote carries its Devanagari `master.json`, which is used directly: a Kannada edition re-imported and rendered as Telugu is exact.

## Translated editions

```sh
uv run vedalipi translate out/book --to en --mantra-script iast --gloss key \
    --context "Smarta, Krishna Yajurveda, Apastamba sutra; follow Sayana" --glossary terms.json
uv run vedalipi render out/book --translation en      # <book>.iast.en.pdf
```

`translate` sends each master page to the `claude` CLI and writes `translation.<lang>.json`. Text in other languages is translated. Sanskrit keeps its text in `--mantra-script` (any vidyut scheme), with its meaning, and with `--gloss key` or `all` a word-by-word gloss, set under it. `--context` and `--glossary` (a JSON object of fixed renderings) steer the wording. A page is translated again only when its master text or these settings change, so hand edits to `translation.<lang>.json` survive. Cost is recorded in the manifest under `translate-<lang>`. In roman schemes the svaras print as Latin marks: svarita as a line above (a̍), anudatta as a bar below (a̱) and the double svarita as two lines above (a̎); dandas print as | and ||.

## EPUB for iPad, iPhone and e-readers

`--format epub` (or `both`) writes `<book>.<script>.epub` next to the PDF. It is an EPUB 3 built from the same document tree as the PDF, so it holds exactly the text of the PDF, block for block: every printed line starts a new line (a line too long for the screen wraps with a hanging indent), svara-marked text gets the wider line pitch, fill-in blanks are dotted rules, tables, list markers, separators and image crops are kept, and drawings show their labels in the target script. The edition variants work the same way: `--side-by-side` sets the Devanagari beside each line on a tablet-width screen and stacks the two on a phone, and `--translation LANG` gives the translated edition with its cues, meanings and glosses.

The book is reflowable, so the reader's font size, margins and night theme apply. It is split into chapters at the book's top-level headings (a very long chapter is cut at a source page), with a table of contents from the headings, a page list of the source scan pages, and a quiet "src p. N" label where each scan page begins, as in the PDF. The package validates clean with [EPUBCheck](https://www.w3.org/publishing/epubcheck/).

The fonts are embedded. This matters: the system fonts of an iPad or iPhone cannot place the Vedic marks (U+0951, U+0952, U+1CDA) on Kannada or Telugu letters, so the book embeds the vendored Noto fonts, with the svara-emphasised "Vedalipi" copies for the body text, and tells Apple Books to use them (`ibooks:specified-fonts` and `META-INF/com.apple.ibooks.display-options.xml`). A book with Kannada body text, Telugu instructions and English notes is about 1.5 MB.

To read it on an iPad or iPhone, AirDrop the `.epub` to the device, or put it in iCloud Drive and open it from Files, or send it to yourself and tap it in Mail; it opens in Apple Books. In Apple Books keep the font set to **Original** (the publisher's font): choosing another font replaces the embedded ones, and the svara marks will then sit wrong or show as boxes. Other readers (Kindle via Send to Kindle, Google Play Books, Kobo, Calibre) open the file too, with the same caveat about embedded fonts.

Every chapter of the EPUB is checked to be well-formed before the file is written. `vedalipi judge` checks the PDF only.

## Adding a script

Every vidyut script name is accepted by `--script`. A script only needs a font: add one line to `SCRIPTS` in `src/vedalipi/render/fonts.py` and put the font file in `fonts/`. Kannada and Telugu replace U+A8F3, which Noto lacks, with ಗ್ಂ and గ్ం (`DEFAULT_A8F3` in `transliterate.py`).

## Tests

```sh
uv run pytest -q
# Also run the official EPUB validator over the test editions (needs java and the epubcheck jar):
VEDALIPI_EPUBCHECK=/path/to/epubcheck.jar uv run pytest -q tests/test_epub.py
```
