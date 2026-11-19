// Kreafinity web interface: conversations with the assistant, live answers,
// approval cards for actions on the computer, settings.

import { Api, ApiError, EventStream } from "./api.js";
import { el, hydrateIcons, icon, relativeTime, shortenPath } from "./dom.js";
import { language, setLanguage, t, translatePage } from "./i18n.js";
import { renderMarkdown } from "./markdown.js";
import { SettingsDialog, applyTheme } from "./settings.js";

const api = new Api();
const $ = (id) => document.getElementById(id);

const state = {
  info: null,
  config: null,
  providers: null,
  conversations: [],
  current: null, // the open conversation (with its messages), or null for a new one
  running: new Map(), // conversation id -> run id
  approvals: new Map(), // approval id -> approval_required event
  live: null, // what is being displayed of the running request
  models: new Map(), // provider name -> model ids
  everConnected: false,
};

let welcome;
let settings;

// --- Start -----------------------------------------------------------------------------

async function boot() {
  applyTheme();
  hydrateIcons();
  welcome = $("welcome");
  await setLanguage(navigator.language?.toLowerCase().startsWith("en") ? "en" : "fr");
  translatePage();
  try {
    state.info = await api.get("/api/info");
  } catch (error) {
    $("loading").hidden = true;
    if (error.status === 401) return showLogin();
    return fatal(error);
  }
  await start();
}

function showLogin() {
  $("login").hidden = false;
  $("login-token").focus();
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    api.setToken($("login-token").value.trim());
    try {
      state.info = await api.get("/api/info");
    } catch {
      api.setToken(null);
      $("login-error").textContent = t("login.invalid");
      $("login-error").hidden = false;
      return;
    }
    $("login").hidden = true;
    await start();
  });
}

function fatal(error) {
  document.body.replaceChildren(
    el("main", { class: "login" }, el("div", { class: "login-card" }, el("h1", {}, t("error.fatal")), el("p", {}, error.message))),
  );
}

async function start() {
  try {
    [state.config, state.providers, state.conversations] = await Promise.all([
      api.get("/api/config"),
      api.get("/api/providers"),
      api.get("/api/conversations"),
    ]);
  } catch (error) {
    return fatal(error);
  }
  if (state.config.language !== language()) {
    await setLanguage(state.config.language);
    translatePage();
  }
  settings = new SettingsDialog({ api, info: () => state.info, onSaved: settingsSaved, toast });
  bindInterface();
  renderWelcome();
  renderPicker();
  showNewConversation();
  $("loading").hidden = true;
  $("app").hidden = false;
  $("prompt").focus();
  new EventStream(api, { onEvent, onStatus: setConnected }).connect();
}

function bindInterface() {
  $("new-conversation").addEventListener("click", () => {
    showNewConversation();
    closeSidebar();
  });
  $("open-settings").addEventListener("click", () => settings.open());
  $("open-sidebar").addEventListener("click", () => $("app").classList.add("sidebar-open"));
  $("close-sidebar").addEventListener("click", closeSidebar);
  $("sidebar-backdrop").addEventListener("click", closeSidebar);
  $("rename-conversation").addEventListener("click", renameConversation);
  $("key-warning-button").addEventListener("click", () => settings.open("keys"));
  $("composer").addEventListener("submit", (event) => {
    event.preventDefault();
    send($("prompt").value);
  });
  $("prompt").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send($("prompt").value);
    }
  });
  $("prompt").addEventListener("input", autosize);
  $("stop").addEventListener("click", stopRequest);
  $("provider-select").addEventListener("change", () => {
    $("model-input").value = "";
    pickerChanged();
  });
  $("model-input").addEventListener("change", pickerChanged);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeSidebar();
  });
}

function closeSidebar() {
  $("app").classList.remove("sidebar-open");
}

function setConnected(online) {
  $("connection").classList.toggle("online", online);
  $("connection-text").textContent = t(online ? "connection.online" : "connection.offline");
}

// --- Sidebar --------------------------------------------------------------------------

function renderSidebar() {
  const list = $("conversation-list");
  if (!state.conversations.length) {
    list.replaceChildren(el("p", { class: "empty" }, t("sidebar.empty")));
    return;
  }
  list.replaceChildren(...state.conversations.map(conversationItem));
}

function conversationItem(conversation) {
  const active = state.current?.id === conversation.id;
  const waiting = [...state.approvals.values()].some((a) => a.conversation_id === conversation.id);
  const title = conversation.title || t("conversation.untitled");
  return el(
    "div",
    { class: `conversation-item${active ? " active" : ""}`, dataset: { id: conversation.id } },
    el(
      "button",
      { class: "conversation-open", type: "button", "aria-current": active ? "page" : null, onclick: () => openConversation(conversation.id) },
      el("span", { class: "conversation-name" }, title),
      el(
        "span",
        { class: "conversation-meta" },
        waiting ? el("span", { class: "badge attention", title: t("sidebar.waiting") }) : null,
        state.running.has(conversation.id) ? el("span", { class: "running-dot", title: t("sidebar.running") }) : null,
        relativeTime(conversation.updated_at, language()),
      ),
    ),
    el(
      "button",
      { class: "icon-button", type: "button", "aria-label": t("conversation.delete"), title: t("conversation.delete"), onclick: () => deleteConversation(conversation) },
      icon("trash"),
    ),
  );
}

function upsertConversation(info) {
  const index = state.conversations.findIndex((c) => c.id === info.id);
  if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...info };
  else state.conversations.unshift(info);
  state.conversations.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  if (state.current?.id === info.id) {
    state.current.title = info.title;
    state.current.provider = info.provider;
    renderHeader();
  }
  renderSidebar();
}

async function deleteConversation(conversation) {
  const title = conversation.title || t("conversation.untitled");
  if (!confirm(t("conversation.delete_confirm", { title }))) return;
  try {
    await api.delete(`/api/conversations/${conversation.id}`);
  } catch (error) {
    toast(errorMessage(error), "error");
  }
}

async function renameConversation() {
  if (!state.current) return;
  const title = prompt(t("conversation.rename_prompt"), state.current.title ?? "");
  if (!title?.trim()) return;
  try {
    upsertConversation(await api.patch(`/api/conversations/${state.current.id}`, { title: title.trim() }));
  } catch (error) {
    toast(errorMessage(error), "error");
  }
}

// --- Provider and model picker ----------------------------------------------------------

function providerInfo(name) {
  return state.providers.providers.find((p) => p.name === name);
}

function renderPicker() {
  $("provider-select").replaceChildren(
    el("option", { value: "" }, t("picker.default", { spec: state.providers.default })),
    ...state.providers.providers.map((p) => el("option", { value: p.name }, p.display_name)),
  );
  syncPicker();
}

function syncPicker() {
  const spec = state.current?.provider;
  const [name, ...model] = spec ? spec.split("/") : [""];
  $("provider-select").value = providerInfo(name) ? name : "";
  $("model-input").value = model.join("/");
  pickerChanged();
}

function effectiveProvider() {
  return $("provider-select").value || state.providers.default.split("/")[0];
}

function pickerSpec() {
  const name = $("provider-select").value;
  if (!name) return null;
  const model = $("model-input").value.trim();
  return model ? `${name}/${model}` : name;
}

function pickerChanged() {
  $("model-input").placeholder = t("picker.model_default");
  $("model-input").hidden = !$("provider-select").value;
  const provider = providerInfo(effectiveProvider());
  const missingKey = provider?.requires_api_key && provider.api_key === "missing";
  $("key-warning").hidden = !missingKey;
  if (missingKey) $("key-warning-text").textContent = t("picker.key_missing", { provider: provider.display_name });
  if (provider && !missingKey) loadModels(provider.name);
}

async function loadModels(name) {
  if (!state.models.has(name)) {
    state.models.set(name, []);
    try {
      state.models.set(name, (await api.get(`/api/providers/${name}/models`)).map((m) => m.id));
    } catch {
      state.models.delete(name); // not reachable now: try again later
      return;
    }
  }
  if (effectiveProvider() === name) {
    $("model-options").replaceChildren(...state.models.get(name).map((id) => el("option", { value: id })));
  }
}

async function refreshProviders() {
  state.providers = await api.get("/api/providers");
  const selected = $("provider-select").value;
  const model = $("model-input").value;
  renderPicker();
  $("provider-select").value = selected;
  $("model-input").value = model;
  pickerChanged();
}

// --- Conversations ------------------------------------------------------------------------

function renderWelcome() {
  // The welcome screen is not in the page while a conversation is shown.
  translatePage(welcome);
  welcome.querySelector("#welcome-title").textContent = t("welcome.title", { name: state.info.user_name });
  welcome.querySelector("#suggestions").replaceChildren(
    ...[1, 2, 3, 4].map((n) =>
      el("button", { class: "suggestion", type: "button", onclick: () => send(t(`welcome.suggestion.${n}`)) }, t(`welcome.suggestion.${n}`)),
    ),
  );
}

function renderHeader() {
  $("conversation-title").textContent = state.current?.title || t("conversation.new");
  $("rename-conversation").hidden = !state.current;
  document.title = state.current?.title ? `${state.current.title} — Kreafinity` : "Kreafinity";
}

function showNewConversation() {
  state.current = null;
  state.live = null;
  $("messages").replaceChildren(welcome);
  renderHeader();
  renderSidebar();
  syncPicker();
  updateComposer();
  $("prompt").focus();
}

async function openConversation(id) {
  closeSidebar();
  try {
    state.current = await api.get(`/api/conversations/${id}`);
  } catch (error) {
    toast(errorMessage(error), "error");
    return;
  }
  renderConversation();
  renderHeader();
  renderSidebar();
  syncPicker();
  updateComposer();
  $("prompt").focus();
}

function renderConversation() {
  const container = $("messages");
  container.replaceChildren();
  state.live = null;
  const conversation = state.current;
  if (!conversation.messages.length && !state.running.has(conversation.id)) {
    container.append(welcome);
    return;
  }
  let assistant = null;
  const chips = new Map();
  for (const message of conversation.messages) {
    if (message.role === "user") {
      assistant = null;
      container.append(userMessage(message.content));
    } else if (message.role === "assistant") {
      assistant ??= appendAssistant(container);
      if (message.content) assistant.body.append(markdownBlock(message.content));
      for (const call of message.tool_calls ?? []) {
        const chip = toolChip(call.id, call.name, describeCall(call));
        chips.set(call.id, chip);
        assistant.body.append(chip);
      }
    } else if (message.role === "tool") {
      const chip = chips.get(message.tool_call_id);
      if (chip) finishChip(chip, statusOfResult(message), message.content);
    }
  }
  if (state.running.has(conversation.id)) {
    state.live = newLive(conversation.id, state.running.get(conversation.id), assistant ?? appendAssistant(container));
    showThinking(state.live);
  }
  for (const approval of state.approvals.values()) {
    if (approval.conversation_id !== conversation.id) continue;
    const target = state.live?.assistant ?? assistant ?? appendAssistant(container);
    target.body.append(approvalCard(approval));
  }
  scrollToBottom(true);
}

function userMessage(text) {
  return el("div", { class: "message user" }, el("div", { class: "bubble" }, text));
}

function appendAssistant(container) {
  const body = el("div", { class: "body" });
  const root = el("article", { class: "message assistant" }, el("div", { class: "avatar" }, el("span", { class: "logo-mark" })), body);
  container.append(root);
  return { root, body };
}

function markdownBlock(text) {
  return el("div", { class: "markdown" }, renderMarkdown(text));
}

function describeCall(call) {
  const args = Object.entries(call.arguments ?? {})
    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    .join(", ");
  return `${call.name}(${args})`;
}

function statusOfResult(message) {
  if (!message.is_error) return "ok";
  if (message.content.startsWith("The user denied") || message.content.startsWith("This action needs")) return "denied";
  if (message.content.startsWith("Not executed")) return "skipped";
  return "error";
}

function toolChip(callId, name, description) {
  const key = `tool.${name}`;
  const label = t(key) === key ? name : t(key);
  return el(
    "details",
    { class: "tool running", dataset: { callId } },
    el(
      "summary",
      {},
      el("span", { class: "tool-status", "aria-hidden": "true" }),
      el("span", { class: "tool-label" }, label),
      el("span", { class: "tool-detail" }, shortenPath(description, state.info.home)),
    ),
    el("pre", { class: "tool-result" }, t("tool.pending")),
  );
}

function finishChip(chip, status, result) {
  chip.classList.remove("running");
  chip.classList.add(status);
  const pre = chip.querySelector(".tool-result");
  let text = result ?? "";
  try {
    text = JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    /* not JSON: shown as is */
  }
  pre.textContent = shortenPath(text, state.info.home) || t(`tool.status.${status}`);
  chip.querySelector("summary").title = t(`tool.status.${status}`);
}

// --- Approval cards -------------------------------------------------------------------------

function approvalCard(approval) {
  const danger = approval.risk === "danger";
  const allow = el("button", { class: `button ${danger ? "danger" : "primary"}`, type: "button" }, t(danger ? "approval.allow_danger" : "approval.allow"));
  const deny = el("button", { class: "button ghost", type: "button" }, t("approval.deny"));
  const card = el(
    "div",
    { class: `approval ${danger ? "danger" : "confirm"}`, dataset: { approvalId: approval.approval_id }, role: "group", "aria-label": t(danger ? "approval.title.danger" : "approval.title.confirm") },
    el("div", { class: "approval-head" }, icon(danger ? "alert" : "shield"), el("span", {}, t(danger ? "approval.title.danger" : "approval.title.confirm"))),
    el("p", { class: "approval-summary" }, approval.summary),
    approval.details.length ? el("ul", { class: "approval-details" }, approval.details.map((line) => el("li", {}, shortenPath(line, state.info.home)))) : null,
  );
  if (danger) {
    const id = `confirm-${approval.approval_id}`;
    const checkbox = el("input", { type: "checkbox", id });
    allow.disabled = true;
    checkbox.addEventListener("change", () => {
      allow.disabled = !checkbox.checked;
    });
    card.append(el("label", { class: "approval-confirm", for: id }, checkbox, t("approval.confirm_check")));
  }
  const actions = el("div", { class: "approval-actions" }, allow, deny);
  card.append(actions, el("p", { class: "approval-state", hidden: true }));
  allow.addEventListener("click", () => decide(approval, true, card));
  deny.addEventListener("click", () => decide(approval, false, card));
  return card;
}

async function decide(approval, approved, card) {
  for (const button of card.querySelectorAll("button, input")) button.disabled = true;
  try {
    await api.post(`/api/approvals/${approval.approval_id}`, { approved });
  } catch (error) {
    if (error.status === 404) {
      settleApprovalCard(card, { approved: false, reason: "timeout" });
    } else {
      for (const button of card.querySelectorAll("button, input")) button.disabled = false;
      toast(errorMessage(error), "error");
    }
  }
}

function settleApprovalCard(card, event) {
  card.classList.add(event.approved ? "approved" : "denied");
  card.querySelector(".approval-actions")?.remove();
  card.querySelector(".approval-confirm")?.remove();
  const status = card.querySelector(".approval-state");
  status.textContent = t(event.approved ? "approval.approved" : event.reason === "timeout" ? "approval.expired" : "approval.denied");
  status.hidden = false;
}

// --- Sending ------------------------------------------------------------------------------------

function autosize() {
  const prompt = $("prompt");
  prompt.style.height = "auto";
  prompt.style.height = `${Math.min(prompt.scrollHeight, 220)}px`;
}

function updateComposer() {
  const running = Boolean(state.current && state.running.has(state.current.id));
  $("send").hidden = running;
  $("stop").hidden = !running;
}

async function send(rawText) {
  const text = rawText.trim();
  if (!text || (state.current && state.running.has(state.current.id))) return;
  $("send").disabled = true;
  try {
    if (!state.current) {
      const created = await api.post("/api/conversations");
      state.current = { ...created, messages: [] };
      upsertConversation(created);
      renderHeader();
      $("messages").replaceChildren();
    }
    const body = { content: text };
    const spec = pickerSpec();
    if (spec && spec !== state.current.provider) body.provider = spec;
    else if (!spec && state.current.provider && state.current.provider !== state.config.provider) body.provider = state.config.provider;
    $("prompt").value = "";
    autosize();
    await api.post(`/api/conversations/${state.current.id}/messages`, body);
  } catch (error) {
    $("prompt").value = text;
    autosize();
    toast(errorMessage(error), "error");
  } finally {
    $("send").disabled = false;
  }
}

async function stopRequest() {
  if (!state.current) return;
  try {
    await api.post(`/api/conversations/${state.current.id}/cancel`);
  } catch (error) {
    toast(errorMessage(error), "error");
  }
}

// --- Live events -----------------------------------------------------------------------------------

function newLive(conversationId, runId, assistant) {
  return { conversationId, runId, assistant, segment: null, text: "", frame: 0, thinking: null };
}

/** The live display for an event of the open conversation (created on demand). */
function liveFor(event) {
  if (!state.current || event.conversation_id !== state.current.id) return null;
  if (!state.live || state.live.runId !== event.run_id) {
    state.live = newLive(event.conversation_id, event.run_id, appendAssistant($("messages")));
  }
  return state.live;
}

function showThinking(live) {
  if (live.thinking) return;
  live.thinking = el("div", { class: "thinking", "aria-label": t("chat.thinking") }, el("span"), el("span"), el("span"));
  live.assistant.body.append(live.thinking);
  scrollToBottom();
}

function hideThinking(live) {
  live.thinking?.remove();
  live.thinking = null;
}

function render(live) {
  live.frame = 0;
  live.segment?.replaceChildren(renderMarkdown(live.text));
  scrollToBottom();
}

function scheduleRender(live) {
  if (!live.frame) live.frame = requestAnimationFrame(() => render(live));
}

function flushRender(live) {
  if (live.frame) cancelAnimationFrame(live.frame);
  render(live);
}

function addToLive(live, node) {
  flushRender(live);
  hideThinking(live);
  live.segment = null;
  live.assistant.body.append(node);
  scrollToBottom(true);
}

function onEvent(event) {
  switch (event.type) {
    case "ready":
      return onReady(event);
    case "run_started": {
      state.running.set(event.conversation_id, event.run_id);
      if (state.current?.id === event.conversation_id) {
        welcome.remove();
        $("messages").append(userMessage(event.content));
        state.live = newLive(event.conversation_id, event.run_id, appendAssistant($("messages")));
        showThinking(state.live);
        scrollToBottom(true);
      }
      updateComposer();
      renderSidebar();
      break;
    }
    case "text_delta": {
      const live = liveFor(event);
      if (!live) break;
      hideThinking(live);
      if (!live.segment) {
        live.text = "";
        live.segment = markdownBlock("");
        live.assistant.body.append(live.segment);
      }
      live.text += event.text;
      scheduleRender(live);
      break;
    }
    case "tool_started": {
      const live = liveFor(event);
      if (live) addToLive(live, toolChip(event.call_id, event.tool, event.description));
      break;
    }
    case "approval_required": {
      state.approvals.set(event.approval_id, event);
      const live = liveFor(event);
      if (live) {
        addToLive(live, approvalCard(event));
        const card = live.assistant.body.lastElementChild;
        (card.querySelector("input[type=checkbox]") ?? card.querySelector("button"))?.focus();
      } else {
        toast(t("approval.elsewhere"), "warning", {
          label: t("approval.show"),
          run: () => openConversation(event.conversation_id),
        });
      }
      renderSidebar();
      break;
    }
    case "approval_resolved": {
      state.approvals.delete(event.approval_id);
      const card = document.querySelector(`.approval[data-approval-id="${CSS.escape(event.approval_id)}"]`);
      if (card) settleApprovalCard(card, event);
      renderSidebar();
      break;
    }
    case "tool_finished": {
      const chip = document.querySelector(`.tool[data-call-id="${CSS.escape(event.call_id)}"]`);
      if (chip) finishChip(chip, event.status, event.result);
      const live = state.live?.runId === event.run_id ? state.live : null;
      if (live) showThinking(live);
      break;
    }
    case "provider_fallback": {
      const live = liveFor(event);
      const text = t("notice.fallback", { unavailable: event.unavailable, fallback: event.fallback });
      if (live) addToLive(live, el("div", { class: "notice info" }, text));
      else toast(text, "warning");
      break;
    }
    case "run_finished":
    case "run_failed":
      return finishRun(event);
    case "conversation_updated":
      return upsertConversation(event.conversation);
    case "conversation_deleted": {
      state.conversations = state.conversations.filter((c) => c.id !== event.conversation_id);
      if (state.current?.id === event.conversation_id) showNewConversation();
      else renderSidebar();
      break;
    }
    case "config_updated":
      state.config = event.config;
      break;
    case "providers_updated":
      refreshProviders();
      break;
    default:
      break;
  }
}

function finishRun(event) {
  state.running.delete(event.conversation_id);
  const live = state.live?.runId === event.run_id ? state.live : null;
  if (live) {
    flushRender(live);
    hideThinking(live);
    const notice = runNotice(event);
    if (notice) live.assistant.body.append(notice);
    state.live = null;
    scrollToBottom();
  }
  updateComposer();
  renderSidebar();
}

function runNotice(event) {
  if (event.type === "run_finished") {
    if (event.reason === "completed") return null;
    return el("div", { class: "notice warning" }, t(`notice.${event.reason}`));
  }
  if (event.code === "provider_auth") {
    return el(
      "div",
      { class: "notice error" },
      el("span", {}, t("notice.provider_auth")),
      el("button", { class: "button small", type: "button", onclick: () => settings.open("keys") }, t("picker.add_key")),
    );
  }
  const key = event.code === "provider_unavailable" ? "notice.provider_unavailable" : "notice.error";
  return el("div", { class: "notice error" }, t(key, { message: event.message }));
}

async function onReady(event) {
  state.running = new Map(event.running.map((run) => [run.conversation_id, run.run_id]));
  state.approvals = new Map(event.pending_approvals.map((approval) => [approval.approval_id, approval]));
  const reconnection = state.everConnected;
  state.everConnected = true;
  if (reconnection) {
    // Events may have been missed while disconnected: reload what is shown.
    try {
      state.conversations = await api.get("/api/conversations");
    } catch {
      /* the next reconnection will try again */
    }
    if (state.current) return openConversation(state.current.id);
  } else if (state.current) {
    renderConversation();
  }
  renderSidebar();
  updateComposer();
}

// --- Settings ----------------------------------------------------------------------------------------

async function settingsSaved(config, changes) {
  state.config = config;
  if (changes.language) {
    await setLanguage(config.language);
    translatePage();
    renderWelcome();
    renderHeader();
    renderSidebar();
  }
  await refreshProviders();
  toast(t("settings.saved"));
}

// --- Utilities ----------------------------------------------------------------------------------------

function scrollToBottom(force = false) {
  const container = $("messages");
  const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 140;
  if (force || nearBottom) container.scrollTop = container.scrollHeight;
}

function errorMessage(error) {
  if (error instanceof ApiError) {
    const key = `error.${error.code}`;
    const translated = t(key);
    return translated === key ? error.message : translated;
  }
  return String(error);
}

function toast(text, type = "info", action) {
  const node = el(
    "div",
    { class: `toast ${type}`, role: type === "error" ? "alert" : "status" },
    el("span", {}, text),
    action
      ? el("button", { class: "button small", type: "button", onclick: () => { node.remove(); action.run(); } }, action.label)
      : null,
  );
  $("toasts").append(node);
  setTimeout(() => node.remove(), action ? 12000 : 6000);
}

boot();
