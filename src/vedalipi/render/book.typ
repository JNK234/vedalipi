// ABOUTME: Typst book template for vedalipi: reads data.json (pages -> blocks) and typesets a clean printed-book PDF.
// ABOUTME: Text is only ever read from JSON strings, never parsed as markup; master.json is attached to the PDF.

#let data = json("data.json")
#let S = data.style
#let gray = luma(110)

#set document(title: data.title)
#pdf.attach(
  data.attachment,
  relationship: "source",
  mime-type: "application/json",
  description: "vedalipi Devanagari master (source of truth for every script)",
)

// ------------------------------------------------------------ source page tracking

// Source pages whose content appears on the current output page (the one running at the top, then any
// that start on this page).
#let covering() = {
  let here-page = here().page()
  let all = query(<srcpage>)
  let on = all.filter(m => m.location().page() == here-page)
  let before = all.filter(m => m.location().page() < here-page)
  let starts-at-top = on.len() > 0 and on.first().location().position().y < (S.margin_top + 24) * 1pt
  if starts-at-top or before.len() == 0 { on } else { (before.last(),) + on }
}

#let latin(body, ..args) = text(font: data.latin.font, lang: "en", ..args, body)

#let page-header() = context {
  let cov = covering()
  if cov.len() == 0 { return }
  let items = cov.first().value.header
  let logo = cov.first().value.at("logo", default: none)
  if items.len() == 0 and logo == none { return }
  let cell(al) = {
    let parts = items.filter(h => h.align == al)
    if parts.len() == 0 { return [] }
    parts.map(h => text(font: h.font, lang: h.lang, size: 0.72em, h.text)).join(h(1em))
  }
  // A lone left-aligned header is a centred running title in practice.
  let centre-only = items.all(h => h.align != "right") and items.filter(h => h.align == "center").len() == 0
  let titles = if centre-only {
    align(center + horizon, items.map(h => text(font: h.font, lang: h.lang, size: 0.72em, h.text)).join(h(1.5em)))
  } else {
    grid(columns: (1fr, auto, 1fr), align(left, cell("left")), align(center, cell("center")), align(right, cell("right")))
  }
  // A plain running title, no rule, as in prapatti.com's PDFs.
  block(width: 100%)[
    #if logo == none { titles } else {
      // The logo sits at the outer end of the header rule, as in the source; titles stay centred.
      grid(columns: (2.2em, 1fr, 2.2em), align: horizon, [], titles, align(right, image(logo, height: 2em)))
    }
  ]
}

// The footer numbers output pages in one plain sequence, centred; the source scan pages the page draws
// from sit on the right in the same "src p. N" form the mid-page source dividers use. (Printed source
// numbers are left out: one source page spans several output pages, and the book mixes numbering styles.)
#let page-footer() = context {
  let cov = covering()
  let scans = cov.map(m => str(m.value.page))
  let scan-label = if scans.len() == 0 { [] } else {
    latin(size: 0.68em, fill: gray)[
      #if scans.len() == 1 [src p. #scans.first()] else [src pp. #scans.first()–#scans.last()]
    ]
  }
  grid(
    columns: (1fr, auto, 1fr),
    [],
    align(center, latin(size: 0.85em, str(here().page()))),
    align(right, scan-label),
  )
}

// ------------------------------------------------------------ page and text setup

#set page(
  paper: S.paper,
  margin: (left: S.margin_left * 1pt, right: S.margin_right * 1pt, top: S.margin_top * 1pt, bottom: S.margin_bottom * 1pt),
  header: page-header(),
  footer: page-footer(),
  header-ascent: 35%,
  footer-descent: 35%,
)
// Every line box spans the same fixed height, so baselines fall on an even pitch whatever the akshara
// stack (as in a XeTeX book); svara-marked blocks add Vedic leading on top of it (see block-gaps).
#set text(font: data.main.font, lang: data.main.lang, size: S.size * 1pt, hyphenate: false, fill: black,
  top-edge: S.line_box.at(0) * 1em, bottom-edge: -S.line_box.at(1) * 1em)
#set par(leading: S.leading * 1em, spacing: S.leading * 1em, justify: false)
#set block(spacing: (S.leading + S.verse_gap) * 1em)
// Keep a danda (and a "॥ १ ॥" verse number) on the line of the word before it; boxing keeps the
// source's plain space in the text layer instead of substituting a no-break space.
#show regex("\\S+(?: [।॥]| [0-9०-९೦-೯౦-౯௦-௯]+ [।॥])+"): it => box(it)

// ------------------------------------------------------------ block renderers

#let al(name) = if name == "center" { center } else if name == "right" { right } else { left }
// Latin text is scaled down to sit evenly with Indic text; Indic runs quoted inside it (a transliterated
// "Acharya Vandhanam (ಆಚಾರ್ಯ ವನ್ದನಮ್)") are scaled back so they match the Indic body size.
#let indic = regex("[\u{0900}-\u{0DFF}\u{1CD0}-\u{1CFF}\u{A8E0}-\u{A8FF}]+")
#let styled(f, body) = {
  // A book set in one script (a translated edition) has no mixed scripts to even out: every font at 1.0.
  let sc = if S.at("uniform_scale", default: false) { 1.0 } else { f.at("scale", default: 1.0) }
  text(font: f.font, lang: f.lang, size: sc * 1em, if f.lang == "en" and sc != 1.0 {
    show indic: it => text(size: 1em / sc, it)
    body
  } else { body })
}
#let join-lines(lines) = lines.map(l => [#l]).join(linebreak())

#let blank(width) = box(width: width * 1em, baseline: 0pt, line(length: 100%, stroke: (paint: luma(60), thickness: 0.9pt, dash: "loosely-dotted")))
#let seg-line(segs) = segs.map(s => if "blank" in s { h(0.15em) + blank(s.blank) + h(0.15em) } else { [#s.t] }).join()

// Regular weight at every level, as prapatti sets its titles (23.91pt over a 16.74pt body).
#let heading-size(level) = {
  // A style may fix its own heading scale (one size per level, the last one for every deeper level).
  let sizes = S.at("heading_sizes", default: (1.43, 1.2, 1.08))
  sizes.at(calc.min(calc.max(level, 1), sizes.len()) - 1) * 1em
}

// Printed lines keep the source's line breaks: one output line per printed line. A block whose widest
// line overruns `width` at the body size is set smaller, one size for the whole block, never below
// S.min_size; a line still too wide at that size wraps with a hanging indent and is reported (<wrapped>).
// Needs context (for measure); `width` is the absolute width the lines are set in.
#let fit-scale(f, lines, width) = {
  let widest = calc.max(0pt, ..lines.map(l => measure(styled(f, [#l])).width))
  // A hair of slack so rounding never pushes a line that fits just onto a second line.
  let room = width * 0.995
  if widest <= room { 1.0 } else { calc.max(S.min_size / S.size, room / widest) }
}
// Records the size a block was set at and any line that still wraps, for the render log.
#let fit-report(b, f, lines, width, k) = {
  [#metadata((id: b.id, size: k * S.size)) <fitted>]
  for (i, l) in lines.enumerate() {
    if k * measure(styled(f, [#l])).width > width { [#metadata((id: b.id, line: i, text: l, over: k * measure(styled(f, [#l])).width / width)) <wrapped>] }
  }
}
#let printed-lines(b, f, lines, width) = {
  let k = fit-scale(f, lines, width)
  fit-report(b, f, lines, width, k)
  // Leading is in em, so the line pitch (and the wider Vedic pitch) shrinks in proportion with the text.
  text(size: k * 1em, styled(f, lines.map(l => par(hanging-indent: 2em, [#l])).join()))
}

// Content of one block for one font source (the target text, or the Devanagari original side by side),
// set in `width`.
#let body-of(b, src, width) = {
  let f = (font: src.font, lang: src.lang, scale: src.at("scale", default: 1.0))
  if b.type == "heading" {
    styled(f, text(size: heading-size(b.level), join-lines(src.lines)))
  } else if b.type == "prose" and b.at("reflow", default: false) {
    // Translated prose has no printed line breaks to keep: it flows as one paragraph (justified unless the
    // style asks for even word spacing).
    styled(f, par(justify: S.at("justify", default: true), src.lines.join(" ")))
  } else if b.type == "verse" or b.type == "prose" {
    printed-lines(b, f, src.lines, width)
  } else if b.type == "list_item" {
    styled(f, grid(columns: (1.6em, 1fr), column-gutter: 0.2em, [#src.marker], src.lines.join(" ")))
  } else if b.type == "fill_blank" {
    styled(f, src.segs.map(seg-line).join(linebreak()))
  } else if b.type == "table" {
    let n = calc.max(1, ..src.rows.map(r => r.len()))
    let lay = b.table
    let rule = 0.5pt + luma(90)
    let hdr = lay.at("header", default: false)
    // Every cell gets the same line box whatever its font, so baselines line up across a row.
    set text(top-edge: 1.05em, bottom-edge: -0.5em)
    styled(f, table(
      columns: lay.widths.map(w => if w == "auto" { auto } else { 1fr }),
      // A boxed table: outer border, column dividers and a rule under the header row, as printed.
      stroke: if lay.ruled {
        (x, y) => (
          left: rule, right: rule,
          top: if y == 0 or (hdr and y == 1) { rule } else { none },
          bottom: if y == src.rows.len() - 1 { rule } else { none },
        )
      } else { none },
      inset: if lay.ruled { (x: 0.55em, y: 0.45em) } else { (x: 0.4em, y: 0.1em) },
      align: (x, y) => if hdr and y == 0 { center + top } else if lay.last_right and x == n - 1 { right + top } else { left + top },
      ..src.rows.enumerate().map(((y, r)) => r.map(c => {
        let cell = c.split("\n").map(x => [#x]).join(linebreak())
        if hdr and y == 0 { strong(cell) } else { cell }
      }) + range(n - r.len()).map(_ => [])).flatten(),
    ))
  } else if b.type == "foreign" {
    styled(f, join-lines(src.lines))
  } else {
    styled(f, join-lines(src.lines))
  }
}

// A drawing's label, set in the target script over the spot its printed text was whited out of: one line,
// centred both ways in its box. Its size is the one at which its ink just fills the box, capped at the
// drawing's typical label size (the median of those sizes) so the labels of one drawing match; it never
// overflows its box either way. Needs context (for measure).
#let set-label(l, size) = text(font: l.font, lang: l.lang, size: size, top-edge: "bounds", bottom-edge: "bounds", l.text)
#let label-fit(l, w, h) = {
  let m = measure(set-label(l, 10pt))
  if m.width > 0pt and m.height > 0pt { calc.min(w / m.width, h / m.height) * 0.98 * 10pt } else { 10pt }
}
#let label-boxes(labels, w, h) = context {
  let fits = labels.map(l => label-fit(l, l.w * w, l.h * h))
  let sorted = fits.sorted()
  let typical = sorted.at(calc.quo(sorted.len(), 2))
  for (l, k) in labels.zip(fits) {
    place(top + left, dx: l.x * w, dy: l.y * h,
      box(width: l.w * w, height: l.h * h, align(center + horizon, set-label(l, calc.min(k, typical)))))
  }
}

#let render-image(b) = align(center, block(above: 1.2em, below: 1.2em, if b.image == none {
  box(width: b.width * 100%, height: 3em, stroke: 0.5pt + gray, align(center + horizon, latin(size: 0.75em, fill: gray)[image not available]))
} else if b.at("labels", default: ()).len() == 0 {
  image(b.image, width: b.width * 100%)
} else {
  layout(size => {
    let w = size.width * b.width
    let h = w * b.aspect
    box(width: w, height: h, {
      place(top + left, image(b.image, width: w, height: h))
      label-boxes(b.labels, w, h)
    })
  })
}))

#let render-block(b) = context {
  if b.type == "image" { return render-image(b) }
  if b.type == "separator" { return align(center, block(above: 1em, below: 1em, line(length: 28%, stroke: 0.6pt + gray))) }
  // Reflowed (translated) prose is a paragraph of its own language: set left and justified, not as printed.
  // A style may also fix heading alignment per level, so headings of one rank always sit alike.
  // A style may set all body blocks to one alignment too, instead of following the scan block by block.
  let ha = S.at("heading_align", default: none)
  let ba = S.at("body_align", default: none)
  let a = if b.at("reflow", default: false) and b.type == "prose" { left } else if b.type == "heading" and ha != none {
    al(ha.at(calc.min(calc.max(b.level, 1), ha.len()) - 1))
  } else if b.type != "heading" and ba != none { al(ba) } else { al(b.align) }
  // Lines of one block sit at the plain pitch, or the wider Vedic pitch when the block carries svaras;
  // blocks (verses) are set apart by an extra verse gap on top of that.
  let lead = (if b.vedic { S.leading_vedic } else { S.leading }) * 1em
  set par(leading: lead, spacing: lead)
  // A uniform-gap style sets every block apart by the same space, svara-marked or not.
  let gap = if S.at("uniform_gap", default: false) { (S.leading + S.verse_gap) * 1em } else { lead + S.verse_gap * 1em }
  let space = if b.tight_above or b.tight_below {
    (above: if b.tight_above { lead + 0.12em } else { gap }, below: if b.tight_below { lead + 0.12em } else { gap })
  } else if b.type == "heading" { (above: lead + 1.0em, below: lead + 0.8em) } else { (above: gap, below: gap) }
  let target = (font: b.font, lang: b.lang, scale: b.scale, lines: b.lines, rows: b.rows, segs: b.at("segs", default: ()), marker: b.at("marker", default: "•"))
  let full = page.width - (S.margin_left + S.margin_right) * 1pt
  let gutter = 1.6em.to-absolute()
  let half = (full - gutter) / 2
  // Short tables stay whole rather than stranding a header row at the foot of a page.
  let whole = b.type == "table" and b.rows.len() <= 10
  // A heading is sticky: it never strands at the foot of a page away from the block it introduces.
  let sticky = b.type == "heading"
  if b.at("meaning", default: none) != none {
    // A translated edition: the Sanskrit, then its meaning and word gloss, kept together as one unit.
    let m = b.meaning
    let mf = (font: m.font, lang: m.lang, scale: m.scale)
    block(width: 100%, breakable: not whole, sticky: sticky, ..space, {
      align(a, body-of(b, target, full))
      if m.lines.len() > 0 {
        set par(leading: S.leading * 0.62em, spacing: S.leading * 0.62em)
        block(above: 0.55em, below: 0pt, inset: (left: 1.2em), text(size: S.at("meaning_size", default: 0.86) * 1em, style: "italic", styled(mf, join-lines(m.lines))))
      }
      if m.gloss.len() > 0 {
        set par(leading: S.leading * 0.55em)
        block(above: 0.5em, below: 0pt, inset: (left: 1.2em), text(size: S.at("gloss_size", default: 0.78) * 1em, styled(mf,
          m.gloss.map(g => [#text(style: "italic", g.at(0)) — #g.at(1)]).join([; ]))))
      }
    })
  } else if b.orig == none {
    block(width: 100%, breakable: not whole, sticky: sticky, ..space, align(a, body-of(b, target, full)))
  } else {
    let o = b.orig
    let orig = (font: o.font, lang: o.lang, scale: o.scale, lines: o.lines, rows: o.rows, segs: o.at("segs", default: ()), marker: o.at("marker", default: "•"))
    let two(x, y) = grid(columns: (1fr, 1fr), column-gutter: gutter, align(a, x), align(a, y))
    if b.type in ("verse", "prose") and o.lines.len() == b.lines.len() {
      // Line-by-line pairing keeps each Devanagari line level with its transliteration; each side is
      // fitted to its column as a whole, so all its lines share one size.
      let ko = fit-scale(orig, o.lines, half)
      let kt = fit-scale(target, b.lines, half)
      let cell(f, k, l) = text(size: k * 1em, styled(f, par(hanging-indent: 2em, [#l])))
      fit-report(b, orig, o.lines, half, ko)
      fit-report(b, target, b.lines, half, kt)
      block(width: 100%, ..space, grid(
        columns: (1fr, 1fr), column-gutter: gutter, row-gutter: lead,
        ..range(b.lines.len()).map(i => (
          align(a, cell(orig, ko, o.lines.at(i))),
          align(a, cell(target, kt, b.lines.at(i))),
        )).flatten()
      ))
    } else {
      block(width: 100%, breakable: not whole, sticky: sticky, ..space, two(body-of(b, orig, half), body-of(b, target, half)))
    }
  }
}

// Marks where a new source page begins; sticky so it never strands at the bottom of an output page.
// A quiet margin label rather than a rule, keeping the page undecorated.
#let page-start(p, first) = block(sticky: true, above: if first { 0pt } else { 0.9em }, below: 0.4em, width: 100%)[
  #metadata((page: p.page, printed: p.printed, header: p.header, logo: p.logo)) <srcpage>
  #if not first {
    align(right, latin(size: 0.58em, fill: luma(150), top-edge: "cap-height", bottom-edge: "baseline")[src p. #p.page])
  }
]

// ------------------------------------------------------------ document body

#for (i, p) in data.pages.enumerate() {
  page-start(p, i == 0)
  for b in p.blocks { render-block(b) }
}
