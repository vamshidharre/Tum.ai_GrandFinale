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

PAGE_LOAD_TIMEOUT_MS = 5_000

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


def document_text(data: bytes, content_type: str, name: str, depth: int = 0) -> str:
    """The text of a fetched file: archives are opened, markup is stripped, PDFs are read when pypdf is installed."""
    kind = (content_type or "").lower()
    lower = name.lower()
    if data[:4] == b"PK\x03\x04" or "zip" in kind or lower.endswith(".zip"):
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return "(unreadable archive)"
        parts = [f"ARCHIVE {name} with files: " + ", ".join(info.filename for info in archive.infolist())]
        for info in archive.infolist()[:40]:
            if info.is_dir() or depth > 2:
                continue
            parts.append(f"=== FILE {info.filename} ===\n" + document_text(archive.read(info), "", info.filename, depth + 1))
        return "\n".join(parts)
    if data[:5] == b"%PDF-" or "pdf" in kind:
        try:
            from pypdf import PdfReader
            return clean_text("\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages))
        except Exception as error:  # noqa: BLE001
            return f"(PDF could not be read: {error})"
    text = data.decode("utf-8", errors="replace")
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
        self.events: list[str] = []
        self.bad_status = 0
        self.loading: set = set()  # page, frame and data requests that have not come back yet
        self.navigation_generation = 0
        self.last_navigation_error = ""
        self._watch(page)

    def _watch(self, page) -> None:
        try:
            page.set_default_navigation_timeout(PAGE_LOAD_TIMEOUT_MS)
        except Exception:  # noqa: BLE001
            pass
        page.on("dialog", lambda dialog: asyncio.ensure_future(self._on_dialog(dialog)))
        page.on("response", self._on_response)
        page.on("request", lambda request: self._on_request(page, request))
        page.on("requestfinished", lambda request: self.loading.discard(request))
        page.on("requestfailed", lambda request: self.loading.discard(request))

    def _on_request(self, page, request) -> None:
        if page is not self.page:
            return
        if request.resource_type in ("document", "xhr", "fetch"):
            self.loading.add(request)
        try:
            if page is self.page and request.is_navigation_request() and request.frame == page.main_frame:
                self.navigation_generation += 1
        except Exception:  # noqa: BLE001
            pass

    async def _replace_page(self) -> None:
        """Replace a hung tab without consuming another sandbox browser slot."""
        old = self.page
        page = await old.context.new_page()
        self.page, self.frames, self.loading, self.bad_status = page, [], set(), 0
        self.last_navigation_error = ""
        self._watch(page)
        try:
            await old.close()
        except Exception:  # noqa: BLE001
            pass

    async def renew(self) -> str:
        """Swap a broken tab for a fresh one in the same browser: same session and cookies, a clean page. A tab can get
        stuck ("frame was detached", loads that never finish) while the website itself is fine: in sandbox runs the
        investigators' tab kept timing out for minutes while a fresh tab on the same site loaded at once."""
        await self._replace_page()
        return await self.goto(self.base_url + "/")

    async def _recover_navigation(self, target: str, previous_url: str, action: str, error: Exception,
                                  refresh_first: bool = True) -> str:
        """After a bounded load wait, try a refresh and then one clean tab in this browser."""
        timeout_s = PAGE_LOAD_TIMEOUT_MS / 1000
        self.events.append(f"{action} did not load within {timeout_s:g}s; trying browser recovery")
        if refresh_first and self.page.url not in ("", "about:blank"):
            try:
                await self.page.reload(wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
                await self.settle()
                if self.page.url != previous_url or self.page.url == target:
                    self.last_navigation_error = ""
                    return f"{action} exceeded {timeout_s:g}s; refreshed successfully at {self.page.url}"
            except Exception:  # noqa: BLE001
                pass

        await self._replace_page()
        try:
            await self.page.goto(target, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            await self.settle()
            self.last_navigation_error = ""
            return f"{action} exceeded {timeout_s:g}s; reopened the tab and loaded {self.page.url}"
        except Exception as reopen_error:  # noqa: BLE001
            self.last_navigation_error = f"{target} did not load after refresh/reopen: {str(reopen_error)[:160]}"
            return (f"could not load {target} within {timeout_s:g}s; refresh and reopen both failed: "
                    f"{str(error)[:100]} | {str(reopen_error)[:100]}")

    async def _finish_navigation_if_started(self, previous_url: str, generation: int, action: str) -> str | None:
        """Bound document loads initiated by controls such as links and form submits."""
        if self.navigation_generation == generation and self.page.url == previous_url:
            return None
        target = self.page.url if self._on_site(self.page.url) else previous_url
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            return None
        except Exception as error:  # noqa: BLE001
            return await self._recover_navigation(target, previous_url, action, error)

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
        end, quiet = loop.time() + 12, None
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
                await frame.wait_for_load_state("domcontentloaded", timeout=8000 if frame == self.page.main_frame else 3000)
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
        if self.last_navigation_error:
            self.document = f"url: {self.page.url}\ntitle: page unavailable\n{self.last_navigation_error}"
            self.document_source = self.page.url
            return self.document
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
            try:
                if await frame.evaluate(script):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

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
        navigation_before = self.navigation_generation
        try:
            await locator.click(timeout=PAGE_LOAD_TIMEOUT_MS)
        except Exception as error:  # noqa: BLE001  hidden in a collapsed menu, or covered: activate it directly
            if self.navigation_generation != navigation_before or self.page.url != before:
                target = self.page.url if self._on_site(self.page.url) else before
                return await self._recover_navigation(target, before, "click navigation", error)
            try:
                await locator.evaluate("el => el.click()")
            except Exception as error:  # noqa: BLE001
                return f"could not click [{aid}]: {str(error)[:150]}"
        recovery = await self._finish_navigation_if_started(before, navigation_before, "click navigation")
        if recovery:
            return recovery
        await self.settle()
        if await self._modal_is_open():
            return "the page now has a modal dialog; read it before acting behind it"
        moved = self.page.url != before
        return f'clicked [{aid}] "{info["name"]}"' + (f"; now at {self.page.url}" if moved else "")

    async def fill(self, aid, text: str, enter: bool = False) -> str:
        locator = self._locator(aid)
        if locator is None or await locator.count() == 0:
            return f"there is no element [{aid}] on the page any more"
        try:
            await locator.fill(str(text), timeout=2500)
        except Exception as error:  # noqa: BLE001
            return f"could not fill [{aid}]: {str(error)[:150]}"
        if enter:
            before, navigation_before = self.page.url, self.navigation_generation
            try:
                await locator.press("Enter", timeout=PAGE_LOAD_TIMEOUT_MS)
            except Exception as error:  # noqa: BLE001
                if self.navigation_generation != navigation_before or self.page.url != before:
                    return await self._recover_navigation(self.page.url, before, "form submission", error)
                return f"could not press Enter in [{aid}]: {str(error)[:150]}"
            recovery = await self._finish_navigation_if_started(before, navigation_before, "form submission")
            if recovery:
                return recovery
            await self.settle()
        if await self._modal_is_open():
            return "the page now has a modal dialog; read it before acting behind it"
        return f"filled [{aid}]" + (" and pressed Enter" if enter else "")

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
        previous_url = self.page.url
        try:
            await self.page.goto(target, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        except Exception as error:  # noqa: BLE001
            return await self._recover_navigation(target, previous_url, "navigation", error)
        await self.settle()
        self.last_navigation_error = ""
        return f"opened {self.page.url}"

    async def back(self) -> str:
        previous_url = self.page.url
        try:
            await self.page.go_back(wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        except Exception as error:  # noqa: BLE001
            target = self.page.url if self._on_site(self.page.url) else previous_url
            return await self._recover_navigation(target, previous_url, "back navigation", error)
        await self.settle()
        self.last_navigation_error = ""
        return f"went back to {self.page.url}"

    async def reload(self) -> str:
        target = self.page.url
        try:
            await self.page.reload(wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        except Exception as error:  # noqa: BLE001
            return await self._recover_navigation(target, target, "reload", error, refresh_first=False)
        await self.settle()
        self.last_navigation_error = ""
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
                before, navigation_before = self.page.url, self.navigation_generation
                try:
                    await frame.locator("[data-login-password]").first.press("Enter", timeout=PAGE_LOAD_TIMEOUT_MS)
                except Exception as error:  # noqa: BLE001
                    if self.navigation_generation != navigation_before or self.page.url != before:
                        return await self._recover_navigation(self.page.url, before, "login navigation", error)
                    return f"login submit did not complete: {str(error)[:150]}"
                recovery = await self._finish_navigation_if_started(before, navigation_before, "login navigation")
                if recovery:
                    return recovery
                await self.settle()
                return f"entered the credentials and submitted the login form; now at {self.page.url}"
        return outcome

    async def collect(self, aid, max_pages: int = 10) -> str:
        """Follow the paging control the model chose until it is gone, and return every page as one document."""
        locator = self._locator(aid)
        if locator is None or await locator.count() == 0:
            return f"there is no element [{aid}] on the page any more"
        info = await self._describe(locator)
        if not info["name"]:
            return f"[{aid}] has no label to follow from page to page"
        first = self.document
        pages, seen = [first], {first}
        for _ in range(max(1, min(int(max_pages or 10), 30)) - 1):
            control = None
            for _, _, frame in self.frames:
                candidates = frame.locator("a[href], button, [role=button], [role=link]")
                for index in range(min(await candidates.count(), 200)):
                    candidate = candidates.nth(index)
                    try:
                        label = clean_text(await candidate.evaluate("el => el.innerText || el.value || el.getAttribute('aria-label') || ''"))[:80]
                        if label == info["name"] and (await candidate.evaluate("el => el.tagName")) == info["tag"] \
                                and not await candidate.evaluate("el => el.disabled === true"):
                            control = candidate
                            break
                    except Exception:  # noqa: BLE001
                        continue
                if control:
                    break
            if control is None:
                break
            try:
                await control.click(timeout=2500)
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
        if len(pages) == 1:
            return f'"{info["name"]}" led to no further page; the list is the page you see'
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
            return f'followed "{info["name"]}" {len(pages) - 1} time(s); the list is now complete on the page'
        self.document = "\n".join(f"{PAGE_BREAK} {number} =====\n{page}" for number, page in enumerate(pages, 1))
        return (f'followed "{info["name"]}" through {len(pages)} page(s); you are on the last one. COLLECTED DOCUMENT '
                f"(file it with keep if it is relevant):\n{without_ids(self.document)[:MAX_OBSERVATION * 2]}")

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
