// Neutral PDF theme for dossier (pandoc's typst writer imports `conf`).
#let accent = rgb("#1F3864")

#let conf(
  title: none, subtitle: none, authors: (), keywords: (), date: none,
  abstract-title: none, abstract: none, thanks: none, cols: 1,
  margin: (x: 2.2cm, y: 2.4cm), paper: "a4", lang: "en", region: "US",
  font: none, fontsize: 10.5pt, mathfont: none, codefont: none, linestretch: 1,
  sectionnumbering: none, linkcolor: none, citecolor: none, filecolor: none,
  pagenumbering: "1", doc,
) = {
  set document(title: title, keywords: keywords)
  set page(
    paper: paper, margin: margin, numbering: pagenumbering, columns: cols,
    header: context {
      if counter(page).get().first() > 1 and title != none [
        #set text(size: 8.5pt, fill: luma(110))
        #title #h(1fr) #if date != none { date }
      ]
    },
  )
  set par(justify: true, leading: linestretch * 0.65em)
  set text(lang: lang, region: region, size: fontsize)
  set text(font: font) if font != none
  show raw: set text(font: codefont) if codefont != none
  set heading(numbering: sectionnumbering)
  show heading: set text(fill: accent)
  show heading.where(level: 1): it => block(above: 1.4em, below: 0.7em, text(size: 1.45em, it))
  show link: set text(fill: rgb("#1F5FBF"))
  set table(
    stroke: (x, y) => if y == 0 { (bottom: 0.8pt + accent) } else { (bottom: 0.4pt + luma(200)) },
    fill: (x, y) => if y == 0 { accent.lighten(85%) },
  )
  if title != none {
    block(below: 1.6em, width: 100%)[
      #text(size: 2em, weight: "bold", fill: accent, hyphenate: false)[#title]
      #if subtitle != none { parbreak(); text(size: 1.2em, fill: luma(80))[#subtitle] }
      #if authors != none and authors != () {
        parbreak()
        text(fill: luma(60))[#authors.map(a => a.name).join(", ")]
      }
      #if date != none { h(1em); text(fill: luma(100))[#date] }
    ]
  }
  doc
}
