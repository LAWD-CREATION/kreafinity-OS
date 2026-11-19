// Translations of the interface: locales/<language>.json, French as reference.

let current = "fr";
let messages = {};
let fallback = {};

async function fetchCatalog(language) {
  const response = await fetch(`locales/${language}.json`);
  if (!response.ok) throw new Error(`No translations for ${language}`);
  return response.json();
}

/** Load the texts of `language` (falls back to French when missing). */
export async function setLanguage(language) {
  if (!Object.keys(fallback).length) fallback = await fetchCatalog("fr");
  try {
    messages = language === "fr" ? fallback : await fetchCatalog(language);
    current = language;
  } catch {
    messages = fallback;
    current = "fr";
  }
  document.documentElement.lang = current;
}

export function language() {
  return current;
}

/** Text for `key`, with {placeholders}; with a numeric `count`, uses key.one / key.other. */
export function t(key, values = {}) {
  let text;
  if (typeof values.count === "number") {
    const singular = current === "fr" ? Math.abs(values.count) <= 1 : values.count === 1;
    const plural = `${key}.${singular ? "one" : "other"}`;
    text = messages[plural] ?? fallback[plural];
  }
  text = text ?? messages[key] ?? fallback[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (match, name) => (name in values ? String(values[name]) : match));
}

/** Translate elements marked with data-i18n, data-i18n-placeholder and data-i18n-aria. */
export function translatePage(root = document) {
  for (const node of root.querySelectorAll("[data-i18n]")) node.textContent = t(node.dataset.i18n);
  for (const node of root.querySelectorAll("[data-i18n-placeholder]")) {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  }
  for (const node of root.querySelectorAll("[data-i18n-aria]")) {
    const label = t(node.dataset.i18nAria);
    node.setAttribute("aria-label", label);
    node.title = label;
  }
}
