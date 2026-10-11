// The world the four sites show, placed as in the main challenge: the tender and its Leistungsverzeichnis on the
// tender portal, the technical specification on the buyer's customer portal, the bidder's mailbox and its ERP. Change
// anything here, then make BRIEF.md and expected.json agree with it: expected.json is the one answer the agent is
// scored against.

export const bidder = {
  name: "Nordlicht Fluidtechnik AG",
  vatId: "DE811000777",
  mailbox: "angebote@nordlicht-fluid.example",
}

export const tender = {
  reference: "AZ-2027/0815",
  title: "Hydraulikzylinder für die Schleusentore Süd",
  buyer: { name: "Hafenbetriebe Elbstadt", street: "Kaiserkai 7", postalCode: "20457", city: "Elbstadt" },
  procedure: "Offenes Verfahren",
  publishedAt: "2026-10-20",
  deadline: "2026-12-11",
  currency: "EUR",
  positions: [
    { position: "1.1", text: "Torzylinder Obertor, Flansch", quantity: 6, unit: "Stk", requirementRef: "R-01" },
    { position: "1.2", text: "Verriegelungszylinder, Gelenkauge", quantity: 980, unit: "Stk", requirementRef: "R-02" },
    { position: "2.1", text: "Notschließzylinder, Fußbefestigung", quantity: 3, unit: "Stk", requirementRef: "R-03" },
  ],
}

/** The buyer's specification: revision 2 is current; revision 1 is superseded and revision 3 only a draft. */
export const specification = {
  number: "SP-0815",
  title: "Leistungsbeschreibung Hydraulik Schleusentore Süd",
  revisions: [
    {
      revision: "1",
      date: "2026-09-30",
      status: "ersetzt",
      note: "Erstausgabe",
      requirements: [
        { ref: "R-01", bore: 100, stroke: 800, mounting: "Flansch", certification: "DNV" },
        { ref: "R-02", bore: 32, stroke: 120, mounting: "Gelenkauge", certification: null },
        { ref: "R-03", bore: 125, stroke: 1000, mounting: "Fuß", certification: "ATEX" },
      ],
    },
    {
      revision: "2",
      date: "2026-10-18",
      status: "gültig",
      note: "R-02: Hub 150 statt 120 mm",
      requirements: [
        { ref: "R-01", bore: 100, stroke: 800, mounting: "Flansch", certification: "DNV" },
        { ref: "R-02", bore: 32, stroke: 150, mounting: "Gelenkauge", certification: null },
        { ref: "R-03", bore: 125, stroke: 1000, mounting: "Fuß", certification: "ATEX" },
      ],
    },
    {
      revision: "3",
      date: "2026-11-02",
      status: "Entwurf (nicht freigegeben)",
      note: "Vorschlag: R-01 Bohrung 110 mm",
      requirements: [
        { ref: "R-01", bore: 110, stroke: 800, mounting: "Flansch", certification: "DNV" },
        { ref: "R-02", bore: 32, stroke: 150, mounting: "Gelenkauge", certification: null },
        { ref: "R-03", bore: 125, stroke: 1000, mounting: "Fuß", certification: "ATEX" },
      ],
    },
  ],
}

/** Documents on the buyer's portal next to the specification: here the quantity change lives (not in the mailbox). */
export const informationDocuments = [
  {
    id: "aenderungsmitteilung-1",
    number: "ÄM-01",
    title: "Änderungsmitteilung 1 zu AZ-2027/0815",
    content:
      "Mengenänderung zum Leistungsverzeichnis: Position 1.2 (Verriegelungszylinder) wird auf 1.040 Stück erhöht. " +
      "Alle übrigen Positionen und Mengen bleiben unverändert.",
  },
  {
    id: "aenderungsmitteilung-x",
    number: "ÄM-07",
    title: "Änderungsmitteilung 7 zu AZ-2027/0816",
    content: "Für das Verfahren AZ-2027/0816 (Pumpwerk Nord) wird Position 1.2 auf 2.000 Stück erhöht.",
  },
  {
    id: "lieferantenleitfaden",
    number: "INF-001",
    title: "Leitfaden für Lieferanten",
    content: "Angebote geben Sie ausschließlich über die Beschaffungsplattform ab.",
  },
]

export const otherTenders = [
  { reference: "AZ-2027/0816", title: "Pumpwerk Nord: Antriebe", buyer: "Hafenbetriebe Elbstadt", deadline: "2026-12-18" },
  { reference: "AZ-2026/0790", title: "Fenderanlage Liegeplatz 4", buyer: "Hafenbetriebe Elbstadt", deadline: "2026-11-27" },
]

/** The bidder's inbox. No discount approval here: approvals are recorded in the ERP (Freigaben). */
export const messages = [
  {
    id: "m-2001",
    date: "2026-10-21T08:40:00Z",
    from: "einkauf@hafen-elbstadt.example",
    subject: "Aufforderung zur Angebotsabgabe AZ-2027/0815",
    body: `Sehr geehrte Damen und Herren,

wir laden Sie ein, ein Angebot für das Verfahren AZ-2027/0815 (Hydraulikzylinder Schleusentore Süd) abzugeben.
Leistungsverzeichnis und Formblätter finden Sie auf der Beschaffungsplattform, die Leistungsbeschreibung und
etwaige Änderungsmitteilungen im Lieferantenportal.

Mit freundlichen Grüßen
Einkauf, Hafenbetriebe Elbstadt`,
  },
  {
    id: "m-2002",
    date: "2026-11-04T16:10:00Z",
    from: "s.weber@nordlicht-fluid.example",
    subject: "AZ-2027/0815 Rabatt?",
    body: `Hi,

der Kunde hat am Telefon 6 % angedeutet. Ich frage bei der Leitung nach, bitte noch nicht verwenden.

Sven`,
  },
  {
    id: "m-2003",
    date: "2026-11-05T07:00:00Z",
    from: "it@nordlicht-fluid.example",
    subject: "Warenwirtschaft: neue Registerkarten",
    body: "In der Warenwirtschaft stehen Bestand und Konditionen eines Artikels jetzt auf eigenen Registerkarten.",
  },
]

/** Discount approvals, recorded in the ERP. The latest approval for exactly this reference counts. */
export const approvals = [
  { date: "2026-10-28", reference: "AZ-2027/0815", percent: 2.5, by: "Leitung Vertrieb", note: "Erstfreigabe" },
  { date: "2026-11-06", reference: "AZ-2027/0815", percent: 4, by: "Leitung Vertrieb", note: "ersetzt Freigabe vom 28.10." },
  { date: "2026-11-07", reference: "AZ-2027/0816", percent: 8, by: "Leitung Vertrieb", note: "" },
]

/** The ERP's articles: attributes and status (Grunddaten), stock and lead times (Bestand), price scale (Konditionen). */
export const materials = [
  {
    number: "ZY-100-800-FL-D", name: "Zylinder 100/800 Flansch DNV", bore: 100, stroke: 800, pressure: 250,
    mounting: "Flansch", certification: "DNV", status: "aktiv", stock: 4, stockWeeks: 3, productionWeeks: 9,
    prices: [{ from: 1, price: 2150 }, { from: 5, price: 2080 }],
  },
  {
    number: "ZY-100-800-FL-D-ALT", name: "Zylinder 100/800 Flansch DNV (Altbestand)", bore: 100, stroke: 800,
    pressure: 250, mounting: "Flansch", certification: "DNV", status: "gesperrt", stock: 20, stockWeeks: 1,
    productionWeeks: 9, prices: [{ from: 1, price: 1900 }],
  },
  {
    number: "ZY-110-800-FL-D", name: "Zylinder 110/800 Flansch DNV", bore: 110, stroke: 800, pressure: 250,
    mounting: "Flansch", certification: "DNV", status: "aktiv", stock: 10, stockWeeks: 3, productionWeeks: 9,
    prices: [{ from: 1, price: 2290 }],
  },
  {
    number: "ZY-32-150-GA", name: "Zylinder 32/150 Gelenkauge", bore: 32, stroke: 150, pressure: 160,
    mounting: "Gelenkauge", certification: null, status: "aktiv", stock: 1500, stockWeeks: 1, productionWeeks: 5,
    prices: [{ from: 1, price: 96.4 }, { from: 500, price: 88.75 }, { from: 1000, price: 84.1 }],
  },
  {
    number: "ZY-32-120-GA", name: "Zylinder 32/120 Gelenkauge", bore: 32, stroke: 120, pressure: 160,
    mounting: "Gelenkauge", certification: null, status: "aktiv", stock: 3000, stockWeeks: 1, productionWeeks: 5,
    prices: [{ from: 1, price: 92 }, { from: 1000, price: 80 }],
  },
  {
    number: "ZY-125-1000-FU-X", name: "Zylinder 125/1000 Fuß ATEX", bore: 125, stroke: 1000, pressure: 250,
    mounting: "Fuß", certification: "ATEX", status: "aktiv", stock: 10, stockWeeks: 2, productionWeeks: 14,
    prices: [{ from: 1, price: 4890 }],
  },
  {
    number: "ZY-125-1000-FU-0", name: "Zylinder 125/1000 Fuß", bore: 125, stroke: 1000, pressure: 250,
    mounting: "Fuß", certification: null, status: "aktiv", stock: 10, stockWeeks: 2, productionWeeks: 14,
    prices: [{ from: 1, price: 4390 }],
  },
]
