// First start: choose the AI (a model on this computer, or a service with a key),
// prepare the voice, discover what the assistant can do.

import { el, hydrateIcons } from "./dom.js";
import { t } from "./i18n.js";

const KEY_PAGES = {
  anthropic: "https://console.anthropic.com/settings/keys",
  openai: "https://platform.openai.com/api-keys",
  mistral: "https://console.mistral.ai/api-keys",
  openrouter: "https://openrouter.ai/keys",
  "ollama-cloud": "https://ollama.com/settings/keys",
};
const CHOICES = ["ollama", "anthropic", "openai", "mistral", "openrouter", "ollama-cloud"];
const INSTALL_OLLAMA = "curl -fsSL https://ollama.com/install.sh | sh";

function sizeLabel(gb) {
  return t("onboarding.size", { size: gb.toLocaleString(document.documentElement.lang, { maximumFractionDigits: 1 }) });
}

export class Welcome {
  constructor({ api, toast, onDone, onPrompt }) {
    this.api = api;
    this.toast = toast;
    this.onDone = onDone;
    this.onPrompt = onPrompt;
    this.dialog = el("dialog", { id: "welcome-dialog", class: "settings onboarding", "aria-labelledby": "welcome-heading" });
    this.dialog.addEventListener("cancel", (event) => event.preventDefault()); // Escape: use « Plus tard »
    document.body.append(this.dialog);
    this.pull = null; // { model, bar, text, resolve, reject }
  }

  async open(status) {
    this.status = status ?? (await this.api.get("/api/setup"));
    this.providers = await this.api.get("/api/providers");
    this.show(this.intro());
    if (!this.dialog.open) this.dialog.showModal();
  }

  close() {
    this.dialog.close();
  }

  show(...content) {
    this.dialog.replaceChildren(el("div", { class: "onboarding-step" }, ...content));
    hydrateIcons(this.dialog);
    this.dialog.querySelector("[autofocus], .button.primary")?.focus();
  }

  later() {
    return el("button", { class: "button ghost", type: "button", onclick: () => this.close() }, t("onboarding.later"));
  }

  intro() {
    return [
      el("span", { class: "logo-mark huge" }),
      el("h2", { id: "welcome-heading" }, t("onboarding.intro.title")),
      el("p", {}, t("onboarding.intro.text")),
      el("ul", { class: "onboarding-points" },
        ["files", "apps", "screen", "safe"].map((key) => el("li", {}, t(`onboarding.intro.${key}`)))),
      el("div", { class: "onboarding-actions" },
        this.later(),
        el("button", { class: "button primary", type: "button", onclick: () => this.show(...this.choose()) }, t("onboarding.start"))),
    ];
  }

  choose() {
    const known = new Map(this.providers.providers.map((p) => [p.name, p]));
    const cards = CHOICES.filter((name) => known.has(name)).map((name) => {
      const provider = known.get(name);
      const ready = name === "ollama"
        ? this.status.ollama.running && this.status.ollama.models.length > 0
        : ["keyring", "environment"].includes(provider.api_key);
      return el(
        "button",
        { class: "choice-card", type: "button", dataset: { provider: name }, onclick: () => this.pick(name) },
        el("strong", {}, t(`onboarding.choice.${name}.title`)),
        el("span", { class: "pill" + (name === "ollama" ? " ok" : "") }, t(`onboarding.choice.${name}.tag`)),
        el("span", { class: "choice-text" }, t(`onboarding.choice.${name}.text`)),
        ready ? el("span", { class: "choice-ready" }, t("onboarding.choice.ready")) : null,
      );
    });
    return [
      el("h2", { id: "welcome-heading" }, t("onboarding.choose.title")),
      el("p", {}, t("onboarding.choose.text")),
      el("div", { class: "choice-grid" }, cards),
      el("div", { class: "onboarding-actions" },
        el("button", { class: "button ghost", type: "button", onclick: () => this.show(...this.intro()) }, t("onboarding.back")),
        this.later()),
    ];
  }

  pick(name) {
    if (name === "ollama") return this.local();
    return this.show(...this.keyStep(name));
  }

  keyStep(name) {
    const provider = this.providers.providers.find((p) => p.name === name);
    const input = el("input", { id: "welcome-key", type: "password", autocomplete: "off", spellcheck: "false", autofocus: true });
    const error = el("p", { class: "form-error", hidden: true });
    const submit = el("button", { class: "button primary", type: "submit" }, t("onboarding.key.check"));
    const form = el(
      "form",
      {
        class: "onboarding-form",
        onsubmit: async (event) => {
          event.preventDefault();
          const key = input.value.trim();
          if (!key) return;
          submit.disabled = true;
          submit.textContent = t("onboarding.key.checking");
          error.hidden = true;
          try {
            await this.api.put(`/api/providers/${name}/key`, { api_key: key });
            await this.api.get(`/api/providers/${name}/models`); // checks the key with the service
            await this.api.put("/api/providers/default", { provider: name });
            this.chosen = provider.display_name;
            this.show(...this.voiceStep());
          } catch (failure) {
            error.textContent = t("onboarding.key.failed", { error: failure.message });
            error.hidden = false;
            submit.disabled = false;
            submit.textContent = t("onboarding.key.check");
          }
        },
      },
      el("label", { for: "welcome-key" }, t("onboarding.key.label", { provider: provider.display_name })),
      input,
      el("p", { class: "hint" }, t(this.providers.keyring_available ? "onboarding.key.safe" : "onboarding.key.no_keyring")),
      el("p", { class: "hint" }, el("a", { href: KEY_PAGES[name], target: "_blank", rel: "noopener" }, t("onboarding.key.get"))),
      error,
      el("div", { class: "onboarding-actions" },
        el("button", { class: "button ghost", type: "button", onclick: () => this.show(...this.choose()) }, t("onboarding.back")),
        submit),
    );
    return [el("h2", { id: "welcome-heading" }, provider.display_name), el("p", {}, t(`onboarding.choice.${name}.text`)), form];
  }

  async local() {
    this.status = await this.api.get("/api/setup");
    const { ollama } = this.status;
    const back = el("button", { class: "button ghost", type: "button", onclick: () => this.show(...this.choose()) }, t("onboarding.back"));
    const retry = el("button", { class: "button primary", type: "button", onclick: () => this.local() }, t("onboarding.local.retry"));
    if (!ollama.running) {
      const missing = !ollama.installed;
      return this.show(
        el("h2", { id: "welcome-heading" }, t("onboarding.choice.ollama.title")),
        el("p", {}, t(missing ? "onboarding.local.missing" : "onboarding.local.stopped")),
        el("div", { class: "command-line" },
          el("code", {}, missing ? INSTALL_OLLAMA : "systemctl start ollama"),
          el("button", {
            class: "button small", type: "button",
            onclick: () => navigator.clipboard?.writeText(missing ? INSTALL_OLLAMA : "systemctl start ollama").then(() => this.toast(t("onboarding.copied"))),
          }, t("onboarding.copy"))),
        el("p", { class: "hint" }, t(missing ? "onboarding.local.missing_hint" : "onboarding.local.stopped_hint")),
        el("div", { class: "onboarding-actions" }, back, retry),
      );
    }
    const recommended = this.status.recommended;
    const names = new Set(ollama.models);
    const options = [];
    for (const model of ollama.models) {
      options.push({ name: model, installed: true, label: model });
    }
    for (const model of this.status.local_models) {
      const installed = names.has(model.name) || names.has(`${model.name}:latest`);
      if (installed) continue;
      const notes = [sizeLabel(model.size_gb)];
      if (model.name === recommended) notes.push(t("onboarding.local.recommended"));
      if (model.vision) notes.push(t("onboarding.local.vision"));
      if (model.min_ram_gb > this.status.memory_gb + 0.5) notes.push(t("onboarding.local.too_big", { ram: model.min_ram_gb }));
      options.push({ name: model.name, installed: false, label: `${model.name} — ${notes.join(" · ")}` });
    }
    const preferred = ollama.models[0] ?? recommended;
    const list = el("div", { class: "model-list", role: "radiogroup" }, options.map((option) =>
      el("label", { class: "model-option" },
        el("input", { type: "radio", name: "welcome-model", value: option.name, checked: option.name === preferred }),
        el("span", {}, option.label),
        option.installed ? el("span", { class: "pill ok" }, t("onboarding.local.installed")) : null)));
    const bar = el("progress", { max: 100, value: 0, hidden: true });
    const progressText = el("p", { class: "hint", hidden: true });
    const error = el("p", { class: "form-error", hidden: true });
    const go = el("button", { class: "button primary", type: "button" }, t("onboarding.local.use"));
    go.addEventListener("click", async () => {
      const model = list.querySelector("input:checked")?.value;
      if (!model) return;
      const option = options.find((o) => o.name === model);
      go.disabled = true;
      error.hidden = true;
      try {
        if (!option.installed) {
          bar.hidden = progressText.hidden = false;
          await this.download(model, bar, progressText);
        }
        await this.api.put("/api/providers/default", { provider: `ollama/${model}` });
        this.chosen = `Ollama · ${model}`;
        this.show(...this.voiceStep());
      } catch (failure) {
        error.textContent = t("onboarding.local.failed", { error: failure.message });
        error.hidden = false;
        go.disabled = false;
      }
    });
    return this.show(
      el("h2", { id: "welcome-heading" }, t("onboarding.choice.ollama.title")),
      el("p", {}, t("onboarding.local.pick", { ram: Math.round(this.status.memory_gb) })),
      list, bar, progressText, error,
      el("div", { class: "onboarding-actions" }, back, go),
    );
  }

  download(model, bar, text) {
    return new Promise((resolve, reject) => {
      this.pull = { model, bar, text, resolve, reject };
      text.textContent = t("onboarding.local.starting");
      this.api.post("/api/setup/ollama/pull", { model }).catch((error) => {
        this.pull = null;
        reject(error);
      });
    });
  }

  /** `ollama_pull` events from the daemon. */
  onPull(event) {
    const pull = this.pull;
    if (!pull || event.model !== pull.model) return;
    if (event.error) {
      this.pull = null;
      return pull.reject(new Error(event.error));
    }
    if (event.done) {
      this.pull = null;
      pull.bar.value = 100;
      return pull.resolve();
    }
    if (event.total) {
      pull.bar.value = Math.round((event.completed / event.total) * 100);
      const gb = (bytes) => (bytes / 1024 ** 3).toLocaleString(document.documentElement.lang, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
      pull.text.textContent = t("onboarding.local.progress", { done: gb(event.completed), total: gb(event.total) });
    } else {
      pull.text.textContent = event.status;
    }
  }

  voiceStep() {
    const prepare = el("button", { class: "button primary", type: "button" }, t("onboarding.voice.prepare"));
    prepare.addEventListener("click", async () => {
      prepare.disabled = true;
      try {
        await this.api.post("/api/voice/prepare");
        this.toast(t("onboarding.voice.started"));
      } catch (error) {
        this.toast(error.message, "error");
      }
      this.show(...this.done());
    });
    return [
      el("h2", { id: "welcome-heading" }, t("onboarding.voice.title")),
      el("p", {}, t("onboarding.voice.text")),
      el("div", { class: "onboarding-actions" },
        el("button", { class: "button ghost", type: "button", onclick: () => this.show(...this.done()) }, t("onboarding.voice.skip")),
        prepare),
    ];
  }

  done() {
    const finish = async (prompt) => {
      try {
        await this.api.post("/api/setup/complete");
      } catch (error) {
        this.toast(error.message, "error");
      }
      this.close();
      this.onDone?.();
      if (prompt) this.onPrompt?.(prompt);
    };
    return [
      el("span", { class: "logo-mark huge" }),
      el("h2", { id: "welcome-heading" }, t("onboarding.done.title")),
      el("p", {}, t("onboarding.done.text", { ai: this.chosen ?? "" })),
      el("ul", { class: "onboarding-points" },
        ["shortcut", "approve", "settings"].map((key) => el("li", {}, t(`onboarding.done.${key}`)))),
      el("p", { class: "hint" }, t("onboarding.done.try")),
      el("div", { class: "suggestions" }, ["1", "2", "3"].map((n) =>
        el("button", { class: "suggestion", type: "button", onclick: () => finish(t(`onboarding.done.prompt${n}`)) }, t(`onboarding.done.prompt${n}`)))),
      el("div", { class: "onboarding-actions" },
        el("button", { class: "button primary", type: "button", onclick: () => finish() }, t("onboarding.done.start"))),
    ];
  }
}
