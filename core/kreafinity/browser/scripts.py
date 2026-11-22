"""JavaScript run inside the page by the assistant's browser.

Each script is a function taking one JSON string argument and returning a
JSON string, so both protocols (WebDriver BiDi and Chrome DevTools) only
exchange plain strings. Page text is read with ``textContent``-like APIs and
written with ``textContent``: nothing from the model is ever parsed as HTML.
"""

from __future__ import annotations

#: Attribute marking the elements listed by `READ_PAGE` (``data-kreafinity-ref``).
REF_ATTRIBUTE = "data-kreafinity-ref"

# Fields the assistant must never fill: passwords and payment card data.
_SECRET_FIELDS = """
const secret = (el) => {
  const type = (el.getAttribute('type') || '').toLowerCase();
  const auto = (el.getAttribute('autocomplete') || '').toLowerCase();
  return type === 'password' || /(^|\\s)(current-password|new-password|one-time-code|cc-[a-z-]+)(\\s|$)/.test(auto);
};
"""

READ_PAGE = (
    """(argument) => {
  const options = JSON.parse(argument);
  const ATTR = '%s';
"""
    % REF_ATTRIBUTE
    + _SECRET_FIELDS
    + """
  for (const old of document.querySelectorAll('[' + ATTR + ']')) old.removeAttribute(ATTR);
  const selector = 'a[href], button, input:not([type=hidden]), select, textarea, summary, ' +
    '[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], ' +
    '[role=option], [role=switch], [role=combobox], [role=searchbox], [role=textbox], ' +
    '[contenteditable=""], [contenteditable=true], [onclick]';
  const clean = (text) => (text || '').replace(/\\s+/g, ' ').trim();
  const label = (el) => {
    const byId = el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
    const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\\s+/)
      .map((id) => document.getElementById(id)).filter(Boolean)
      .map((node) => node.textContent).join(' ');
    return clean(el.getAttribute('aria-label') || labelledBy || (byId && byId.textContent) ||
      (el.closest('label') && el.closest('label').textContent) ||
      (el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes(el.type) ? el.value : '') ||
      el.innerText || el.getAttribute('title') || el.getAttribute('placeholder') ||
      el.getAttribute('alt') || (el.querySelector('img[alt]') && el.querySelector('img[alt]').alt) ||
      el.getAttribute('name') || '').slice(0, 80);
  };
  const height = window.innerHeight, width = window.innerWidth;
  const found = [];
  for (const el of document.querySelectorAll(selector)) {
    const box = el.getBoundingClientRect();
    if (box.width < 2 || box.height < 2) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
    const inView = box.bottom > 0 && box.top < height && box.right > 0 && box.left < width;
    found.push({ el, inView, top: box.top });
  }
  // What is on screen first, then the rest of the page from top to bottom.
  found.sort((a, b) => (b.inView - a.inView) || (a.top - b.top));
  const elements = [];
  for (const { el, inView } of found.slice(0, options.max_elements)) {
    const ref = elements.length + 1;
    el.setAttribute(ATTR, String(ref));
    const tag = el.tagName.toLowerCase();
    const item = { ref, tag, role: el.getAttribute('role') || '', label: label(el), visible: inView };
    if (tag === 'input') item.type = (el.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'a') item.href = el.href.slice(0, 200);
    if (secret(el)) item.secret = true;
    else if (['input', 'textarea'].includes(tag) && !['checkbox', 'radio', 'submit', 'button'].includes(item.type || ''))
      item.value = clean(el.value).slice(0, 80);
    if (tag === 'select') item.value = clean(el.selectedOptions[0] ? el.selectedOptions[0].text : '').slice(0, 80);
    if (el.checked !== undefined && ['checkbox', 'radio'].includes(item.type || '')) item.checked = el.checked;
    if (el.disabled) item.disabled = true;
    elements.push(item);
  }
  const text = clean(document.body ? document.body.innerText : '');
  return JSON.stringify({
    url: location.href, title: document.title, text: text.slice(0, options.max_text),
    text_truncated: text.length > options.max_text, elements,
    scroll: { y: Math.round(scrollY), height: document.documentElement.scrollHeight, viewport: height },
  });
}"""
)

#: Scroll the element into view and say where to click it (or why not).
LOCATE = (
    """(argument) => {
  const { ref, purpose } = JSON.parse(argument);
"""
    + _SECRET_FIELDS
    + """
  const el = document.querySelector('[%s="' + ref + '"]');
  if (!el) return JSON.stringify({ error: 'gone' });
  if (purpose === 'type' && secret(el)) return JSON.stringify({ error: 'secret' });
  el.scrollIntoView({ block: 'center', inline: 'center' });
  const box = el.getBoundingClientRect();
  if (purpose === 'type') {
    el.focus();
    if (typeof el.select === 'function') el.select();
    else if (el.isContentEditable) {
      const range = document.createRange();
      range.selectNodeContents(el);
      const selection = getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
  }
  return JSON.stringify({
    x: Math.round(box.left + box.width / 2), y: Math.round(box.top + box.height / 2),
    tag: el.tagName.toLowerCase(), disabled: !!el.disabled,
  });
}"""
    % REF_ATTRIBUTE
)

SELECT_OPTION = (
    """(argument) => {
  const { ref, option } = JSON.parse(argument);
  const el = document.querySelector('[%s="' + ref + '"]');
  if (!el) return JSON.stringify({ error: 'gone' });
  if (el.tagName !== 'SELECT') return JSON.stringify({ error: 'not_select' });
  const wanted = option.trim().toLowerCase();
  const choice = [...el.options].find((o) => o.text.trim().toLowerCase() === wanted || o.value.toLowerCase() === wanted)
    || [...el.options].find((o) => o.text.trim().toLowerCase().includes(wanted));
  if (!choice) return JSON.stringify({ error: 'no_option', options: [...el.options].map((o) => o.text.trim()).slice(0, 30) });
  el.value = choice.value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return JSON.stringify({ selected: choice.text.trim() });
}"""
    % REF_ATTRIBUTE
)

SCROLL = """(argument) => {
  const { direction } = JSON.parse(argument);
  const step = Math.round(innerHeight * 0.8);
  if (direction === 'top') scrollTo(0, 0);
  else if (direction === 'bottom') scrollTo(0, document.documentElement.scrollHeight);
  else scrollBy(0, direction === 'up' ? -step : step);
  return JSON.stringify({ y: Math.round(scrollY) });
}"""

BACK = """(argument) => { history.back(); return JSON.stringify({}); }"""

CLEAR_POINTER = """(argument) => {
  for (const old of document.querySelectorAll('[data-kreafinity-pointer]')) old.remove();
  return JSON.stringify({});
}"""

READY_STATE = """(argument) => JSON.stringify({ state: document.readyState, url: location.href })"""

#: Show the user an element: a glowing frame and a bubble with the assistant's message.
POINT = (
    """(argument) => {
  const { ref, message, seconds } = JSON.parse(argument);
  const el = document.querySelector('[%s="' + ref + '"]');
  if (!el) return JSON.stringify({ error: 'gone' });
  for (const old of document.querySelectorAll('[data-kreafinity-pointer]')) old.remove();
  el.scrollIntoView({ block: 'center', inline: 'center' });
  const box = el.getBoundingClientRect();
  const layer = document.createElement('div');
  layer.setAttribute('data-kreafinity-pointer', '');
  layer.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;';
  const frame = document.createElement('div');
  frame.style.cssText = 'position:fixed;border:3px solid #8b5cf6;border-radius:10px;' +
    'box-shadow:0 0 0 4px rgba(139,92,246,.35),0 0 24px rgba(34,211,238,.8);transition:opacity .4s;' +
    'left:' + (box.left - 6) + 'px;top:' + (box.top - 6) + 'px;width:' + (box.width + 6) + 'px;height:' + (box.height + 6) + 'px;';
  frame.animate([{ opacity: 1 }, { opacity: .35 }, { opacity: 1 }], { duration: 1200, iterations: Infinity });
  const bubble = document.createElement('div');
  bubble.textContent = '∞ ' + message;
  const below = box.bottom + 70 < innerHeight;
  bubble.style.cssText = 'position:fixed;max-width:340px;padding:10px 14px;border-radius:12px;' +
    'background:#1e1b4b;color:#fff;font:15px/1.4 system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.35);' +
    'left:' + Math.max(8, Math.min(box.left, innerWidth - 360)) + 'px;' +
    (below ? 'top:' + (box.bottom + 14) + 'px;' : 'top:' + Math.max(8, box.top - 64) + 'px;');
  layer.append(frame, bubble);
  document.documentElement.append(layer);
  setTimeout(() => layer.remove(), seconds * 1000);
  return JSON.stringify({ shown: true });
}"""
    % REF_ATTRIBUTE
)
