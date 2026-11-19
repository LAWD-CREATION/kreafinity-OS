// A small, safe Markdown renderer for the assistant's answers.
// It builds DOM nodes directly: the text is never interpreted as HTML, and
// links are only created for http(s) addresses.

import { el } from "./dom.js";

const FENCE = /^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

const INLINE = new RegExp(
  [
    "(`+)([\\s\\S]*?[^`])\\1(?!`)", // 1-2: code
    "\\*\\*([\\s\\S]+?)\\*\\*", // 3: bold
    "__([\\s\\S]+?)__", // 4: bold
    "~~([\\s\\S]+?)~~", // 5: strikethrough
    "\\*([^*\\s](?:[\\s\\S]*?[^*\\s])?)\\*", // 6: italic
    "(?<![\\w])_([^_\\s](?:[\\s\\S]*?[^_\\s])?)_(?![\\w])", // 7: italic
    "\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)", // 8-9: link
    "(https?:\\/\\/[^\\s<>()]*[^\\s<>().,;:!?'\"])", // 10: bare address
  ].join("|"),
  "g",
);

/** Render Markdown `text` into a DocumentFragment. */
export function renderMarkdown(text) {
  const fragment = document.createDocumentFragment();
  fragment.append(...blocks(text.replace(/\r\n?/g, "\n").split("\n")));
  return fragment;
}

function blocks(lines) {
  const nodes = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    let match;
    if ((match = line.match(FENCE))) {
      const closing = new RegExp(`^\\s*${match[1][0] === "`" ? "`" : "~"}{${match[1].length},}\\s*$`);
      const code = [];
      index += 1;
      while (index < lines.length && !closing.test(lines[index])) code.push(lines[index++]);
      index += 1; // closing fence (or end of the text while streaming)
      const attributes = match[2] ? { class: `language-${match[2]}` } : {};
      nodes.push(el("pre", {}, el("code", attributes, code.join("\n"))));
    } else if ((match = line.match(HEADING))) {
      const level = Math.min(match[1].length + 2, 6);
      nodes.push(el(`h${level}`, {}, inline(match[2])));
      index += 1;
    } else if (RULE.test(line)) {
      nodes.push(el("hr"));
      index += 1;
    } else if (QUOTE.test(line)) {
      const quoted = [];
      while (index < lines.length && (match = lines[index].match(QUOTE))) {
        quoted.push(match[1]);
        index += 1;
      }
      nodes.push(el("blockquote", {}, blocks(quoted)));
    } else if (line.includes("|") && TABLE_SEPARATOR.test(lines[index + 1] ?? "")) {
      const rows = [];
      rows.push(cells(line));
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        rows.push(cells(lines[index++]));
      }
      nodes.push(table(rows));
    } else if (ITEM.test(line)) {
      const [list, next] = parseList(lines, index);
      nodes.push(list);
      index = next;
    } else {
      const paragraph = [];
      while (index < lines.length && lines[index].trim() && !startsBlock(lines, index)) {
        paragraph.push(lines[index++].trim());
      }
      if (!paragraph.length) paragraph.push(lines[index++].trim());
      nodes.push(el("p", {}, inline(paragraph.join("\n"))));
    }
  }
  return nodes;
}

function startsBlock(lines, index) {
  const line = lines[index];
  return (
    FENCE.test(line) ||
    HEADING.test(line) ||
    RULE.test(line) ||
    QUOTE.test(line) ||
    ITEM.test(line) ||
    (line.includes("|") && TABLE_SEPARATOR.test(lines[index + 1] ?? ""))
  );
}

function indentOf(line) {
  return line.match(/^\s*/)[0].replace(/\t/g, "    ").length;
}

function parseList(lines, start) {
  const first = lines[start].match(ITEM);
  const base = indentOf(lines[start]);
  const ordered = /\d/.test(first[2]);
  const list = el(ordered ? "ol" : "ul");
  if (ordered && parseInt(first[2], 10) !== 1) list.setAttribute("start", parseInt(first[2], 10));
  let index = start;
  let item = null;
  let text = [];
  const flush = () => {
    if (item) item.prepend(...inline(text.join("\n")));
    text = [];
  };
  while (index < lines.length) {
    const line = lines[index];
    const match = line.match(ITEM);
    if (match && indentOf(line) === base && /\d/.test(match[2]) === ordered) {
      flush();
      item = el("li");
      list.append(item);
      text.push(match[3]);
      index += 1;
    } else if (match && indentOf(line) > base && item) {
      flush();
      const [nested, next] = parseList(lines, index);
      item.append(nested);
      index = next;
    } else if (line.trim() && indentOf(line) > base && item && !startsBlock(lines, index)) {
      text.push(line.trim()); // continuation of the item
      index += 1;
    } else {
      break;
    }
  }
  flush();
  return [list, index];
}

function cells(line) {
  let row = line.trim();
  if (row.startsWith("|")) row = row.slice(1);
  if (row.endsWith("|")) row = row.slice(0, -1);
  return row.split("|").map((cell) => cell.trim());
}

function table(rows) {
  const [header, ...body] = rows;
  return el(
    "table",
    {},
    el("thead", {}, el("tr", {}, header.map((cell) => el("th", {}, inline(cell))))),
    el(
      "tbody",
      {},
      body.map((row) => el("tr", {}, header.map((_, column) => el("td", {}, inline(row[column] ?? ""))))),
    ),
  );
}

function link(address, children) {
  return el("a", { href: address, target: "_blank", rel: "noopener noreferrer" }, children);
}

/** Inline formatting, as an array of nodes (newlines become <br>). */
export function inline(text) {
  const nodes = [];
  let last = 0;
  const plain = (value) => {
    value.split("\n").forEach((part, position) => {
      if (position > 0) nodes.push(el("br"));
      if (part) nodes.push(document.createTextNode(part));
    });
  };
  for (const match of text.matchAll(INLINE)) {
    plain(text.slice(last, match.index));
    last = match.index + match[0].length;
    if (match[2] !== undefined) nodes.push(el("code", {}, match[2]));
    else if (match[3] !== undefined || match[4] !== undefined) {
      nodes.push(el("strong", {}, inline(match[3] ?? match[4])));
    } else if (match[5] !== undefined) nodes.push(el("del", {}, inline(match[5])));
    else if (match[6] !== undefined || match[7] !== undefined) {
      nodes.push(el("em", {}, inline(match[6] ?? match[7])));
    } else if (match[8] !== undefined) nodes.push(link(match[9], inline(match[8])));
    else if (match[10] !== undefined) nodes.push(link(match[10], match[10]));
  }
  plain(text.slice(last));
  return nodes;
}
