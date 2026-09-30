// ABOUTME: Typst book template for vedalipi: reads data.json (pages -> blocks) and typesets a clean printed-book PDF.
// ABOUTME: Text is only ever read from JSON strings, never parsed as markup; master.json is attached to the PDF.

#let data = json("data.json")
#let S = data.style
#let mm = 1mm
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
  let starts-at-top = on.len() > 0 and on.first().location().position().y < (S.margin_top + 8) * mm
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
    parts.map(h => text(font: h.font, lang: h.lang, size: 0.82em, h.text)).join(h(1em))
  }
  // A lone left-aligned header is a centred running title in practice.
  let centre-only = items.all(h => h.align != "right") and items.filter(h => h.align == "center").len() == 0
  let titles = if centre-only {
    align(center + horizon, items.map(h => text(font: h.font, lang: h.lang, size: 0.82em, h.text)).join(h(1.5em)))
  } else {
    grid(columns: (1fr, auto, 1fr), align(left, cell("left")), align(center, cell("center")), align(right, cell("right")))
  }
  block(width: 100%, stroke: (bottom: 0.5pt + gray), inset: (bottom: 4pt))[
    #if logo == none { titles } else {
      // The logo sits at the outer end of the header rule, as in the source; titles stay centred.
      grid(columns: (2.2em, 1fr, 2.2em), align: horizon, [], titles, align(right, image(logo, height: 2em)))
    }
  ]
}

#let page-footer() = context {
  let cov = covering()
  if cov.len() == 0 { return }
  let printed = cov.map(m => if m.value.printed == none { none } else { m.value.printed }).filter(p => p != none)
  let scans = cov.map(m => str(m.value.page))
  let scan-label = latin(size: 0.68em, fill: gray)[
    #if scans.len() == 1 [src p. #scans.first()] else [src pp. #scans.first()–#scans.last()]
  ]
  let middle = if printed.len() > 0 {
    text(size: 0.85em, [— #printed.join(" / ") —])
  } else {
    latin(size: 0.85em)[— #scans.join(" / ") —]
  }
  let odd = calc.odd(here().page())
  grid(
    columns: (1fr, auto, 1fr),
    align(left, if odd { [] } else { scan-label }),
    align(center, middle),
    align(right, if odd { scan-label } else { [] }),
  )
}

// ------------------------------------------------------------ page and text setup

#set page(
  paper: S.paper,
  margin: (inside: S.margin_inside * mm, outside: S.margin_outside * mm, top: S.margin_top * mm, bottom: S.margin_bottom * mm),
  header: page-header(),
  footer: page-footer(),
  header-ascent: 35%,
  footer-descent: 35%,
)
// Full ascender/descender line boxes so stacked subscripts and Vedic marks count toward line height.
#set text(font: data.main.font, lang: data.main.lang, size: S.size * 1pt, hyphenate: false, top-edge: "ascender", bottom-edge: "descender")
#set par(leading: S.leading * 1em, spacing: S.spacing * 1em, justify: false)
#set block(spacing: S.spacing * 1em)
// Keep a danda (and a "॥ १ ॥" verse number) on the line of the word before it; boxing keeps the
// source's plain space in the text layer instead of substituting a no-break space.
#show regex("\\S+(?: [।॥]| [0-9०-९೦-೯౦-౯௦-௯]+ [।॥])+"): it => box(it)

// ------------------------------------------------------------ block renderers

#let al(name) = if name == "center" { center } else if name == "right" { right } else { left }
#let styled(f, body) = text(font: f.font, lang: f.lang, size: f.at("scale", default: 1.0) * 1em, body)
#let join-lines(lines) = lines.map(l => [#l]).join(linebreak())

#let blank(width) = box(width: width * 1em, baseline: 0pt, line(length: 100%, stroke: (paint: luma(60), thickness: 0.9pt, dash: "loosely-dotted")))
#let seg-line(segs) = segs.map(s => if "blank" in s { h(0.15em) + blank(s.blank) + h(0.15em) } else { [#s.t] }).join()

#let heading-size(level) = if level <= 1 { 1.45em } else if level == 2 { 1.22em } else { 1.08em }

// Content of one block for one font source (the target text, or the Devanagari original side by side).
#let body-of(b, src) = {
  let f = (font: src.font, lang: src.lang, scale: src.at("scale", default: 1.0))
  if b.type == "heading" {
    styled(f, text(size: heading-size(b.level), weight: "bold", join-lines(src.lines)))
  } else if b.type == "verse" {
    // One paragraph per printed line; a line too long for the measure wraps with a hanging indent.
    styled(f, src.lines.map(l => par(hanging-indent: 2em, spacing: S.leading * 1em, [#l])).join())
  } else if b.type == "prose" {
    styled(f, par(justify: true, src.lines.join(" ")))
  } else if b.type == "list_item" {
    styled(f, grid(columns: (1.6em, 1fr), column-gutter: 0.2em, [#src.marker], src.lines.join(" ")))
  } else if b.type == "fill_blank" {
    styled(f, src.segs.map(seg-line).join(linebreak()))
  } else if b.type == "table" {
    let n = calc.max(1, ..src.rows.map(r => r.len()))
    let lay = b.table
    styled(f, table(
      columns: lay.widths.map(w => if w == "auto" { auto } else { 1fr }),
      stroke: if lay.ruled { 0.5pt + luma(90) } else { none },
      inset: if lay.ruled { (x: 0.55em, y: 0.5em) } else { (x: 0.4em, y: 0.3em) },
      align: (x, _) => if lay.last_right and x == n - 1 { right + horizon } else { left + horizon },
      ..src.rows.map(r => r.map(c => c.split("\n").map(x => [#x]).join(linebreak())) + range(n - r.len()).map(_ => [])).flatten(),
    ))
  } else if b.type == "foreign" {
    styled(f, join-lines(src.lines))
  } else {
    styled(f, join-lines(src.lines))
  }
}

#let render-image(b) = align(center, block(above: 1.2em, below: 1.2em, if b.image == none {
  box(width: b.width * 100%, height: 3em, stroke: 0.5pt + gray, align(center + horizon, latin(size: 0.75em, fill: gray)[image not available]))
} else {
  image(b.image, width: b.width * 100%)
}))

#let render-block(b) = {
  if b.type == "image" { return render-image(b) }
  if b.type == "separator" { return align(center, block(above: 1em, below: 1em, line(length: 28%, stroke: 0.6pt + gray))) }
  let a = if b.type == "heading" and b.align != "right" { center } else { al(b.align) }
  let space = if b.tight_above or b.tight_below {
    (above: if b.tight_above { 0.55em } else { S.spacing * 1em }, below: if b.tight_below { 0.55em } else { S.spacing * 1em })
  } else if b.type == "heading" { (above: 1.5em, below: 1.1em) } else if b.type == "table" { (above: 1.1em, below: 1.1em) } else { (:) }
  let target = (font: b.font, lang: b.lang, scale: b.scale, lines: b.lines, rows: b.rows, segs: b.at("segs", default: ()), marker: b.at("marker", default: "•"))
  // Short tables stay whole rather than stranding a header row at the foot of a page.
  let whole = b.type == "table" and b.rows.len() <= 10
  if b.orig == none {
    block(width: 100%, breakable: not whole, ..space, align(a, body-of(b, target)))
  } else {
    let o = b.orig
    let orig = (font: o.font, lang: o.lang, scale: o.scale, lines: o.lines, rows: o.rows, segs: o.at("segs", default: ()), marker: o.at("marker", default: "•"))
    let two(x, y) = grid(columns: (1fr, 1fr), column-gutter: 1.6em, align(a, x), align(a, y))
    if b.type == "verse" and o.lines.len() == b.lines.len() {
      // Line-by-line pairing keeps each Devanagari line level with its transliteration.
      block(width: 100%, ..space, grid(
        columns: (1fr, 1fr), column-gutter: 1.6em, row-gutter: S.leading * 1em,
        ..range(b.lines.len()).map(i => (
          align(a, styled(orig, [#o.lines.at(i)])),
          align(a, styled(target, [#b.lines.at(i)])),
        )).flatten()
      ))
    } else {
      block(width: 100%, breakable: not whole, ..space, two(body-of(b, orig), body-of(b, target)))
    }
  }
}

// Marks where a new source page begins; sticky so it never strands at the bottom of an output page.
#let page-start(p, first) = block(sticky: true, above: if first { 0pt } else { 1.3em }, below: 0.9em, width: 100%)[
  #metadata((page: p.page, printed: p.printed, header: p.header, logo: p.logo)) <srcpage>
  #if not first {
    grid(
      columns: (1fr, auto, 1fr), column-gutter: 0.6em, align: horizon,
      line(length: 100%, stroke: 0.3pt + luma(170)),
      latin(size: 0.62em, fill: luma(140))[p. #p.page],
      line(length: 100%, stroke: 0.3pt + luma(170)),
    )
  }
]

// ------------------------------------------------------------ document body

#for (i, p) in data.pages.enumerate() {
  page-start(p, i == 0)
  for b in p.blocks { render-block(b) }
}
