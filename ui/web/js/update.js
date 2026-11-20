// The update banner: « Une mise à jour est disponible » with a button to install it.
// Installing restarts the daemon: the page then waits for the new version and reloads.

import { ApiError } from "./api.js";
import { el } from "./dom.js";
import { t } from "./i18n.js";

const DISMISSED_KEY = "kreafinity.update.dismissed";
const POLL_INTERVAL = 2000;
const POLL_LIMIT = 15 * 60 * 1000;
const $ = (id) => document.getElementById(id);

function dismissed() {
  try {
    return localStorage.getItem(DISMISSED_KEY);
  } catch {
    return null; // private browsing
  }
}

export class UpdateBanner {
  constructor(api, { toast }) {
    this.api = api;
    this.toast = toast;
    this.info = null;
    this.installing = null; // target commit while installing
    $("update-install").addEventListener("click", () => this.install());
    $("update-later").addEventListener("click", () => this.later());
  }

  /** Ask the daemon what is installed and available. */
  async refresh() {
    try {
      this.show(await this.api.get("/api/update"));
    } catch {
      /* older daemon or offline: no banner */
    }
  }

  /** Look for an update now (button in the settings); returns the answer. */
  async check() {
    const info = await this.api.post("/api/update/check");
    this.show(info, { force: true });
    return info;
  }

  onEvent(event) {
    if (event.type === "update_available") this.show(event.update, { force: true });
    else if (event.type === "update_started") this.follow(event.target);
  }

  show(info, { force = false } = {}) {
    this.info = info;
    if (this.installing) return;
    if (info.status?.state === "running" && info.status.target) {
      this.follow(info.status.target);
      return;
    }
    const banner = $("update-banner");
    const visible = info.supported && info.available && (force || dismissed() !== info.latest);
    banner.hidden = !visible;
    if (!visible) return;
    banner.classList.remove("working", "failed");
    const count = info.changes.length;
    $("update-title").textContent = t("update.title");
    $("update-detail").textContent = count ? t("update.detail", { count }) : "";
    const list = $("update-list");
    list.replaceChildren(...info.changes.slice().reverse().map((change) => el("li", {}, change.title)));
    $("update-changes").hidden = !count;
    const failed = info.status?.state === "failed" && info.status.target === info.latest;
    if (failed) {
      banner.classList.add("failed");
      $("update-detail").textContent = t("update.failed", { message: info.status.message ?? "" });
    }
    $("update-install").textContent = t(failed ? "update.retry" : "update.install");
    $("update-install").disabled = false;
    $("update-later").hidden = false;
  }

  later() {
    try {
      if (this.info?.latest) localStorage.setItem(DISMISSED_KEY, this.info.latest);
    } catch {
      /* private browsing */
    }
    $("update-banner").hidden = true;
  }

  async install() {
    $("update-install").disabled = true;
    try {
      const info = await this.api.post("/api/update");
      this.follow(info.status.target);
    } catch (error) {
      $("update-install").disabled = false;
      this.toast(error instanceof ApiError ? error.message : String(error), "error");
    }
  }

  /** Show the progress, then reload once the daemon runs the new version. */
  async follow(target) {
    if (this.installing === target) return;
    this.installing = target;
    const banner = $("update-banner");
    banner.hidden = false;
    banner.classList.remove("failed");
    banner.classList.add("working");
    $("update-title").textContent = t("update.installing");
    $("update-detail").textContent = t("update.restarting");
    $("update-changes").hidden = true;
    $("update-install").disabled = true;
    $("update-later").hidden = true;
    const deadline = Date.now() + POLL_LIMIT;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL));
      let info;
      try {
        info = await this.api.get("/api/update");
      } catch {
        continue; // the daemon is restarting
      }
      if (info.current?.commit === target) {
        $("update-title").textContent = t("update.done");
        $("update-detail").textContent = "";
        setTimeout(() => location.reload(), 1200);
        return;
      }
      if (info.status?.state === "failed" && info.status.target === target) {
        this.installing = null;
        this.show(info, { force: true });
        return;
      }
    }
    this.installing = null;
    banner.classList.remove("working");
    $("update-detail").textContent = t("update.too_long");
  }
}
