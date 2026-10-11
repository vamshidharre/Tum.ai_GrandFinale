// The bidder's ERP (Warenwirtschaft): an article search, each article's record on three tabs (Grunddaten, Bestand,
// Konditionen) and the discount approvals (Freigaben). Unlike variant A, the facts of one article sit on separate
// pages, and the discount approval is kept here rather than in the mailbox.
import { approvals, bidder, materials } from "../world.js"
import { count, germanDate, html, money, page, send, siteUrl } from "./html.js"

const nav = () => [
  ["/", "Artikelsuche"],
  ["/freigaben", "Freigaben"],
  [`${siteUrl("postfach")}/`, "Postfach"],
]
export const view = (title, body, signedIn) =>
  page({ site: `Warenwirtschaft · ${bidder.name}`, title, nav: nav(), body, accent: "#2f4a5b", signedIn })

const tabs = (number, active) =>
  html`<p>${[
    ["", "Grunddaten"],
    ["/bestand", "Bestand"],
    ["/konditionen", "Konditionen"],
  ].map(([path, label]) =>
    label === active ? html`<strong>${label}</strong> · ` : html`<a href="/artikel/${number}${path}">${label}</a> · `,
  )}<a href="/">Zur Artikelsuche</a></p>`

export async function erp(request, response) {
  const url = new URL(request.url, "http://warenwirtschaft")
  if (request.method !== "GET") return send(response, 405, view("Nicht erlaubt", html`<p><a href="/">Zurück</a></p>`))
  if (url.pathname === "/") {
    const query = (url.searchParams.get("q") ?? "").trim().toLowerCase()
    const rows = query
      ? materials.filter((material) => `${material.number} ${material.name}`.toLowerCase().includes(query))
      : []
    return send(
      response,
      200,
      view(
        "Artikelsuche",
        html`<form method="get" action="/"><label>Artikelnummer oder Bezeichnung <input name="q" value="${url.searchParams.get("q") ?? ""}"></label><button type="submit">Suchen</button></form>
        ${query ? html`<p>${rows.length} Treffer</p>` : html`<p>Bitte einen Suchbegriff eingeben (z. B. Bohrung/Hub wie „32/150“ oder „Zylinder“).</p>`}
        <table><thead><tr><th>Artikel</th><th>Bezeichnung</th></tr></thead><tbody>${rows.map(
          (material) => html`<tr><td><a href="/artikel/${material.number}">${material.number}</a></td><td>${material.name}</td></tr>`,
        )}</tbody></table>`,
      ),
    )
  }
  if (url.pathname === "/freigaben")
    return send(
      response,
      200,
      view(
        "Rabattfreigaben",
        html`<p>Von der Vertriebsleitung erteilte Rabattfreigaben je Vergabeverfahren. Es gilt die jüngste Freigabe zum Aktenzeichen.</p>
        <table><thead><tr><th>Datum</th><th>Aktenzeichen</th><th>Rabatt</th><th>Freigegeben von</th><th>Vermerk</th></tr></thead><tbody>${approvals.map(
          (item) =>
            html`<tr><td>${germanDate(item.date)}</td><td>${item.reference}</td><td>${String(item.percent).replace(".", ",")} %</td><td>${item.by}</td><td>${item.note}</td></tr>`,
        )}</tbody></table>`,
      ),
    )
  const match = /^\/artikel\/([\w-]+)(\/bestand|\/konditionen)?$/.exec(url.pathname)
  const material = match && materials.find((item) => item.number === match[1])
  if (!material) return send(response, 404, view("Artikel nicht gefunden", html`<p><a href="/">Zur Artikelsuche</a></p>`))
  const title = `${material.number} · ${material.name}`
  if (match[2] === "/bestand")
    return send(
      response,
      200,
      view(
        title,
        html`${tabs(material.number, "Bestand")}<dl><dt>Frei verfügbarer Bestand</dt><dd>${material.stock.toLocaleString("de-DE")} Stk</dd>
        <dt>Lieferzeit ab Lager</dt><dd>${count(material.stockWeeks, "Woche", "Wochen")}</dd>
        <dt>Fertigungszeit</dt><dd>${count(material.productionWeeks, "Woche", "Wochen")}</dd></dl>`,
      ),
    )
  if (match[2] === "/konditionen")
    return send(
      response,
      200,
      view(
        title,
        html`${tabs(material.number, "Konditionen")}<h2>Preisstaffel (EUR netto je Stück)</h2><table><thead><tr><th>ab Menge</th><th>Preis</th></tr></thead><tbody>${material.prices.map(
          (scale) => html`<tr><td>${scale.from.toLocaleString("de-DE")} Stk</td><td>${money(scale.price)}</td></tr>`,
        )}</tbody></table>`,
      ),
    )
  return send(
    response,
    200,
    view(
      title,
      html`${tabs(material.number, "Grunddaten")}<dl><dt>Status</dt><dd>${material.status}</dd><dt>Bohrung</dt><dd>${material.bore} mm</dd>
      <dt>Hub</dt><dd>${material.stroke} mm</dd><dt>Betriebsdruck</dt><dd>${material.pressure} bar</dd>
      <dt>Befestigung</dt><dd>${material.mounting}</dd><dt>Zertifizierung</dt><dd>${material.certification ?? "keine"}</dd></dl>`,
    ),
  )
}
