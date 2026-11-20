// The settings dialog: AI providers, API keys (system keyring), tools, folders, general.

import { ApiError } from "./api.js";
import { el, icon } from "./dom.js";
import { t } from "./i18n.js";

const THEME_KEY = "kreafinity.theme";
const RISK_ORDER = { safe: 0, confirm: 1, danger: 2 };

/** Apply the theme preference ("system", "dark" or "light"). */
export function applyTheme(preference) {
  let choice = preference;
  if (choice === undefined) {
    try {
      choice = localStorage.getItem(THEME_KEY) || "dark";
    } catch {
      choice = "dark";
    }
  }
  const light = choice === "light" || (choice === "system" && matchMedia("(prefers-color-scheme: light)").matches);
  document.documentElement.dataset.theme = light ? "light" : "dark";
  return choice;
}

function splitSpec(spec) {
  if (!spec) return ["", ""];
  const [name, ...model] = spec.split("/");
  return [name, model.join("/")];
}

function joinSpec(name, model) {
  if (!name) return null;
  return model.trim() ? `${name}/${model.trim()}` : name;
}

export class SettingsDialog {
  constructor({ api, info, onSaved, toast, updates }) {
    this.api = api;
    this.info = info;
    this.onSaved = onSaved;
    this.toast = toast;
    this.updates = updates;
    this.dialog = document.getElementById("settings");
    this.dialog.addEventListener("click", (event) => {
      if (event.target === this.dialog) this.dialog.close(); // click on the backdrop
    });
  }

  async open(section) {
    try {
      const [config, providers, tools] = await Promise.all([
        this.api.get("/api/config"),
        this.api.get("/api/providers"),
        this.api.get("/api/tools"),
      ]);
      this.config = config;
      this.providers = providers;
      this.render(config, providers, tools);
    } catch (error) {
      this.toast(error.message, "error");
      return;
    }
    this.dialog.showModal();
    if (section) this.dialog.querySelector(`[data-section="${section}"]`)?.scrollIntoView();
  }

  providerOptions(selected, { none } = {}) {
    const options = [];
    if (none) options.push(el("option", { value: "" }, t("settings.fallback_none")));
    for (const provider of this.providers.providers) {
      options.push(el("option", { value: provider.name, selected: provider.name === selected }, provider.display_name));
    }
    return options;
  }

  render(config, providers, tools) {
    const [defaultName, defaultModel] = splitSpec(config.provider);
    const [fallbackName, fallbackModel] = splitSpec(config.fallback);
    this.fields = {
      provider: el("select", { id: "settings-provider" }, this.providerOptions(defaultName)),
      model: el("input", { id: "settings-model", value: defaultModel, placeholder: t("picker.model_default"), autocomplete: "off" }),
      fallback: el("select", { id: "settings-fallback" }, this.providerOptions(fallbackName, { none: true })),
      fallbackModel: el("input", { id: "settings-fallback-model", value: fallbackModel, placeholder: t("picker.model_default"), autocomplete: "off" }),
      folders: el("textarea", { id: "settings-folders", rows: 3, spellcheck: "false" }),
      language: el(
        "select",
        { id: "settings-language" },
        el("option", { value: "fr", selected: config.language === "fr" }, "Français"),
        el("option", { value: "en", selected: config.language === "en" }, "English"),
      ),
      iterations: el("input", { id: "settings-iterations", type: "number", min: 1, max: 50, value: config.max_iterations }),
      theme: el(
        "select",
        { id: "settings-theme" },
        ["dark", "light", "system"].map((value) =>
          el("option", { value, selected: applyTheme() === value }, t(`settings.theme.${value}`)),
        ),
      ),
      checkUpdates: el("input", { id: "settings-check-updates", type: "checkbox", checked: config.check_updates !== false }),
    };
    this.fields.folders.value = config.allowed_paths.join("\n");
    this.toolBoxes = new Map();
    this.error = el("p", { class: "form-error", hidden: true });

    const sortedTools = [...tools].sort(
      (a, b) => RISK_ORDER[a.risk] - RISK_ORDER[b.risk] || a.name.localeCompare(b.name),
    );

    const form = el(
      "form",
      { method: "dialog", onsubmit: (event) => this.save(event) },
      el(
        "div",
        { class: "settings-header" },
        el("h2", {}, t("settings.title")),
        el("button", { class: "icon-button", type: "button", "aria-label": t("settings.close"), title: t("settings.close"), onclick: () => this.dialog.close() }, icon("close")),
      ),
      el(
        "div",
        { class: "settings-body" },
        el(
          "section",
          { class: "settings-section", dataset: { section: "ai" } },
          el("h3", {}, t("settings.ai.title")),
          el("p", {}, t("settings.ai.text")),
          el("div", { class: "field-row" }, el("label", { for: "settings-provider" }, t("settings.ai.default")), this.fields.provider, this.fields.model),
          el("div", { class: "field-row" }, el("label", { for: "settings-fallback" }, t("settings.ai.fallback")), this.fields.fallback, this.fields.fallbackModel),
        ),
        this.renderKeys(providers),
        el(
          "section",
          { class: "settings-section", dataset: { section: "tools" } },
          el("h3", {}, t("settings.tools.title")),
          el("p", {}, t("settings.tools.text")),
          sortedTools.map((tool) => this.toolRow(tool)),
        ),
        el(
          "section",
          { class: "settings-section", dataset: { section: "folders" } },
          el("h3", {}, t("settings.folders.title")),
          el("p", {}, t("settings.folders.text")),
          this.fields.folders,
        ),
        el(
          "section",
          { class: "settings-section", dataset: { section: "general" } },
          el("h3", {}, t("settings.general.title")),
          el("div", { class: "field-row two" }, el("label", { for: "settings-language" }, t("settings.general.language")), this.fields.language),
          el("div", { class: "field-row two" }, el("label", { for: "settings-iterations" }, t("settings.general.iterations")), this.fields.iterations),
          el("div", { class: "field-row two" }, el("label", { for: "settings-theme" }, t("settings.general.theme")), this.fields.theme),
          el("div", { class: "field-row two" }, el("label", { for: "settings-check-updates" }, t("settings.updates.auto")), el("div", {}, this.fields.checkUpdates)),
          this.renderUpdates(),
        ),
        this.error,
      ),
      el(
        "div",
        { class: "settings-footer" },
        el("span", { class: "version" }, t("settings.version", { version: this.info().version })),
        el("button", { class: "button ghost", type: "button", onclick: () => this.dialog.close() }, t("settings.cancel")),
        el("button", { class: "button primary", type: "submit" }, t("settings.save")),
      ),
    );
    this.dialog.replaceChildren(form);
  }

  toolRow(tool) {
    const id = `tool-${tool.name}`;
    const checkbox = el("input", { type: "checkbox", id, checked: tool.enabled });
    this.toolBoxes.set(tool.name, checkbox);
    const label = t(`tool.${tool.name}`);
    return el(
      "div",
      { class: "tool-row" },
      checkbox,
      el("label", { for: id }, el("div", { class: "tool-name" }, label === `tool.${tool.name}` ? tool.name : label), el("div", { class: "hint" }, t(`tool.${tool.name}.help`) === `tool.${tool.name}.help` ? tool.description : t(`tool.${tool.name}.help`))),
      el("span", { class: `risk ${tool.risk}` }, t(`risk.${tool.risk}`)),
    );
  }

  /** Installed version and « Rechercher maintenant » (updates). */
  renderUpdates() {
    const info = this.updates?.info;
    if (!info) return null;
    if (!info.supported) return el("p", { class: "hint" }, t("settings.updates.dev"));
    const result = el("span", { class: "hint", id: "settings-update-result" });
    const button = el("button", { class: "button small", type: "button", id: "settings-update-check" }, t("settings.updates.check"));
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const answer = await this.updates.check();
        result.textContent = t(answer.available ? "settings.updates.available" : "settings.updates.up_to_date");
      } catch (error) {
        result.textContent = error instanceof ApiError ? error.message : String(error);
      } finally {
        button.disabled = false;
      }
    });
    const commit = info.current?.commit ? info.current.commit.slice(0, 7) : "?";
    return el(
      "div",
      { class: "field-row two" },
      el("span", {}, t("settings.updates.current", { commit, ref: info.current?.ref ?? "" })),
      el("div", { class: "update-check" }, button, result),
    );
  }

  renderKeys(providers) {
    const section = el(
      "section",
      { class: "settings-section", dataset: { section: "keys" } },
      el("h3", {}, t("settings.keys.title")),
      el("p", {}, t(providers.keyring_available ? "settings.keys.text" : "settings.keys.no_keyring")),
    );
    for (const provider of providers.providers.filter((p) => p.requires_api_key)) {
      section.append(this.keyRow(provider, providers.keyring_available));
    }
    return section;
  }

  keyRow(provider, available) {
    const status = el("span", {});
    const setStatus = (source) => {
      status.className = `pill ${source === "missing" ? "missing" : "ok"}`;
      status.textContent = t(`settings.keys.${source}`, { variable: provider.api_key_env_var });
    };
    setStatus(provider.api_key);
    const input = el("input", {
      type: "password",
      autocomplete: "off",
      spellcheck: "false",
      placeholder: t("settings.keys.placeholder"),
      "aria-label": t("settings.keys.label", { provider: provider.display_name }),
      disabled: !available,
    });
    const save = el("button", { class: "button small", type: "button", disabled: !available }, t("settings.keys.save"));
    const remove = el("button", { class: "button small ghost", type: "button", disabled: !available || provider.api_key !== "keyring" }, t("settings.keys.delete"));
    save.addEventListener("click", async () => {
      if (!input.value.trim()) return input.focus();
      try {
        const result = await this.api.put(`/api/providers/${provider.name}/key`, { api_key: input.value.trim() });
        input.value = "";
        setStatus(result.api_key);
        remove.disabled = result.api_key !== "keyring";
        this.toast(t("settings.keys.saved", { provider: provider.display_name }));
      } catch (error) {
        this.toast(error.message, "error");
      }
    });
    remove.addEventListener("click", async () => {
      try {
        const result = await this.api.delete(`/api/providers/${provider.name}/key`);
        setStatus(result.api_key);
        remove.disabled = true;
        this.toast(t("settings.keys.deleted", { provider: provider.display_name }));
      } catch (error) {
        this.toast(error.message, "error");
      }
    });
    return el("div", { class: "key-row" }, el("strong", {}, provider.display_name), status, input, save, remove);
  }

  async save(event) {
    event.preventDefault();
    const fields = this.fields;
    const changes = {};
    const provider = joinSpec(fields.provider.value, fields.model.value);
    if (provider && provider !== this.config.provider) changes.provider = provider;
    const fallback = joinSpec(fields.fallback.value, fields.fallbackModel.value);
    if (fallback !== (this.config.fallback ?? null)) changes.fallback = fallback;
    const enabled = [...this.toolBoxes].filter(([, box]) => box.checked).map(([name]) => name);
    const tools = enabled.length === this.toolBoxes.size ? null : enabled;
    if (JSON.stringify(tools) !== JSON.stringify(this.config.tools)) changes.tools = tools;
    const folders = fields.folders.value.split("\n").map((line) => line.trim()).filter(Boolean);
    if (JSON.stringify(folders) !== JSON.stringify(this.config.allowed_paths)) changes.allowed_paths = folders;
    if (fields.language.value !== this.config.language) changes.language = fields.language.value;
    const iterations = Number(fields.iterations.value);
    if (iterations !== this.config.max_iterations) changes.max_iterations = iterations;
    if (fields.checkUpdates.checked !== (this.config.check_updates !== false)) {
      changes.check_updates = fields.checkUpdates.checked;
    }

    const theme = fields.theme.value;
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch {
      /* private browsing: the theme is not remembered */
    }
    applyTheme(theme);

    this.error.hidden = true;
    try {
      const config = Object.keys(changes).length ? await this.api.patch("/api/config", changes) : this.config;
      this.dialog.close();
      this.onSaved(config, changes);
    } catch (error) {
      this.error.textContent = error instanceof ApiError ? error.message : String(error);
      this.error.hidden = false;
    }
  }
}
