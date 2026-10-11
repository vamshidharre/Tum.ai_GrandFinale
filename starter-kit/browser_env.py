"""What an agent sees of a browser tab and what it can do there.

`Tab.observe()` renders any web page as accessible text: what a screen reader would announce, with every control
numbered so the model can act on it. Nothing here knows a particular website. It reads labels, descriptions,
tooltips, embedded frames and collapsed sections, drops invisible characters, and leaves every choice of where to go
and what to read to the model.
"""
from __future__ import annotations

import asyncio
import io
import re
import zipfile
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from sanity import same_value

NOT_KEPT = "BUT the field now holds"  # how `fill` reports a field that did not keep what was typed

# Renders one document. Returns {text, next}: `text` with ⟦FRAME:n⟧ markers where embedded documents go, `next` the
# first unused element id. Every control gets data-aid="<id>" so it can be found again.
SNAPSHOT_JS = r"""
(startId) => {
  const ZW = /[​-‏⁠-⁤﻿­͏؜᠎]/g;
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','HEAD','META','LINK','TITLE','OPTION','OPTGROUP',
                        'DATALIST','SOURCE','TRACK','PARAM']);
  const CONTROL = new Set(['INPUT','SELECT','TEXTAREA','BUTTON']);
  let nextId = startId;
  document.querySelectorAll('[data-aid]').forEach(e => e.removeAttribute('data-aid'));
  const described = new Set();
  document.querySelectorAll('[aria-describedby],[aria-labelledby]').forEach(e => {
    ((e.getAttribute('aria-describedby') || '') + ' ' + (e.getAttribute('aria-labelledby') || ''))
      .split(/\s+/).forEach(id => id && described.add(id));
  });
  const clean = s => (s || '').replace(ZW, '').replace(/\s+/g, ' ').trim();
  const cut = (s, n) => s.length > n ? s.slice(0, n) + '…' : s;
  const styleOf = el => getComputedStyle(el);
  const cssHidden = el => { const cs = styleOf(el); return cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse'; };
  const isHidden = el => el.hidden || el.getAttribute('aria-hidden') === 'true' || (el.tagName === 'INPUT' && el.type === 'hidden') || cssHidden(el);
  const pseudo = (el, which) => {
    let c; try { c = getComputedStyle(el, which).content; } catch (e) { return ''; }
    if (!c || c === 'none' || c === 'normal') return '';
    const attr = c.match(/^attr\(([^)]+)\)$/);
    if (attr) return el.getAttribute(attr[1].trim()) || '';
    const quoted = c.match(/^"((?:[^"\\]|\\.)*)"$/) || c.match(/^'((?:[^'\\]|\\.)*)'$/);
    return quoted ? quoted[1].replace(/\\(.)/g, '$1') : '';
  };
  const reversed = el => { const cs = styleOf(el); return cs.direction === 'rtl' && /bidi-override/.test(cs.unicodeBidi); };
  // The text a screen reader would read for a subtree. `force` reads a hidden root (tooltips, descriptions).
  const textOf = (node, force) => {
    if (node.nodeType === 3) return node.nodeValue;
    if (node.nodeType !== 1 || SKIP.has(node.tagName)) return '';
    if (!force && isHidden(node)) return '';
    const label = node.getAttribute('aria-label');
    const role = node.getAttribute('role');
    const tag = node.tagName.toUpperCase();
    if (label && (role === 'img' || tag === 'CANVAS' || tag === 'SVG' || tag === 'IMG')) return ' ' + label + ' ';
    if (tag === 'IMG') return ' ' + (node.getAttribute('alt') || '') + ' ';
    if (tag === 'BR') return '\n';
    if (CONTROL.has(tag) && tag !== 'BUTTON') return '';
    let s = pseudo(node, '::before');
    for (const child of node.childNodes) s += textOf(child, false);
    s += pseudo(node, '::after');
    if (reversed(node)) s = [...s].reverse().join('');
    return s;
  };
  const byIds = ids => clean((ids || '').split(/\s+/).map(id => { const e = id && document.getElementById(id); return e ? textOf(e, true) : ''; }).join(' '));
  const nameOf = el => clean(byIds(el.getAttribute('aria-labelledby')) || el.getAttribute('aria-label') || textOf(el, false) || el.getAttribute('title') || el.getAttribute('value') || '');
  const labelOf = el => {
    const parts = [byIds(el.getAttribute('aria-labelledby')), el.getAttribute('aria-label') || ''];
    if (el.labels) for (const l of el.labels) parts.push(textOf(l, false));
    let s = clean(parts.join(' '));
    if (!s) s = clean(el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '');
    return s;
  };
  const disabled = el => el.disabled === true || el.getAttribute('aria-disabled') === 'true' || !!el.closest('fieldset[disabled],[inert]');
  const dataHints = el => {
    const out = [];
    for (const a of el.attributes) {
      if (!a.name.startsWith('data-') || a.name === 'data-aid' || out.length >= 3) continue;
      out.push(a.value ? a.name + '=' + cut(clean(a.value), 60) : a.name);
    }
    return out.length ? ' {' + out.join(' ') + '}' : '';
  };
  const hrefOf = el => {
    const raw = el.getAttribute('href') || '';
    try { const u = new URL(raw, location.href); return u.origin === location.origin ? u.pathname + u.search + u.hash : u.href; }
    catch (e) { return raw; }
  };
  const q = s => '"' + cut(clean(s), 160).replace(/"/g, "'") + '"';
  const flags = el => (disabled(el) ? ' (disabled)' : '') + (el.required ? ' (required)' : '') + (el.readOnly ? ' (read-only)' : '');
  const descr = el => { const d = byIds(el.getAttribute('aria-describedby')); return d ? ' (described: ' + q(d) + ')' : ''; };
  const tag = (el, collapsed) => { const id = nextId++; el.setAttribute('data-aid', String(id)); return '[' + id + ']' + (collapsed ? ' (in collapsed menu)' : ''); };
  // One control as text, or null when `el` is not a control.
  const control = (el, collapsed) => {
    const t = el.tagName.toUpperCase(), role = el.getAttribute('role') || '', type = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'A' && el.hasAttribute('href')) return tag(el, collapsed) + ' link ' + q(nameOf(el)) + ' -> ' + cut(hrefOf(el), 200) + dataHints(el) + descr(el);
    if (t === 'BUTTON' || (t === 'INPUT' && ['submit', 'button', 'reset', 'image'].includes(type)) || role === 'button') {
      const nv = el.getAttribute('name') ? ' ' + el.getAttribute('name') + '=' + (el.getAttribute('value') || '') : '';
      return tag(el, collapsed) + ' button ' + q(nameOf(el)) + nv + flags(el) + dataHints(el) + descr(el);
    }
    if (t === 'INPUT' && (type === 'checkbox' || type === 'radio'))
      return tag(el, collapsed) + ' ' + type + ' ' + q(labelOf(el)) + (el.checked ? ' [checked]' : ' [unchecked]') + (el.name ? ' name=' + el.name : '') + (el.value && el.value !== 'on' ? ' value=' + el.value : '') + flags(el);
    if (t === 'INPUT' && type === 'range')
      return tag(el, collapsed) + ' slider ' + q(labelOf(el)) + ' value=' + el.value + ' min=' + (el.min || 0) + ' max=' + (el.max || 100) + flags(el);
    if (t === 'INPUT' && type === 'file') return tag(el, collapsed) + ' file-upload ' + q(labelOf(el)) + flags(el);
    if (t === 'INPUT' || t === 'TEXTAREA') {
      const shown = type === 'password' ? (el.value ? '••••' : '') : el.value;
      return tag(el, collapsed) + ' input' + (type && type !== 'text' ? '(' + type + ')' : '') + ' ' + q(labelOf(el)) + ' value=' + q(shown) + (el.placeholder ? ' placeholder=' + q(el.placeholder) : '') + flags(el) + descr(el);
    }
    if (t === 'SELECT') {
      const options = [...el.options].slice(0, 80).map(o => (o.selected ? '*' : '') + (clean(o.textContent) === o.value ? q(o.value) : q(o.value) + '=' + q(o.textContent)));
      return tag(el, collapsed) + ' select ' + q(labelOf(el)) + flags(el) + ' options(* = selected): ' + options.join('; ') + (el.options.length > 80 ? '; …' : '');
    }
    if (t === 'SUMMARY') return tag(el, collapsed) + ' toggle ' + q(nameOf(el)) + (el.parentElement && el.parentElement.open ? ' [open]' : ' [closed]') + flags(el);
    if (['link', 'tab', 'menuitem', 'option', 'checkbox', 'radio', 'switch', 'treeitem'].includes(role) || el.hasAttribute('onclick') || el.isContentEditable && !el.parentElement.isContentEditable)
      return tag(el, collapsed) + ' ' + (role || 'clickable') + ' ' + q(nameOf(el)) + flags(el) + dataHints(el) + descr(el);
    return null;
  };
  const lines = [];
  let cur = '', inline = 0;
  const flush = () => { if (inline) { cur += ' '; return; } const t = cur.replace(ZW, '').replace(/[ \t ]+/g, ' ').trim(); if (t) lines.push(t); cur = ''; };
  const isBlock = el => { const d = styleOf(el).display; return !(d.startsWith('inline') || d === 'contents' || d === 'table-cell' || d === 'ruby'); };
  const isMenu = el => /^(UL|OL|MENU|DIV)$/.test(el.tagName) && !el.hidden && el.getAttribute('aria-hidden') !== 'true'
    && !!el.querySelector('a[href],button,[role=menuitem]')
    && (!!el.closest('nav,[role=navigation],[role=menubar],[role=menu]') || !!(el.previousElementSibling && el.previousElementSibling.matches('button,summary,[aria-haspopup],[aria-expanded]')));
  const walk = (node, collapsed) => {
    if (node.nodeType === 3) { cur += node.nodeValue.replace(ZW, '').replace(/\s+/g, ' '); return; }
    if (node.nodeType !== 1 || SKIP.has(node.tagName)) return;
    const el = node, t = el.tagName.toUpperCase(), role = el.getAttribute('role') || '';
    if (isHidden(el)) {
      if (role === 'tooltip' || (el.id && described.has(el.id))) { if (!(el.id && described.has(el.id))) cur += ' (tooltip: ' + q(textOf(el, true)) + ') '; return; }
      if (!collapsed && cssHidden(el) && isMenu(el)) { flush(); for (const c of el.childNodes) walk(c, true); flush(); }
      return;
    }
    if (t === 'BR') { flush(); return; }
    if (t === 'HR') { flush(); return; }
    if (t === 'IFRAME' || t === 'FRAME') { flush(); const id = nextId++; el.setAttribute('data-aid', String(id)); lines.push('⟦FRAME:' + id + '⟧' + clean(el.getAttribute('title') || el.getAttribute('aria-label') || '')); return; }
    const label = el.getAttribute('aria-label');
    if (label && (role === 'img' || t === 'CANVAS' || t === 'SVG' || t === 'IMG')) { cur += ' ' + (t === 'CANVAS' ? '[canvas: ' + clean(label) + ']' : clean(label)) + ' ' + descr(el); return; }
    if (t === 'CANVAS') { const s = clean(textOf(el, false)); if (s) cur += ' [canvas: ' + s + '] '; return; }
    if (t === 'IMG') { const s = clean(el.getAttribute('alt')); if (s) cur += ' [image: ' + s + '] '; return; }
    if (t === 'SVG') { const s = clean(textOf(el, false)); if (s) cur += ' [graphic: ' + s + '] '; return; }
    if (t === 'LABEL' && (el.control || el.getAttribute('for'))) {
      // A label is announced with its control; print only controls nested inside it.
      el.querySelectorAll('input,select,textarea,button').forEach(c => { if (!isHidden(c)) { const s = control(c, collapsed); if (s) { flush(); cur += s; flush(); } } });
      return;
    }
    const asControl = control(el, collapsed);
    if (asControl) { if (isBlock(el) || t === 'SELECT') { flush(); cur += asControl; flush(); } else cur += ' ' + asControl + ' '; return; }
    const block = isBlock(el);
    if (block) flush();
    if (/^H[1-6]$/.test(t)) cur += '#'.repeat(+t[1]) + ' ';
    else if (t === 'LI') cur += '- ';
    else if (t === 'SUMMARY') cur += '▸ ';
    else if (t === 'CAPTION') cur += 'Table: ';
    else if (t === 'LEGEND') cur += '· ';
    if (role === 'status' || role === 'alert') cur += '(' + role + ') ';
    if (label && block && !['img'].includes(role)) { cur += '— ' + clean(label) + ' —'; flush(); }
    else if (label && !block && !clean(textOf(el, false)).includes(clean(label))) cur += ' {' + clean(label) + '} ';
    if (block && (el.matches('fieldset[disabled]') || el.hasAttribute('inert'))) { cur += '(LOCKED AREA: the controls below are disabled until this area is unlocked)'; flush(); }
    if (t === 'TR') {
      inline++;
      const cells = [];
      for (const cell of el.children) {
        if (isHidden(cell)) continue;
        const before = cur;
        cur = pseudo(cell, '::before');
        for (const c of cell.childNodes) walk(c, collapsed);
        cur += pseudo(cell, '::after') + descr(cell);
        cells.push(cur.replace(/\s+/g, ' ').trim());
        cur = before;
      }
      inline--;
      cur += cells.join(' | '); flush(); return;
    }
    if (t === 'PRE') { const pre = textOf(el, false).replace(ZW, ''); if (inline) cur += pre.replace(/\s+/g, ' '); else { flush(); pre.split('\n').forEach(l => { if (l.trim()) lines.push(l.replace(/\s+$/, '')); }); } return; }
    cur += pseudo(el, '::before');
    if (reversed(el)) cur += clean(textOf(el, false));
    else for (const c of el.childNodes) walk(c, collapsed);
    cur += pseudo(el, '::after');
    if (t === 'TIME' && el.getAttribute('datetime')) cur += ' (datetime=' + el.getAttribute('datetime') + ')';
    if ((t === 'ABBR' || t === 'SPAN' || t === 'TD' || t === 'DFN') && el.getAttribute('title')) cur += ' (title: ' + q(el.getAttribute('title')) + ')';
    if (t === 'DT') cur += ':';
    cur += descr(el);
    if (block) flush();
  };
  const modals = [...document.querySelectorAll('dialog[open],[aria-modal="true"]')].filter(d => !isHidden(d) && (d.tagName !== 'DIALOG' || d.matches(':modal')));
  for (const modal of modals) {
    lines.push('!! A MODAL DIALOG covers the page; nothing behind it works until it is dealt with:');
    walk(modal, false); flush();
    lines.push('!! END OF MODAL DIALOG. The page behind it:');
  }
  const body = document.body || document.documentElement;
  if (modals.length) {
    // A dialog can be nested inside <main>. Hide it while reading the background so it is not rendered a second
    // time with new ids; otherwise the ids reported for its controls no longer point at any element.
    const hidden = modals.map(m => [m, m.getAttribute('aria-hidden')]);
    hidden.forEach(([m]) => m.setAttribute('aria-hidden', 'true'));
    for (const c of body.childNodes) walk(c, false);
    hidden.forEach(([m, value]) => value === null ? m.removeAttribute('aria-hidden') : m.setAttribute('aria-hidden', value));
  } else walk(body, false);
  flush();
  return { text: lines.join('\n'), next: nextId, title: clean(document.title) };
}
"""

# Opens collapsed sections and closes purely informational modal popups (no form controls inside): reading a page
# the model chose should not cost it a click per paragraph. Returns the popups it closed.
PREPARE_JS = r"""
() => {
  let opened = 0; const closed = [];
  document.querySelectorAll('details:not([open])').forEach(d => { d.open = true; opened++; });
  document.querySelectorAll('dialog[open]').forEach(d => {
    if (!d.matches(':modal') || d.querySelector('input:not([type=hidden]),select,textarea')) return;
    closed.push((d.getAttribute('aria-label') || (d.querySelector('h1,h2,h3') || {}).textContent || 'popup').trim().slice(0, 80));
    try { d.close(); } catch (e) {}
  });
  return { opened, closed };
}
"""

LOGIN_JS = r"""
([username, password]) => {
  const visible = el => { const cs = getComputedStyle(el); return !el.hidden && cs.display !== 'none' && cs.visibility !== 'hidden' && el.type !== 'hidden'; };
  const pass = [...document.querySelectorAll('input[type=password]')].find(visible);
  if (!pass) return 'no password field on this page';
  const scope = pass.form || document;
  const fields = [...scope.querySelectorAll('input')].filter(el => visible(el) && ['text', 'email', '', 'tel'].includes((el.getAttribute('type') || '').toLowerCase()));
  const user = fields.filter(el => el.compareDocumentPosition(pass) & Node.DOCUMENT_POSITION_FOLLOWING).pop() || fields[0];
  const set = (el, value) => { el.focus(); el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); };
  if (user) set(user, username);
  set(pass, password);
  pass.setAttribute('data-login-password', '1');
  return 'ok';
}
"""

MAX_OBSERVATION = 14000
# A cookie wall's privacy-preserving choice: a visible control for "necessary only", else "decline", on a page that
# mentions cookies; settings/customise links are not choices. Marks the control and returns its label, or null.
CONSENT_JS = r"""
() => {
  const text = ((document.body && document.body.innerText) || '').toLowerCase();
  if (!text.includes('cookie')) return null;
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && !el.disabled; };
  const label = el => (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
  const controls = [...document.querySelectorAll('button, a, [role=button], input[type=submit], input[type=button]')]
    .filter(visible).filter(el => !/einstellung|setting|anpass|custom|details|mehr|more/i.test(label(el)));
  for (const wanted of [/notwendig|necessary|essential|erforderlich|nur technisch|only required/i,
                        /ablehnen|reject|decline|deny|nicht zustimmen/i]) {
    const el = controls.find(e => wanted.test(label(e)));
    if (el) { el.setAttribute('data-consent-choice', '1'); return label(el).slice(0, 80); }
  }
  return null;
}
"""
# Playwright's own defaults wait 30 s for an element and 30 s for a navigation. On the challenge's flaky portals a
# control that is not there within a few seconds is not coming (scored run a176359f: one click fallback waited the
# full 30 s on a detached frame), and a fresh tab is quicker than waiting (Tab.renew).
ACTION_TIMEOUT_MS, NAVIGATION_TIMEOUT_MS = 8000, 15000
SETTLE_SECONDS = 8  # the longest a page may keep loading data before it is read as it is
# A select value that means "none", and an option (value=label) that offers none, in the languages the forms use.
NONE_VALUE = re.compile(r"^\s*(?:keine?[nrs]?|kein|none|ohne|null|n/?a|without|-+|—|–)\s*$", re.I)
NONE_OPTION = re.compile(r"\b(?:keine?[nrs]?|kein|none|ohne|without)\b", re.I)
LOGIN_WAIT_SECONDS = 8  # after a login is submitted: how long the login form may stay before it counts as not passed
UNREADABLE = "(this document could not be read"
PAGE_BREAK = "===== collected page"  # between the pages of a list or document that `collect` followed to its end


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("p", "div", "tr", "br", "li", "h1", "h2", "h3", "table"):
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def clean_text(text: str) -> str:
    text = re.sub(r"[​-‏⁠-⁤﻿­͏؜᠎]", "", text)
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[  ]+", " ", text)).strip()


def without_ids(text: str) -> str:
    """A document for reading only: the element ids of the pages it came from are gone by now."""
    return re.sub(r"\[\d+\] (?=(?:\(in collapsed menu\) )?[a-z-]+(?:\([a-z]+\))? \")", "", text)


def _xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xlsx_text(archive: zipfile.ZipFile) -> str:
    """Every sheet of a workbook, one line per row, cells separated by " | " (text cells resolved from the
    shared-strings table, where Excel keeps them apart from the rows)."""
    import xml.etree.ElementTree as ET
    names = set(archive.namelist())
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        for item in ET.fromstring(archive.read("xl/sharedStrings.xml")):
            shared.append("".join(node.text or "" for node in item.iter() if _xml_local(node.tag) == "t"))
    sheets = sorted((n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml")),
                    key=lambda n: int(re.sub(r"\D", "", n) or 0))
    parts = []
    for sheet in sheets:
        rows = []
        for row in ET.fromstring(archive.read(sheet)).iter():
            if _xml_local(row.tag) != "row":
                continue
            cells = []
            for cell in row:
                if _xml_local(cell.tag) != "c":
                    continue
                value = next((n.text or "" for n in cell.iter() if _xml_local(n.tag) == "v"), None)
                if cell.get("t") == "s" and value is not None and value.isdigit() and int(value) < len(shared):
                    value = shared[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = "".join(n.text or "" for n in cell.iter() if _xml_local(n.tag) == "t")
                cells.append(value or "")
            if any(cells):
                rows.append(" | ".join(cells))
        parts.append(f"=== SHEET {sheet.rsplit('/', 1)[-1]} ===\n" + "\n".join(rows))
    return "\n".join(parts)


def _docx_text(archive: zipfile.ZipFile) -> str:
    """A Word document's paragraphs, one per line; a table row's cells on one line separated by " | "."""
    import xml.etree.ElementTree as ET
    body = ET.fromstring(archive.read("word/document.xml"))

    def text_of(node) -> str:
        return "".join(n.text or "" for n in node.iter() if _xml_local(n.tag) == "t")

    lines = []
    for node in body.iter():
        tag = _xml_local(node.tag)
        if tag == "tr":
            lines.append(" | ".join(text_of(cell) for cell in node if _xml_local(cell.tag) == "tc"))
        elif tag == "p" and not any(_xml_local(parent.tag) == "tc" for parent in _ancestors(body, node)):
            lines.append(text_of(node))
    return "\n".join(line for line in lines if line.strip())


def _ancestors(root, target) -> list:
    parents = {child: parent for parent in root.iter() for child in parent}
    found, node = [], target
    while node in parents:
        node = parents[node]
        found.append(node)
    return found


def _decode(data: bytes) -> str:
    """UTF-8, else Windows-1252 (what German Excel and many older systems write), so umlauts survive."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def document_text(data: bytes, content_type: str, name: str, depth: int = 0) -> str:
    """The text of a fetched file: archives are opened, Word and Excel files are read as text and table rows, markup
    is stripped, PDFs are read when pypdf is installed."""
    kind = (content_type or "").lower()
    lower = name.lower()
    if data[:4] == b"PK\x03\x04" or "zip" in kind or lower.endswith(".zip"):
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return "(unreadable archive)"
        names = set(archive.namelist())
        try:
            if "xl/workbook.xml" in names:
                return clean_text(f"SPREADSHEET {name}\n" + _xlsx_text(archive))
            if "word/document.xml" in names:
                return clean_text(f"DOCUMENT {name}\n" + _docx_text(archive))
        except Exception:  # noqa: BLE001  (a damaged Office file: fall back to reading it as an archive)
            pass
        parts = [f"ARCHIVE {name} with files: " + ", ".join(info.filename for info in archive.infolist())]
        for info in archive.infolist()[:40]:
            if info.is_dir() or depth > 4:
                continue
            parts.append(f"=== FILE {info.filename} ===\n" + document_text(archive.read(info), "", info.filename, depth + 1))
        return "\n".join(parts)
    if data[:5] == b"%PDF-" or "pdf" in kind:
        try:
            from pypdf import PdfReader
            return clean_text("\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages))
        except Exception as error:  # noqa: BLE001
            return f"(PDF could not be read: {error})"
    text = _decode(data)
    if "html" in kind or "xml" in kind or re.search(r"<(html|body|table|div|p|w:document|worksheet)\b", text[:4000], re.I):
        parser = _Text()
        parser.feed(text)
        text = "".join(parser.parts)
    return clean_text(text)


class Tab:
    """One browser tab on one website, driven by one agent."""

    def __init__(self, page, site: dict):
        self.page = page
        self.site_id = site.get("siteId", "")
        self.base_url = site["url"].rstrip("/")
        self.credentials = site.get("credentials") or {}
        self.frames: list[tuple[int, int, object]] = []  # id range -> frame
        self.document = ""  # what `keep` files: the page as last read, a collected listing or a fetched file
        self.document_source = ""
        self.last_collect: dict | None = None  # how the last `collect` ended: where, control, pages, capped
        self.events: list[str] = []
        self.bad_status = 0
        self.loading: set = set()  # page, frame and data requests that have not come back yet
        self.spares: list = []  # browser contexts of the run's other browsers, for a renewal elsewhere (see renew)
        self.renewals = 0
        self._watch(page)

    def _watch(self, page) -> None:
        for setter, value in (("set_default_timeout", ACTION_TIMEOUT_MS),
                              ("set_default_navigation_timeout", NAVIGATION_TIMEOUT_MS)):
            try:
                getattr(page, setter)(value)
            except Exception:  # noqa: BLE001  a stand-in page in the tests has no such setting
                pass
        page.on("dialog", lambda dialog: asyncio.ensure_future(self._on_dialog(dialog)))
        page.on("response", self._on_response)
        page.on("request", lambda request: self.loading.add(request) if request.resource_type in ("document", "xhr", "fetch") else None)
        page.on("requestfinished", lambda request: self.loading.discard(request))
        page.on("requestfailed", lambda request: self.loading.discard(request))

    async def renew(self) -> str:
        """Swap a broken tab for a fresh one in the same browser: same session and cookies, a clean page. A tab can get
        stuck ("frame was detached", loads that never finish) while the website itself is fine: in sandbox runs the
        investigators' tab kept timing out for minutes while a fresh tab on the same site loaded at once.
        From the second renewal on, the fresh tab opens in another of the run's browsers and logs in again: in sandbox
        runs 41a5de64, c3c4b1e8 and 64397a5e one browser could not load the tender portal for the whole run, fresh
        tabs in it included, while the other browsers kept working."""
        old = self.page
        self.renewals += 1
        moved = self.renewals >= 2 and bool(self.spares)
        context = self.spares[(self.renewals - 2) % len(self.spares)] if moved else old.context
        page = await context.new_page()
        self.page, self.frames, self.loading, self.bad_status = page, [], set(), 0
        self._watch(page)
        try:
            await old.close()
        except Exception:  # noqa: BLE001
            pass
        result = await self.goto(self.base_url + "/")
        if not moved:
            return result
        try:
            result += "; " + await self.login()
        except Exception as error:  # noqa: BLE001
            result += f"; logging in again failed: {str(error)[:120]}"
        return "moved to another browser (this one could not load the website): " + result

    async def screenshot(self) -> str | None:
        """The visible part of the page as a base64 JPEG, for a model that reads images; None when it fails."""
        try:
            import base64
            image = await asyncio.wait_for(self.page.screenshot(type="jpeg", quality=55, full_page=False), 15)
            return base64.b64encode(image).decode("ascii")
        except Exception:  # noqa: BLE001
            return None

    async def _on_dialog(self, dialog):
        self.events.append(f'the browser asked "{dialog.message}" ({dialog.type}); it was accepted')
        try:
            await dialog.accept()
        except Exception:  # noqa: BLE001
            pass

    def _on_response(self, response):
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self.bad_status = response.status if response.status >= 500 else 0
        except Exception:  # noqa: BLE001
            pass

    async def settle(self):
        """Wait until the page, its frames and the data they fetch have arrived and nothing more is on its way."""
        loop = asyncio.get_running_loop()
        end, quiet = loop.time() + SETTLE_SECONDS, None
        await asyncio.sleep(0.1)  # let a click start its request
        while loop.time() < end:
            if self.loading:
                quiet = None
            elif quiet is None:
                quiet = loop.time()
            elif loop.time() - quiet >= 0.2:
                break
            await asyncio.sleep(0.04)
        else:
            self.loading.clear()  # a request that never ends must not hold up every later step
        for frame in list(self.page.frames):
            try:
                await frame.wait_for_load_state("domcontentloaded", timeout=6000 if frame == self.page.main_frame else 3000)
            except Exception:  # noqa: BLE001
                pass
        for _ in range(3):  # a server error page: retry the same address, as a person would press reload
            if not self.bad_status:
                break
            self.events.append(f"the server answered HTTP {self.bad_status}; reloaded")
            await asyncio.sleep(0.4)
            try:
                await self.page.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:  # noqa: BLE001
                break

    async def _frame_text(self, frame, start: int, depth: int) -> tuple[str, int]:
        try:
            prepared = await frame.evaluate(PREPARE_JS)
            if prepared["opened"]:
                await self.settle()  # lazy content of the sections just opened
            for name in prepared["closed"]:
                self.events.append(f'closed the popup "{name}"')
            shot = await frame.evaluate(SNAPSHOT_JS, start)
        except Exception as error:  # noqa: BLE001
            return f"{UNREADABLE}: {str(error)[:120]})", start
        self.frames.append((start, shot["next"], frame))
        text, following = shot["text"], shot["next"]
        for marker in re.findall(r"⟦FRAME:(\d+)⟧", text):
            inner = "(embedded document not loaded)"
            if depth < 4:
                try:
                    handle = await frame.query_selector(f'[data-aid="{marker}"]')
                    child = await handle.content_frame() if handle else None
                    if child:
                        try:
                            await child.wait_for_load_state("domcontentloaded", timeout=4000)
                        except Exception:  # noqa: BLE001
                            pass
                        inner, following = await self._frame_text(child, following, depth + 1)
                except Exception as error:  # noqa: BLE001
                    inner = f"(embedded document could not be read: {str(error)[:120]})"
            block = "┌── embedded document: " if inner.strip() else "┌── embedded document (empty): "
            text = text.replace(f"⟦FRAME:{marker}⟧", block, 1).replace(
                block, block, 1)
            # the marker line carries the frame's title; append the frame's content under it
            lines = text.split("\n")
            for index, line in enumerate(lines):
                if line.startswith(block) and not line.endswith("⁣"):
                    lines[index] = line + "⁣"
                    lines.insert(index + 1, "\n".join("│ " + row for row in inner.split("\n")) + "\n└── end of embedded document")
                    break
            text = "\n".join(lines)
        return text.replace("⁣", ""), following

    async def read(self) -> str:
        """The current page as text, with fresh element ids."""
        for _ in range(4):  # a page that navigates while it is read is read again
            await self.settle()
            self.frames = []
            text, _ = await self._frame_text(self.page.main_frame, 1, 0)
            if not text.startswith(UNREADABLE):
                break
        try:
            title = await self.page.title()
        except Exception:  # noqa: BLE001
            title = ""
        self.document = f"url: {self.page.url}\ntitle: {clean_text(title)}\n{clean_text(text)}"
        self.document_source = self.page.url
        return self.document

    async def observe(self) -> str:
        text = await self.read()
        if len(text) > MAX_OBSERVATION:
            text = text[:MAX_OBSERVATION] + f"\n… (page text cut here; {len(text) - MAX_OBSERVATION} more characters. `keep` files the whole page.)"
        return text

    def take_events(self) -> list[str]:
        events, self.events = self.events, []
        return events

    async def _modal_is_open(self) -> bool:
        """Whether a page-level dialog now prevents ordinary controls from being used."""
        script = """() => {
          const visible = el => { const s = getComputedStyle(el); return !el.hidden && el.getAttribute('aria-hidden') !== 'true' && s.display !== 'none' && s.visibility !== 'hidden'; };
          return [...document.querySelectorAll('dialog[open],[aria-modal="true"]')]
            .some(el => visible(el) && (el.tagName !== 'DIALOG' || el.matches(':modal')));
        }"""
        for frame in self.page.frames:
            try:  # bounded: a frame that is being replaced never answers (run 20e3c020: clicks ran past 20 s)
                if await asyncio.wait_for(frame.evaluate(script), 3):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def choose_necessary_cookies(self) -> str | None:
        """On a cookie wall, choose the privacy-preserving option (only the necessary cookies, else decline), as
        every agent's first or second turn did by hand. Nothing happens on a page that does not mention cookies or
        offers only "accept all": the model deals with it. Returns what was chosen, or None."""
        for frame in list(self.page.frames):
            try:
                label = await asyncio.wait_for(frame.evaluate(CONSENT_JS), 3)
            except Exception:  # noqa: BLE001
                continue
            if not label:
                continue
            try:
                await frame.locator("[data-consent-choice]").first.click(timeout=2500, no_wait_after=True)
            except Exception:  # noqa: BLE001  a click that started a page load is still a click
                pass
            await self.settle()
            return f'chose "{label}" on the cookie notice; now at {self.page.url}'
        return None

    def _locator(self, aid):
        try:
            number = int(aid)
        except (TypeError, ValueError):
            return None
        for start, end, frame in self.frames:
            if start <= number < end:
                return frame.locator(f'[data-aid="{number}"]')
        return None

    async def _describe(self, locator) -> dict:
        info = await self._describe_raw(locator)
        info["name"] = clean_text(info["name"])[:80]  # without the invisible characters pages hide in names
        return info

    async def _describe_raw(self, locator) -> dict:
        return await locator.evaluate(
            """el => ({tag: el.tagName, type: (el.getAttribute('type') || '').toLowerCase(), href: el.href || '',
                      disabled: el.disabled === true || !!el.closest('fieldset[disabled],[inert]'),
                      name: (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().slice(0, 400)})""")

    async def click(self, aid) -> str:
        locator = self._locator(aid)
        if locator is None or await locator.count() == 0:
            return f"there is no element [{aid}] on the page any more; read the current page"
        info = await self._describe(locator)
        if info["disabled"]:
            return f"[{aid}] is disabled (inside a locked or inert area?); nothing happened"
        before = self.page.url
        try:
            await locator.click(timeout=2500, no_wait_after=True)
        except Exception as error:  # noqa: BLE001  hidden in a collapsed menu, or covered: activate it directly
            # A click that started a slow page load is reported as failed although it happened (local run: every
            # login and cookie button; scored run a176359f: a 78 s login). Clicking again would press it twice (a
            # second code request voids the first code), so only a control that is still there, on a page that
            # has not moved, is activated directly.
            if not await self._still_here(locator, before, error):
                await self.settle()
                return f'clicked [{aid}] "{info["name"]}"' + (f"; now at {self.page.url}" if self.page.url != before else "")
            try:
                await locator.evaluate("el => el.click()", timeout=3000)
            except Exception as error:  # noqa: BLE001
                return f"could not click [{aid}]: {str(error)[:150]}"
        await self.settle()
        if await self._modal_is_open():
            return "the page now has a modal dialog; read it before acting behind it"
        moved = self.page.url != before
        return f'clicked [{aid}] "{info["name"]}"' + (f"; now at {self.page.url}" if moved else "")

    async def _still_here(self, locator, before: str, error: Exception) -> bool:
        """After a click that raised: is the control still on an unchanged page, so that the click did not happen?"""
        if self.page.url != before or "navigat" in str(error).lower() or "detached" in str(error).lower():
            return False
        try:
            return await asyncio.wait_for(locator.count(), 2) > 0
        except Exception:  # noqa: BLE001  the page is being replaced
            return False

    async def fill(self, aid, text: str, enter: bool = False) -> str:
        locator = self._locator(aid)
        if locator is None or await locator.count() == 0:
            return f"there is no element [{aid}] on the page any more"
        try:
            await locator.fill(str(text), timeout=2500)
        except Exception as error:  # noqa: BLE001
            return f"could not fill [{aid}]: {str(error)[:150]}"
        readback = await self._readback(locator, str(text))  # before Enter can load another page
        if enter:
            try:
                await locator.press("Enter", no_wait_after=True)
            except Exception:  # noqa: BLE001  Enter started a slow page load (a search): it was pressed
                pass
            await self.settle()
        if await self._modal_is_open():
            return "the page now has a modal dialog; read it before acting behind it"
        return f"filled [{aid}]" + (" and pressed Enter" if enter else "") + readback

    async def _readback(self, locator, typed: str) -> str:
        """What a field holds right after typing, when that is not what was typed: a form can reformat, cut or
        silently refuse a value, and a fill that "worked" proves nothing about what the portal will save."""
        try:
            kind, value = await locator.evaluate(
                """el => [(el.getAttribute('type') || '').toLowerCase(),
                          'value' in el ? String(el.value) : (el.isContentEditable ? el.innerText : null)]""")
        except Exception:  # noqa: BLE001
            return ""
        if kind == "password" or value is None:
            return ""
        shown, wanted = clean_text(value), clean_text(typed)
        if shown == wanted:
            return ""
        if same_value(wanted, shown):
            return f'; the field shows it as "{shown[:80]}"'
        return (f'; {NOT_KEPT} "{shown[:80]}", not what was typed: it changed or refused the value. '
                "Read the field's hint and correct it before you move on")

    async def select(self, aid, value: str) -> str:
        locator = self._locator(aid)
        if locator is None or await locator.count() == 0:
            return f"there is no element [{aid}] on the page any more"
        wanted = str(value)
        for attempt in ({"value": wanted}, {"label": wanted}):
            try:
                await locator.select_option(timeout=2000, **attempt)
                await self.settle()
                if await self._modal_is_open():
                    return "the page now has a modal dialog; read it before acting behind it"
                return f'selected "{wanted}" in [{aid}]'
            except Exception:  # noqa: BLE001
                continue
        options = await locator.evaluate("el => [...(el.options || [])].map(o => o.value + '=' + o.textContent.trim())")
        loose = [o.split("=", 1)[0] for o in options if wanted.lower() in o.lower()]
        if len(loose) == 1:
            await locator.select_option(value=loose[0])
            await self.settle()
            return f'selected "{loose[0]}" in [{aid}]'
        # A value that means "none" takes the list's one option for none, whatever the form calls it (run ddfa36f5:
        # "Keine" and then "none" for an option labelled "Ohne Zertifizierung" cost the clerk two turns).
        if NONE_VALUE.match(wanted):
            nothing = [o.split("=", 1)[0] for o in options if o.split("=", 1)[0].strip() and NONE_OPTION.search(o)]
            if len(nothing) == 1:
                await locator.select_option(value=nothing[0])
                await self.settle()
                return f'selected "{nothing[0]}" in [{aid}] (the list\'s option for none)'
        return f'[{aid}] has no option "{wanted}"; options: {"; ".join(options)[:600]}'

    async def set(self, aid, value) -> str:
        locator = self._locator(aid)
        if locator is None or await locator.count() == 0:
            return f"there is no element [{aid}] on the page any more"
        info = await self._describe(locator)
        if info["disabled"]:
            return f"[{aid}] is disabled; nothing happened"
        try:
            if info["type"] in ("checkbox", "radio"):
                wanted = bool(value)
                try:
                    await locator.set_checked(wanted, timeout=1500)
                except Exception:  # noqa: BLE001  covered or restyled: toggle it as its label would
                    if await locator.is_checked() != wanted:
                        await locator.evaluate("el => el.click()")
                if await locator.is_checked() != wanted:
                    return f"[{aid}] did not change; it is still {'unchecked' if wanted else 'checked'}"
            else:  # a slider or another input whose value is set in one go
                await locator.evaluate(
                    """(el, v) => { const proto = Object.getPrototypeOf(el); const d = Object.getOwnPropertyDescriptor(proto, 'value');
                                   d && d.set ? d.set.call(el, String(v)) : el.value = String(v);
                                   el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }""",
                    value)
        except Exception as error:  # noqa: BLE001
            return f"could not set [{aid}]: {str(error)[:150]}"
        await self.settle()
        if await self._modal_is_open():
            return "the page now has a modal dialog; read it before acting behind it"
        return f"set [{aid}] to {value}"

    def _on_site(self, url: str) -> bool:
        return urlparse(url).netloc == urlparse(self.base_url).netloc

    async def goto(self, url: str) -> str:
        target = urljoin(self.page.url if self._on_site(self.page.url) else self.base_url + "/", str(url).strip())
        if not self._on_site(target):
            return f"{target} is not on this website; you only work on {self.base_url}"
        try:
            await self.page.goto(target, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        except Exception as error:  # noqa: BLE001
            return f"could not open {target}: {str(error)[:150]}"
        await self.settle()
        return f"opened {self.page.url}"

    async def back(self) -> str:
        try:
            await self.page.go_back(wait_until="domcontentloaded", timeout=10000)
        except Exception as error:  # noqa: BLE001
            return f"could not go back: {str(error)[:120]}"
        await self.settle()
        return f"went back to {self.page.url}"

    async def reload(self) -> str:
        try:
            await self.page.reload(wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        except Exception as error:  # noqa: BLE001
            return f"could not reload: {str(error)[:120]}"
        await self.settle()
        return "reloaded the page"

    async def login(self) -> str:
        if not self.credentials.get("password"):
            return "there are no credentials for this website"
        outcome = "no password field on this page"
        for frame in self.page.frames:
            try:
                outcome = await frame.evaluate(LOGIN_JS, [self.credentials.get("username", ""), self.credentials["password"]])
            except Exception:  # noqa: BLE001
                continue
            if outcome == "ok":
                try:
                    await frame.locator("[data-login-password]").first.press("Enter", no_wait_after=True)
                except Exception:  # noqa: BLE001  the login started a slow page load: it was submitted
                    pass
                await self.settle()
                # Success is read from the page, not waited for: the login form is gone. A form still showing
                # after the bounded wait is reported at once instead of being waited on for 30-45 s (run a176359f).
                if await self.password_field_shown():
                    return ("submitted the login form, but the login form is still shown: read the page for an error "
                            f"message (now at {self.page.url})")
                return f"entered the credentials and submitted the login form; logged in, now at {self.page.url}"
        return outcome

    async def password_field_shown(self, wait: float = LOGIN_WAIT_SECONDS) -> bool:
        """Whether a visible password field is still on the page after up to `wait` seconds: the DOM marker of a
        login that has not gone through (polled, so a login that went through returns at once)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + wait
        while True:
            shown = False
            for frame in list(self.page.frames):
                try:
                    if await frame.evaluate("() => [...document.querySelectorAll('input[type=password]')]"
                                            ".some(e => e.offsetParent !== null)"):
                        shown = True
                        break
                except Exception:  # noqa: BLE001  a frame that is being replaced
                    continue
            if not shown or loop.time() >= end:
                return shown
            await asyncio.sleep(0.25)

    async def collect(self, aid, max_pages: int = 10) -> str:
        """Follow the paging control the model chose until it is gone, and return every page as one document.
        `last_collect` records how it ended: a list that stopped at the page limit with its control still there is
        not complete, and the answer says so instead of passing for the whole list."""
        self.last_collect = None
        locator = self._locator(aid)
        if locator is None or await locator.count() == 0:
            return f"there is no element [{aid}] on the page any more"
        info = await self._describe(locator)
        if not info["name"]:
            return f"[{aid}] has no label to follow from page to page"
        first, start = self.document, self.page.url
        pages, seen = [first], {first}
        limit = max(1, min(int(max_pages or 10), 30))

        async def next_control():
            for _, _, frame in self.frames:
                candidates = frame.locator("a[href], button, [role=button], [role=link]")
                for index in range(min(await candidates.count(), 200)):
                    candidate = candidates.nth(index)
                    try:
                        label = clean_text(await candidate.evaluate("el => el.innerText || el.value || el.getAttribute('aria-label') || ''"))[:80]
                        if label == info["name"] and (await candidate.evaluate("el => el.tagName")) == info["tag"] \
                                and not await candidate.evaluate(
                                    "el => el.disabled === true || el.getAttribute('aria-disabled') === 'true'"):
                            return candidate
                    except Exception:  # noqa: BLE001
                        continue
            return None

        capped = False
        for turn in range(limit):
            control = await next_control()
            if control is None:
                break
            if turn == limit - 1:  # the page limit ended this, not the list: the control is still there
                capped = True
                break
            try:
                await control.click(timeout=2500, no_wait_after=True)
            except Exception:  # noqa: BLE001
                try:
                    await control.evaluate("el => el.click()")
                except Exception:  # noqa: BLE001
                    break
            await self.read()
            if self.document in seen:
                break
            seen.add(self.document)
            pages.append(self.document)
        self.last_collect = {"where": start, "control": info["name"], "pages": len(pages), "capped": capped}
        more = (f' THE LIST IS NOT COMPLETE: this stopped at the limit of {limit} page(s) and "{info["name"]}" is '
                "still there. Collect again from this page (a higher max_pages reads further) before you treat the "
                "list as complete." if capped else "")
        if len(pages) == 1:
            return (f"max_pages {limit} reads only the page you see." + more if capped
                    else f'"{info["name"]}" led to no further page; the list is the page you see')
        # A button may append rows in place rather than lead to a new page. Compare page bodies without generated
        # element ids or the changing paging control: both change between reads even when every earlier row remains.
        def body(page: str) -> str:
            return "\n".join(line for line in without_ids(page).splitlines()
                             if not re.match(r'(?:button|link) "', line.strip()))

        bodies = [body(page) for page in pages]
        grows = (len(pages) > 1 and info["tag"].upper() != "A"
                 and all(later.startswith(earlier) and len(later) >= len(earlier)
                         for earlier, later in zip(bodies, bodies[1:])))
        if grows:  # a "load more" control: the last page holds every row
            self.document = pages[-1]
            return (f'followed "{info["name"]}" {len(pages) - 1} time(s); '
                    + ("the rows loaded so far are on the page." + more if capped
                       else "the list is now complete on the page"))
        self.document = "\n".join(f"{PAGE_BREAK} {number} =====\n{page}" for number, page in enumerate(pages, 1))
        return (f'followed "{info["name"]}" through {len(pages)} page(s); you are on the last one.{more} COLLECTED '
                f"DOCUMENT (file it with keep if it is relevant):\n{without_ids(self.document)[:MAX_OBSERVATION * 2]}")

    async def fetch(self, aid) -> str:
        """Download what the chosen link points to and read it as a document."""
        locator = self._locator(aid)
        if locator is None or await locator.count() == 0:
            return f"there is no element [{aid}] on the page any more"
        target = await locator.evaluate("el => el.href || el.src || el.getAttribute('data-url') || el.getAttribute('formaction') || ''")
        if not target:
            return f"[{aid}] has no address to download"
        target = urljoin(self.page.url, target)
        if not self._on_site(target):
            return f"{target} is not on this website"
        try:
            response = await self.page.context.request.get(target, timeout=20000)
            data = await response.body()
        except Exception as error:  # noqa: BLE001
            return f"could not download {target}: {str(error)[:150]}"
        name = urlparse(target).path.rsplit("/", 1)[-1] or "download"
        disposition = response.headers.get("content-disposition", "")
        named = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition)
        if named:
            name = named.group(1)
        self.document = document_text(data, response.headers.get("content-type", ""), name)
        self.document_source = target
        return (f"downloaded {name} (HTTP {response.status}, {len(data)} bytes). DOCUMENT (file it with keep if it is "
                f"relevant):\n{self.document[:MAX_OBSERVATION * 2]}")
