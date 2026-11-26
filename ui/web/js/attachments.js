// Files sent in the conversation: the paperclip, drag and drop, pasted images.
// Each file is uploaded to the daemon when the message is sent.

import { ApiError } from "./api.js";
import { el, icon } from "./dom.js";
import { t } from "./i18n.js";

/** Starts the attachments part of a saved user message (see core/kreafinity/attachments.py). */
export const MARKER = "\n\n---\n📎 ";
const MAX_FILES = 10;
const MAX_SIZE = 25 * 1024 * 1024;

/** A user message's own text, and the names of its attachments. */
export function splitAttachments(content) {
  const index = content.indexOf(MARKER);
  if (index < 0) return { text: content, names: [] };
  const names = [];
  for (const line of content.slice(index).split("\n")) {
    const match = line.match(/^📎 [^:]+ : (.+?)(?: \([^()]*\))?$/u);
    if (match) names.push(match[1]);
  }
  return { text: content.slice(0, index), names };
}

function sizeLabel(bytes) {
  const lang = document.documentElement.lang;
  if (bytes < 1024 * 1024) return t("attach.kb", { size: Math.max(1, Math.round(bytes / 1024)) });
  return t("attach.mb", { size: (bytes / 1024 / 1024).toLocaleString(lang, { maximumFractionDigits: 1 }) });
}

export class Attachments {
  /**
   * @param picker In the KDE panel, which cannot show a file dialog: asks the
   *   daemon to show the desktop's and returns the files it already kept.
   */
  constructor(api, { list, button, input, dropZone, toast, onChange, picker }) {
    this.api = api;
    this.list = list;
    this.toast = toast;
    this.onChange = onChange;
    this.files = []; // { file, url } or, picked by the daemon, { remote: {id, name, size} }
    button.addEventListener("click", () => (picker ? this.pick(picker, button) : input.click()));
    input.addEventListener("change", () => {
      this.add(input.files);
      input.value = "";
    });
    dropZone.addEventListener("dragover", (event) => {
      if (![...(event.dataTransfer?.types ?? [])].includes("Files")) return;
      event.preventDefault();
      dropZone.classList.add("dragging");
    });
    dropZone.addEventListener("dragleave", (event) => {
      if (!dropZone.contains(event.relatedTarget)) dropZone.classList.remove("dragging");
    });
    dropZone.addEventListener("drop", (event) => {
      dropZone.classList.remove("dragging");
      if (!event.dataTransfer?.files?.length) return;
      event.preventDefault();
      this.add(event.dataTransfer.files);
    });
  }

  /** Images pasted in the text field. */
  paste(event) {
    const files = [...(event.clipboardData?.files ?? [])];
    if (!files.length) return;
    event.preventDefault();
    this.add(files);
  }

  /** Files chosen in the desktop's dialog (already kept by the daemon). */
  async pick(picker, button) {
    button.disabled = true;
    try {
      const { files, refused } = await picker();
      for (const remote of files) {
        if (this.files.length >= MAX_FILES) {
          this.toast(t("attach.too_many", { count: MAX_FILES }), "warning");
          break;
        }
        this.files.push({ remote, url: null });
      }
      if (refused?.length) this.toast(t("attach.refused", { names: refused.join(", ") }), "warning");
      this.render();
    } catch (error) {
      this.toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  get count() {
    return this.files.length;
  }

  add(fileList) {
    for (const file of fileList) {
      if (this.files.length >= MAX_FILES) {
        this.toast(t("attach.too_many", { count: MAX_FILES }), "warning");
        break;
      }
      if (file.size > MAX_SIZE) {
        this.toast(t("attach.too_big", { name: file.name }), "error");
        continue;
      }
      const url = file.type.startsWith("image/") ? URL.createObjectURL(file) : null;
      this.files.push({ file, url });
    }
    this.render();
  }

  remove(entry) {
    if (entry.url) URL.revokeObjectURL(entry.url);
    this.files = this.files.filter((item) => item !== entry);
    this.render();
  }

  clear() {
    for (const entry of this.files) if (entry.url) URL.revokeObjectURL(entry.url);
    this.files = [];
    this.render();
  }

  render() {
    this.list.replaceChildren(
      ...this.files.map((entry) => {
        const { name, size } = entry.file ?? entry.remote;
        return el("div", { class: "attachment", title: name },
          entry.url ? el("img", { src: entry.url, alt: "" }) : el("span", { class: "attachment-icon" }, icon("paperclip")),
          el("span", { class: "attachment-name" }, name),
          el("span", { class: "attachment-size" }, sizeLabel(size)),
          el("button", {
            class: "icon-button small", type: "button", "aria-label": t("attach.remove", { name }),
            onclick: () => this.remove(entry),
          }, icon("close")));
      }),
    );
    this.list.hidden = !this.files.length;
    this.onChange?.();
  }

  /** Send the files to the daemon; their ids, for the message. */
  async upload() {
    const ids = [];
    for (const { file, remote } of this.files) {
      if (remote) {
        ids.push(remote.id);
        continue;
      }
      const headers = {
        "X-Kreafinity-Client": "web",
        "Content-Type": file.type || "application/octet-stream",
        "X-Filename": encodeURIComponent(file.name || "fichier"),
      };
      if (this.api.token) headers.Authorization = `Bearer ${this.api.token}`;
      const response = await fetch("/api/uploads", { method: "POST", headers, body: file });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new ApiError(response.status, data?.error?.code ?? "unknown", `${file.name} : ${data?.error?.message ?? response.statusText}`);
      }
      ids.push(data.id);
    }
    return ids;
  }
}

/** The chips under a sent message. */
export function attachmentChips(names) {
  if (!names.length) return null;
  return el("div", { class: "message-attachments" },
    names.map((name) => el("span", { class: "attachment small" }, icon("paperclip"), el("span", { class: "attachment-name" }, name))));
}
