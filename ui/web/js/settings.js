// The settings dialog: AI providers, API keys (system keyring), tools, folders, general.

import { ApiError } from "./api.js";
import { el, icon } from "./dom.js";
import { t } from "./i18n.js";
import { McpSection } from "./mcp.js";

const THEME_KEY = "kreafinity.theme";
const RISK_ORDER = { safe: 0, confirm: 1, danger: 2 };

/** The tabs of the dialog, and the sections each one shows. */
const TABS = [
  { id: "ai", icon: "sparkle", sections: ["ai", "keys"] },
  { id: "tools", icon: "tool", sections: ["tools"] },
  { id: "security", icon: "shield", sections: ["permissions", "folders"] },
  { id: "extensions", icon: "puzzle", sections: ["mcp"] },
  { id: "memory", icon: "book", sections: ["memory"] },
  { id: "voice", icon: "mic", sections: ["voice"] },
  { id: "general", icon: "sliders", sections: ["general"] },
];

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
  constructor({ api, info, onSaved, toast, updates, openWelcome }) {
    this.api = api;
    this.openWelcome = openWelcome;
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
      const [config, providers, tools, memory, voice] = await Promise.all([
        this.api.get("/api/config"),
        this.api.get("/api/providers"),
        this.api.get("/api/tools"),
        this.api.get("/api/memory").catch(() => null),
        this.api.get("/api/voice").catch(() => null),
      ]);
      this.config = config;
      this.providers = providers;
      this.memory = memory;
      this.voice = voice;
      this.render(config, providers, tools);
    } catch (error) {
      this.toast(error.message, "error");
      return;
    }
    const tab = section ? TABS.find((entry) => entry.sections.includes(section)) : null;
    this.showTab(tab?.id ?? this.tab ?? TABS[0].id);
    this.dialog.showModal();
    if (section && tab?.sections[0] !== section) this.dialog.querySelector(`[data-section="${section}"]`)?.scrollIntoView();
  }

  /** Show one tab of the dialog. */
  showTab(id, { focus = false } = {}) {
    this.tab = id;
    for (const button of this.dialog.querySelectorAll("[role=tab]")) {
      const selected = button.dataset.tab === id;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      if (selected && focus) button.focus();
    }
    for (const panel of this.dialog.querySelectorAll("[role=tabpanel]")) panel.hidden = panel.dataset.tab !== id;
    const body = this.dialog.querySelector(".settings-panels");
    if (body) body.scrollTop = 0;
  }

  /** The tab list and its panels, from the dialog's sections. */
  renderTabs(sections) {
    const byName = new Map(sections.filter(Boolean).map((section) => [section.dataset.section, section]));
    const tabs = el("div", { class: "settings-tabs", role: "tablist", "aria-label": t("settings.title"), "aria-orientation": "vertical" },
      TABS.map((tab) => el("button", {
        type: "button", role: "tab", id: `settings-tab-${tab.id}`, dataset: { tab: tab.id },
        "aria-controls": `settings-panel-${tab.id}`, "aria-selected": "false", tabIndex: -1,
        onclick: () => this.showTab(tab.id),
      }, icon(tab.icon), el("span", {}, t(`settings.tab.${tab.id}`)))));
    tabs.addEventListener("keydown", (event) => {
      const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[event.key];
      const index = TABS.findIndex((tab) => tab.id === this.tab);
      let next = null;
      if (step) next = (index + step + TABS.length) % TABS.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = TABS.length - 1;
      if (next === null) return;
      event.preventDefault();
      this.showTab(TABS[next].id, { focus: true });
    });
    const panels = el("div", { class: "settings-panels" },
      TABS.map((tab) => el("div", {
        role: "tabpanel", id: `settings-panel-${tab.id}`, dataset: { tab: tab.id },
        "aria-labelledby": `settings-tab-${tab.id}`, hidden: true,
      }, tab.sections.map((name) => byName.get(name)).filter(Boolean))));
    return [tabs, panels];
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
      memory: el("input", { id: "settings-memory", type: "checkbox", checked: config.memory !== false }),
      memoryReview: el("input", { id: "settings-memory-review", type: "checkbox", checked: config.memory_review !== false }),
      memoryUser: el("textarea", { id: "settings-memory-user", class: "prose", rows: 4, spellcheck: "true" }),
      memoryNotes: el("textarea", { id: "settings-memory-notes", class: "prose", rows: 4, spellcheck: "true" }),
      soul: el("textarea", { id: "settings-soul", class: "prose", rows: 7, spellcheck: "true", maxlength: this.memory?.soul_limit ?? 1500 }),
      soulEvolution: el("input", { id: "settings-soul-evolution", type: "checkbox", checked: config.soul_evolution !== false }),
      voiceInput: el(
        "select",
        { id: "settings-voice-input" },
        ["local", "openai", "off"].map((value) => el("option", { value, selected: config.voice_input === value }, t(`settings.voice.input.${value}`))),
      ),
      voiceModel: el(
        "select",
        { id: "settings-voice-model" },
        ["base", "small", "medium"].map((value) => el("option", { value, selected: config.voice_model === value }, t(`settings.voice.model.${value}`))),
      ),
      voiceReply: el(
        "select",
        { id: "settings-voice-reply" },
        ["voice", "always", "never"].map((value) => el("option", { value, selected: config.voice_reply === value }, t(`settings.voice.reply.${value}`))),
      ),
    };
    this.fields.soul.value = this.memory?.soul ?? "";
    this.fields.memoryUser.value = this.memory?.user ?? "";
    this.fields.memoryNotes.value = this.memory?.notes ?? "";
    this.fields.folders.value = config.allowed_paths.join("\n");
    this.toolBoxes = new Map();
    this.error = el("p", { class: "form-error", hidden: true });

    const sortedTools = tools.filter((tool) => !tool.server).sort(
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
        ...this.renderTabs([
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
        this.renderPermissions(config.always_allow ?? []),
        new McpSection({ api: this.api, toast: this.toast }).element(),
        this.renderMemory(),
        this.renderVoice(),
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
        ]),
      ),
      this.error,
      el(
        "div",
        { class: "settings-footer" },
        el("span", { class: "version" }, t("settings.version", { version: this.info().version })),
        this.openWelcome
          ? el("button", {
            class: "button ghost", type: "button",
            onclick: () => { this.dialog.close(); this.openWelcome(); },
          }, t("settings.welcome"))
          : null,
        el("button", { class: "button ghost", type: "button", onclick: () => this.dialog.close() }, t("settings.cancel")),
        el("button", { class: "button primary", type: "submit" }, t("settings.save")),
      ),
    );
    // A wrong value in a hidden tab: show that tab, so the browser can point at it.
    form.addEventListener("invalid", (event) => {
      const panel = event.target.closest("[role=tabpanel]");
      if (panel?.hidden) this.showTab(panel.dataset.tab);
    }, true);
    this.dialog.replaceChildren(form);
  }

  /** Actions allowed for good from an approval card (« Toujours autoriser »). */
  renderPermissions(rules) {
    const list = el("ul", { class: "permission-list" });
    const empty = el("p", { class: "hint" }, t("settings.permissions.empty"));
    const show = (entries) => {
      empty.hidden = entries.length > 0;
      list.replaceChildren(...entries.map((entry) => {
        const label = t(`tool.${entry.tool}`) === `tool.${entry.tool}` ? entry.tool : t(`tool.${entry.tool}`);
        const remove = el("button", { class: "button small ghost", type: "button", "aria-label": t("settings.permissions.remove_label", { what: entry.command ?? label }) }, t("settings.permissions.remove"));
        remove.addEventListener("click", async () => {
          try {
            show(await this.api.post("/api/permissions/remove", { rule: entry.rule }));
          } catch (error) {
            this.toast(error.message, "error");
          }
        });
        return el("li", { class: "permission", dataset: { rule: entry.rule } },
          el("div", {},
            el("div", { class: "tool-name" }, entry.command ? t("settings.permissions.command") : t("settings.permissions.every", { tool: label })),
            entry.command ? el("code", {}, entry.command) : null),
          remove);
      }));
    };
    show(rules.map((rule) => {
      const [tool, ...rest] = rule.split(" ");
      return rest.length ? { rule, tool, command: rest.join(" ") } : { rule, tool };
    }));
    return el(
      "section",
      { class: "settings-section", dataset: { section: "permissions" } },
      el("h3", {}, t("settings.permissions.title")),
      el("p", {}, t("settings.permissions.text")),
      empty,
      list,
    );
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

  /** What Kreafinity remembers: notebooks it curates, skills it learned. */
  renderMemory() {
    const memory = this.memory;
    const section = el(
      "section",
      { class: "settings-section", dataset: { section: "memory" } },
      el("h3", {}, t("settings.memory.title")),
      el("p", {}, t("settings.memory.text")),
      el("div", { class: "field-row two" }, el("label", { for: "settings-memory" }, t("settings.memory.enabled")), el("div", {}, this.fields.memory)),
      el("div", { class: "field-row two" }, el("label", { for: "settings-memory-review" }, t("settings.memory.review")), el("div", {}, this.fields.memoryReview)),
    );
    if (!memory) return section;
    const soulHint = el("p", { class: "hint", id: "settings-soul-seed", hidden: !memory.soul_is_seed }, t("settings.memory.soul_seed"));
    const resetSoul = el("button", { class: "button small ghost", type: "button", id: "settings-soul-reset", hidden: memory.soul_is_seed }, t("settings.memory.soul_reset"));
    const showSoul = () => {
      this.fields.soul.value = this.memory.soul;
      soulHint.hidden = !this.memory.soul_is_seed;
      resetSoul.hidden = this.memory.soul_is_seed;
    };
    resetSoul.addEventListener("click", async () => {
      if (!confirm(t("settings.memory.soul_reset_confirm"))) return;
      try {
        this.memory = await this.api.delete("/api/memory/soul");
        showSoul();
        this.toast(t("settings.memory.soul_was_reset"));
      } catch (error) {
        this.toast(error.message, "error");
      }
    });
    section.append(
      el("h4", {}, t("settings.memory.soul")),
      el("p", { class: "hint" }, t("settings.memory.soul_text")),
      soulHint,
      this.fields.soul,
      el("div", { class: "field-row two" }, el("label", { for: "settings-soul-evolution" }, t("settings.memory.soul_evolution")), el("div", {}, this.fields.soulEvolution)),
      el("div", {}, resetSoul),
      el("label", { for: "settings-memory-user" }, t("settings.memory.user")),
      this.fields.memoryUser,
      el("label", { for: "settings-memory-notes" }, t("settings.memory.notes")),
      this.fields.memoryNotes,
      el("h4", {}, t("settings.memory.skills")),
    );
    if (!memory.skills.length) section.append(el("p", { class: "hint" }, t("settings.memory.no_skills")));
    for (const skill of memory.skills) {
      const row = el(
        "div",
        { class: "skill-row" },
        el("div", {}, el("div", { class: "tool-name" }, skill.title), el("div", { class: "hint" }, skill.description)),
        el("span", { class: "hint" }, skill.uses ? t("settings.memory.uses", { count: skill.uses }) : t("settings.memory.unused")),
      );
      const remove = el("button", { class: "button small ghost", type: "button" }, t("settings.memory.delete_skill"));
      remove.addEventListener("click", async () => {
        try {
          this.memory = await this.api.delete(`/api/memory/skills/${encodeURIComponent(skill.name)}`);
          row.remove();
        } catch (error) {
          this.toast(error.message, "error");
        }
      });
      row.append(remove);
      section.append(row);
    }
    const forget = el("button", { class: "button small danger-ghost", type: "button", id: "settings-memory-forget" }, t("settings.memory.forget"));
    forget.addEventListener("click", async () => {
      if (!confirm(t("settings.memory.forget_confirm"))) return;
      try {
        this.memory = await this.api.delete("/api/memory");
        this.fields.memoryUser.value = "";
        this.fields.memoryNotes.value = "";
        showSoul();
        section.querySelectorAll(".skill-row").forEach((row) => row.remove());
        this.toast(t("settings.memory.forgotten"));
      } catch (error) {
        this.toast(error.message, "error");
      }
    });
    section.append(el("div", {}, forget));
    return section;
  }

  /** Installed version and « Rechercher maintenant » (updates). */
  /** Talking to Kreafinity: how it listens, whether it answers aloud, its voice engines. */
  renderVoice() {
    const field = (id, label, control) => el("div", { class: "field-row two" }, el("label", { for: id }, t(label)), control);
    this.voiceStatus = el("p", { class: "hint", id: "settings-voice-status" });
    this.voicePrepare = el("button", { class: "button small", type: "button", id: "settings-voice-prepare" }, t("settings.voice.prepare"));
    this.voicePrepare.addEventListener("click", async () => {
      try {
        this.voiceUpdated(await this.api.post("/api/voice/prepare"));
      } catch (error) {
        this.toast(error.message, "error");
      }
    });
    this.voiceUpdated(this.voice);
    return el(
      "section",
      { class: "settings-section", dataset: { section: "voice" } },
      el("h3", {}, t("settings.voice.title")),
      el("p", {}, t("settings.voice.text")),
      field("settings-voice-input", "settings.voice.input", this.fields.voiceInput),
      field("settings-voice-model", "settings.voice.model", this.fields.voiceModel),
      field("settings-voice-reply", "settings.voice.reply", this.fields.voiceReply),
      el("div", { class: "update-check" }, this.voicePrepare, this.voiceStatus),
    );
  }

  /** The voice engines' state changed (preparation started, finished or failed). */
  voiceUpdated(info) {
    this.voice = info;
    if (!this.voiceStatus) return;
    let key = "settings.voice.unavailable";
    if (info?.preparing) key = `settings.voice.${info.preparing}`;
    else if (info?.error) key = "settings.voice.error";
    else if (info?.listening && info?.speaking) key = "settings.voice.ready";
    else if (info?.listening) key = "settings.voice.ready_listening";
    else if (info) key = "settings.voice.missing";
    this.voiceStatus.textContent = t(key, { message: info?.error ?? "" });
    this.voicePrepare.disabled = Boolean(info?.preparing);
    this.voicePrepare.hidden = Boolean(info?.listening && info?.speaking && !info?.error);
  }

  renderUpdates() {
    const info = this.updates?.info;
    if (!info) return null;
    if (!info.supported) return el("p", { class: "hint" }, t(info.managed ? "settings.updates.managed" : "settings.updates.dev"));
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
    if (fields.memory.checked !== (this.config.memory !== false)) changes.memory = fields.memory.checked;
    if (fields.memoryReview.checked !== (this.config.memory_review !== false)) {
      changes.memory_review = fields.memoryReview.checked;
    }
    for (const [key, field] of [["voice_input", fields.voiceInput], ["voice_model", fields.voiceModel], ["voice_reply", fields.voiceReply]]) {
      if (field.value !== this.config[key]) changes[key] = field.value;
    }
    if (fields.soulEvolution.checked !== (this.config.soul_evolution !== false)) {
      changes.soul_evolution = fields.soulEvolution.checked;
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
      for (const [section, field] of [["user", fields.memoryUser], ["notes", fields.memoryNotes]]) {
        if (this.memory && field.value.trim() !== this.memory[section].trim()) {
          await this.api.put(`/api/memory/${section}`, { content: field.value });
        }
      }
      if (this.memory && fields.soul.value.trim() !== this.memory.soul.trim()) {
        await this.api.put("/api/memory/soul", { content: fields.soul.value });
      }
      const config = Object.keys(changes).length ? await this.api.patch("/api/config", changes) : this.config;
      this.dialog.close();
      this.onSaved(config, changes);
    } catch (error) {
      this.error.textContent = error instanceof ApiError ? error.message : String(error);
      this.error.hidden = false;
    }
  }
}
