// DOM helpers. Untrusted text (from the AI, file names…) is only ever inserted
// as text nodes: never through innerHTML.

const SVG = "http://www.w3.org/2000/svg";

const ICONS = {
  plus: "M12 5v14M5 12h14",
  send: "M12 19V5M5 12l7-7 7 7",
  stop: "M7 7h10v10H7z",
  sliders: "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
  menu: "M3 6h18M3 12h18M3 18h18",
  close: "M18 6 6 18M6 6l12 12",
  trash: "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6",
  edit: "M4 20h4L19 9l-4-4L4 16v4z",
  alert: "M12 3 2 21h20L12 3zM12 10v4M12 17.5v.5",
  shield: "M12 3l8 4v5c0 5-3.5 8.5-8 9-4.5-.5-8-4-8-9V7l8-4z",
  key: "M15 3a6 6 0 0 0-5.7 7.9L3 17.2V21h3.8v-2.5h2.5V16h2.5l1.3-1.3A6 6 0 1 0 15 3zM16.5 7.5h.01",
  check: "M5 12.5l4.5 4.5L19 7",
  expand: "M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7",
  download: "M12 3v12M7 10l5 5 5-5M5 21h14",
  wave: "M3 12h2M7 8v8M11 5v14M15 9v6M19 7v10M21 12h0",
  paperclip: "M21 11.5l-8.6 8.6a5.5 5.5 0 0 1-7.8-7.8l8.6-8.6a3.7 3.7 0 0 1 5.2 5.2l-8.6 8.6a1.8 1.8 0 0 1-2.6-2.6l7.9-7.9",
  tool: "M14.7 6.3a4 4 0 0 0-5.4 5.2L3 17.8V21h3.2l6.3-6.3a4 4 0 0 0 5.2-5.4l-2.6 2.6-2.4-.6-.6-2.4z",
  sparkle: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15z",
  puzzle: "M10 3h4v2.5a1.5 1.5 0 0 0 3 0V3h4v7h-2.5a1.5 1.5 0 0 0 0 3H21v8h-7v-2.5a1.5 1.5 0 0 0-3 0V21H3v-8h2.5a1.5 1.5 0 0 0 0-3H3V3h7z",
  book: "M4 4.5A1.5 1.5 0 0 1 5.5 3H20v16H5.5A1.5 1.5 0 0 0 4 20.5zM4 20.5A1.5 1.5 0 0 0 5.5 22H20M8 7h8M8 11h6",
  mic: "M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM5 11a7 7 0 0 0 14 0M12 18v3M8.5 21h7",
};

/** Build an element: el("p", {class: "x", dataset: {id: "1"}, onclick: fn}, "text", child). */
export function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === undefined || value === null || value === false) continue;
    if (name === "class") node.className = value;
    else if (name === "dataset") Object.assign(node.dataset, value);
    else if (name.startsWith("on") && typeof value === "function") {
      node.addEventListener(name.slice(2), value);
    } else if (value === true) node.setAttribute(name, "");
    else node.setAttribute(name, String(value));
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/** An inline SVG icon. */
export function icon(name) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", ICONS[name] ?? "");
  svg.append(path);
  return svg;
}

/** Replace every <span data-icon="name"> placeholder with its icon. */
export function hydrateIcons(root = document) {
  for (const placeholder of root.querySelectorAll("[data-icon]")) {
    placeholder.replaceChildren(icon(placeholder.dataset.icon));
  }
}

/** Show paths inside the home folder as ~/… */
export function shortenPath(text, home) {
  if (!home || typeof text !== "string") return text;
  return text.split(home + "/").join("~/").split(`'${home}'`).join("'~'");
}

/** "à l'instant", "il y a 5 min"… using the current language. */
export function relativeTime(iso, language) {
  const date = new Date(iso);
  const seconds = (date.getTime() - Date.now()) / 1000;
  const format = new Intl.RelativeTimeFormat(language, { numeric: "auto" });
  const steps = [
    [60, "second"],
    [3600, "minute", 60],
    [86400, "hour", 3600],
    [604800, "day", 86400],
  ];
  if (Math.abs(seconds) < 45) return format.format(0, "second");
  for (const [limit, unit, divisor = 1] of steps) {
    if (Math.abs(seconds) < limit) return format.format(Math.round(seconds / divisor), unit);
  }
  return date.toLocaleDateString(language, { day: "numeric", month: "short" });
}
