// Settings › Extensions (MCP): servers adding tools to the assistant.
// Changes here are applied at once (not with the settings' « Enregistrer »).

import { el, icon } from "./dom.js";
import { t } from "./i18n.js";

/** Split a command line on spaces, keeping "quoted parts" together. */
export function splitCommand(text) {
  const parts = [];
  for (const match of text.matchAll(/"([^"]*)"|'([^']*)'|(\S+)/g)) parts.push(match[1] ?? match[2] ?? match[3]);
  return parts;
}

export class McpSection {
  constructor({ api, toast }) {
    this.api = api;
    this.toast = toast;
    this.section = el("section", { class: "settings-section", dataset: { section: "mcp" } });
  }

  element() {
    this.load();
    return this.section;
  }

  async load() {
    this.section.replaceChildren(
      el("h3", {}, t("mcp.title")),
      el("p", {}, t("mcp.text")),
      el("p", { class: "hint" }, t("mcp.loading")),
    );
    let data;
    try {
      data = await this.api.get("/api/mcp");
    } catch (error) {
      this.section.lastChild.textContent = error.message;
      return;
    }
    this.section.lastChild.remove();
    for (const server of data.servers) this.section.append(this.serverCard(server, data.keyring_available));
    if (!data.servers.length) this.section.append(el("p", { class: "hint" }, t("mcp.none")));
    this.section.append(this.addForm());
    // A server is starting: look again shortly (while the settings stay open).
    clearTimeout(this.refresh);
    if (data.servers.some((server) => server.status === "starting" || server.status === "stopped")) {
      this.refresh = setTimeout(() => this.section.isConnected && this.load(), 1500);
    }
  }

  async act(promise, message) {
    try {
      await promise;
      if (message) this.toast(message);
    } catch (error) {
      this.toast(error.message, "error");
    }
    await this.load();
  }

  serverCard(server, keyringAvailable) {
    const status = el("span", { class: `pill ${server.status === "ready" ? "ok" : ["error", "needs_secret"].includes(server.status) ? "missing" : ""}` },
      t(`mcp.status.${server.status}`));
    const enabled = el("input", { type: "checkbox", checked: server.enabled, "aria-label": t("mcp.enabled") });
    enabled.addEventListener("change", () =>
      this.act(this.api.patch(`/api/mcp/servers/${server.name}`, { enabled: enabled.checked })));
    const restart = el("button", { class: "button small ghost", type: "button" }, t("mcp.restart"));
    restart.addEventListener("click", () => this.act(this.api.post(`/api/mcp/servers/${server.name}/restart`)));
    const remove = el("button", { class: "button small danger-ghost", type: "button" }, t("mcp.remove"));
    remove.addEventListener("click", () => {
      if (window.confirm(t("mcp.remove_confirm", { name: server.name }))) {
        this.act(this.api.delete(`/api/mcp/servers/${server.name}`), t("mcp.removed", { name: server.name }));
      }
    });
    const card = el("div", { class: "mcp-card" },
      el("div", { class: "mcp-head" },
        el("label", { class: "mcp-switch" }, enabled, el("strong", {}, server.name)),
        status,
        el("span", { class: "mcp-actions" }, restart, remove)),
      el("code", { class: "mcp-target" }, server.target),
      server.error && server.status === "error" ? el("p", { class: "form-error" }, server.error) : null,
    );
    for (const secret of server.secrets) card.append(this.secretRow(server, secret, keyringAvailable));
    if (server.tools.length) {
      card.append(el("p", { class: "hint" }, t("mcp.tools_hint")));
      const list = el("div", { class: "mcp-tools" });
      for (const tool of server.tools) {
        const box = el("input", { type: "checkbox", checked: tool.auto_approve });
        box.addEventListener("change", () => {
          const approved = server.tools.filter((item) => (item.name === tool.name ? box.checked : item.auto_approve)).map((item) => item.name);
          this.act(this.api.patch(`/api/mcp/servers/${server.name}`, { auto_approve: approved }));
        });
        list.append(el("label", { class: "mcp-tool", title: tool.description },
          box,
          el("span", {}, tool.title),
          tool.destructive ? el("span", { class: "pill missing" }, t("mcp.destructive")) : null));
      }
      card.append(list);
    }
    return card;
  }

  secretRow(server, secret, keyringAvailable) {
    const input = el("input", { type: "password", autocomplete: "off", spellcheck: "false", placeholder: t("mcp.secret_placeholder"), disabled: !keyringAvailable });
    const save = el("button", { class: "button small", type: "button", disabled: !keyringAvailable }, t("mcp.secret_save"));
    save.addEventListener("click", () => {
      if (!input.value.trim()) return input.focus();
      this.act(this.api.put(`/api/mcp/servers/${server.name}/secrets/${secret.name}`, { value: input.value.trim() }), t("mcp.secret_saved"));
    });
    return el("div", { class: "mcp-secret" },
      el("span", { class: "mono" }, secret.name === "token" ? t("mcp.token") : secret.name),
      el("span", { class: `pill ${secret.stored ? "ok" : "missing"}` }, t(secret.stored ? "mcp.secret_stored" : "mcp.secret_missing")),
      input, save);
  }

  addForm() {
    const name = el("input", { id: "mcp-name", placeholder: "heure", autocomplete: "off", spellcheck: "false", maxlength: 24 });
    const command = el("input", { id: "mcp-command", placeholder: "uvx mcp-server-time --local-timezone=Indian/Reunion", autocomplete: "off", spellcheck: "false" });
    const secrets = el("input", { id: "mcp-secrets", placeholder: "GITHUB_PERSONAL_ACCESS_TOKEN", autocomplete: "off", spellcheck: "false" });
    const token = el("input", { id: "mcp-token", type: "checkbox" });
    const error = el("p", { class: "form-error", hidden: true });
    const submit = el("button", { class: "button small primary", type: "button" }, icon("plus"), el("span", {}, t("mcp.add")));
    submit.addEventListener("click", async () => {
      const target = command.value.trim();
      const body = { name: name.value.trim().toLowerCase() };
      if (/^https?:\/\//.test(target)) {
        body.url = target;
        body.token = token.checked;
      } else {
        const parts = splitCommand(target);
        body.command = parts[0] ?? "";
        body.args = parts.slice(1);
      }
      body.secret_env = secrets.value.split(/[\s,]+/).filter(Boolean);
      error.hidden = true;
      try {
        await this.api.post("/api/mcp/servers", body);
        this.toast(t("mcp.added", { name: body.name }));
        await this.load();
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
      }
    });
    return el("details", { class: "mcp-add" },
      el("summary", {}, t("mcp.add_title")),
      el("p", { class: "hint" }, t("mcp.add_text")),
      el("div", { class: "field-row two" }, el("label", { for: "mcp-name" }, t("mcp.name")), name),
      el("div", { class: "field-row two" }, el("label", { for: "mcp-command" }, t("mcp.command")), command),
      el("div", { class: "field-row two" }, el("label", { for: "mcp-secrets" }, t("mcp.secrets")), secrets),
      el("label", { class: "checkbox-row" }, token, el("span", {}, t("mcp.token_needed"))),
      el("p", { class: "hint" }, t("mcp.warning")),
      error,
      el("div", { class: "onboarding-actions" }, submit));
  }
}
