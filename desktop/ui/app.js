const byId = (id) => document.getElementById(id);
const all = (selector) => document.querySelectorAll(selector);

let token = sessionStorage.getItem("loomis-token");
let project, socket, magicTimer;
let selection = 0;
let magicAttempt = 0;
let statusRevision = 0;
let specBusy = false;
let actionBusy = false;
let messageBusy = false;
let selectedAgent;
let attachment, recording, microphonePending;
let telegramLogsEnabled = false;
const teamTargets = new Set();
const deletedAgents = new Set();
const agentSelector = byId("agent-selector");
const agentMenu = byId("agent-menu");

const icon = (paths) => `<svg viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>`;

async function api(path, body, accessToken = token, method = body === undefined ? "GET" : "POST") {
  const headers = {};
  const multipart = body instanceof FormData;
  if (accessToken) headers.Authorization = "Bearer " + accessToken;
  if (body !== undefined && !multipart) headers["Content-Type"] = "application/json";
  const response = await fetch("/api/" + path, {
    headers, method, body: multipart ? body : JSON.stringify(body),
  });
  if (response.status === 204) return;
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join(", ") : data.detail || "Ошибка запроса");
    error.status = response.status;
    error.detail = data.detail;
    throw error;
  }
  return data;
}

function notify(message) {
  const toast = document.createElement("div");
  toast.className = "toast glass-panel";
  toast.textContent = message;
  byId("toasts").append(toast);
  setTimeout(() => toast.remove(), 4500);
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("loomis-theme", theme);
  all(".theme-switch").forEach((button) => {
    button.innerHTML = icon(theme === "dark"
      ? '<path d="M20.9 13.3A9 9 0 0 1 10.7 3.1a9 9 0 1 0 10.2 10.2Z"/>'
      : '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.4 1.4m11.2 11.2L19 19M5 19l1.4-1.4M17.6 6.4 19 5"/>');
    button.setAttribute("aria-label", theme === "dark" ? "Светлая тема" : "Тёмная тема");
    button.title = theme === "dark" ? "Dark Liquid Glass" : "Light iOS Glass";
    button.setAttribute("aria-pressed", theme === "light");
  });
}
setTheme(localStorage.getItem("loomis-theme") === "light" ? "light" : "dark");
all(".theme-switch").forEach((button) => button.onclick = () => {
  setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});

function stopMagic() {
  magicAttempt++;
  clearTimeout(magicTimer);
  byId("magic-submit").disabled = false;
  byId("magic-status").classList.add("hidden");
  byId("magic-form").classList.remove("hidden");
}
byId("magic-cancel").onclick = stopMagic;
all("[data-auth-tab]").forEach((tab) => tab.onclick = () => {
  stopMagic();
  byId("auth-error").textContent = "";
  all("[data-auth-tab]").forEach((button) => {
    button.classList.toggle("active", button === tab);
    button.setAttribute("aria-pressed", button === tab);
  });
  all(".auth-pane").forEach((pane) => pane.classList.toggle("active", pane.id === "auth-" + tab.dataset.authTab));
});

function showWorkspace(visible) {
  const auth = byId("auth-screen");
  const workspace = byId("app-screen");
  auth.inert = visible;
  workspace.inert = !visible;
  auth.classList.toggle("hidden", visible);
  workspace.classList.toggle("hidden", !visible);
}

async function signIn(accessToken, isCurrent = () => true) {
  const user = await api("auth/me", undefined, accessToken);
  if (!isCurrent()) return;
  token = accessToken;
  telegramLogsEnabled = user.tg_logs_enabled;
  renderTelegramLogs();
  sessionStorage.setItem("loomis-token", token);
  byId("user-name").textContent = user.username || "Loomis";
  byId("user-avatar").textContent = byId("user-name").textContent[0].toUpperCase();
  byId("telegram-dot").classList.toggle("online", Boolean(user.telegram_id));
  byId("telegram-label").textContent = user.telegram_id ? "Привязан" : "Не привязан";
  showWorkspace(true);
  try {
    await loadProjects();
  } catch (error) {
    if (token === accessToken) notify(error.message);
  }
}

let registering = false;
byId("auth-mode-toggle").onclick = () => {
  registering = !registering;
  byId("auth-submit").textContent = registering ? "Зарегистрироваться" : "Войти в Loomis";
  byId("auth-mode-toggle").textContent = registering ? "Уже есть аккаунт? Войти" : "Нет аккаунта? Зарегистрироваться";
  byId("auth-password-input").autocomplete = registering ? "new-password" : "current-password";
};
byId("password-form").onsubmit = async (event) => {
  event.preventDefault();
  const button = byId("auth-submit");
  const magicTab = document.querySelector('[data-auth-tab="magic"]');
  magicTab.disabled = true;
  button.disabled = true;
  byId("auth-error").textContent = "";
  try {
    const credentials = {username: byId("auth-username").value, password: byId("auth-password-input").value};
    if (registering) await api("auth/register", credentials);
    await signIn((await api("auth/login", credentials)).access_token);
    event.target.reset();
  } catch (error) {
    byId("auth-error").textContent = error.message;
  } finally {
    button.disabled = false;
    magicTab.disabled = false;
  }
};
byId("magic-form").onsubmit = async (event) => {
  event.preventDefault();
  const attempt = ++magicAttempt;
  byId("magic-submit").disabled = true;
  byId("auth-error").textContent = "";
  const fail = (error) => {
    if (attempt !== magicAttempt) return;
    stopMagic();
    byId("auth-error").textContent = error.message;
  };
  try {
    const request = await api("auth/magic-login/request", {username: byId("magic-username").value});
    if (attempt !== magicAttempt) return;
    const expires = Date.now() + request.expires_in * 1000;
    byId("magic-status").classList.remove("hidden");
    byId("magic-form").classList.add("hidden");
    async function poll() {
      try {
        const remaining = Math.max(0, Math.ceil((expires - Date.now()) / 1000));
        byId("magic-countdown").textContent = Math.floor(remaining / 60) + ":" + String(remaining % 60).padStart(2, "0");
        if (!remaining) throw new Error("Время подтверждения истекло.");
        const status = await api("auth/magic-login/status/" + request.request_id);
        if (attempt !== magicAttempt) return;
        if (status.status === "approved") {
          await signIn(status.access_token, () => attempt === magicAttempt);
          if (attempt === magicAttempt) stopMagic();
        } else if (status.status === "pending") {
          magicTimer = setTimeout(poll, 1000);
        } else {
          throw new Error("Вход через Telegram отклонён.");
        }
      } catch (error) {
        fail(error);
      }
    }
    await poll();
  } catch (error) {
    fail(error);
  }
};


const streams = {team: new Map(), direct: new Map(), terminal: new Map()};
const traffic = new Map(), lights = new Map();
let agentsInProject = [], terminalAgent, view = "team", frame;
const timeFormat = new Intl.DateTimeFormat("ru", {dateStyle: "short", timeStyle: "medium"});

function element(tag, css, text) {
  const node = document.createElement(tag);
  if (css) node.className = css;
  if (text != null) node.textContent = text;
  return node;
}

function inlineMarkdown(node, text) {
  let offset = 0;
  for (const match of text.matchAll(/\x60([^\x60]+)\x60|\*\*([^*]+)\*\*|__([^_]+)__/g)) {
    node.append(text.slice(offset, match.index), element(match[1] ? "code" : "strong", "", match[1] ?? match[2] ?? match[3]));
    offset = match.index + match[0].length;
  }
  node.append(text.slice(offset));
}

function codeCard(language, source) {
  const card = element("div", "code-card");
  const head = element("div", "code-head");
  const copy = element("button", "text-action", "Копировать");
  copy.type = "button";
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(source);
      copy.textContent = "Скопировано";
      setTimeout(() => { copy.textContent = "Копировать"; }, 1600);
    } catch (error) {
      notify(error.message);
    }
  };
  head.append(element("span", "", language || "Код"), copy);
  const pre = element("pre"), code = element("code");
  let offset = 0;
  const tokens = /("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|\/\/[^\n]*|\b(?:const|let|return|function|class|import|from|def|async|await|if|else|for|true|false|null|None)\b|\b\d+(?:\.\d+)?\b)/g;
  for (const match of source.matchAll(tokens)) {
    code.append(source.slice(offset, match.index), element("span", "code-token", match[0]));
    offset = match.index + match[0].length;
  }
  code.append(source.slice(offset));
  pre.append(code);
  card.append(head, pre);
  return card;
}

function renderMarkdown(body, text, terminal) {
  const lines = text.split(/\r?\n/);
  let list;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const item = line.match(/^\s*(?:[-+*]|\d+[.)])\s+(.+)/);
    if (item) {
      const tag = /^\s*\d/.test(line) ? "ol" : "ul";
      if (list?.localName !== tag) {
        list = element(tag);
        if (tag === "ol") list.start = parseInt(line, 10);
        body.append(list);
      }
      const entry = element("li");
      inlineMarkdown(entry, item[1]);
      list.append(entry);
      continue;
    }
    list = undefined;
    const fence = line.match(/^\s*\x60{3,}\s*([\w+#.-]*)\s*$/);
    if (fence) {
      const code = [];
      while (++i < lines.length && !/^\s*\x60{3,}\s*$/.test(lines[i])) code.push(lines[i]);
      body.append(codeCard(fence[1], code.join("\n")));
    } else if (terminal) {
      body.append(line + (i + 1 < lines.length ? "\n" : ""));
    } else if (line.trim()) {
      const heading = line.match(/^(#{1,6})\s+(.+)/);
      const block = element(heading ? "h" + heading[1].length : "p");
      inlineMarkdown(block, heading ? heading[2] : line);
      body.append(block);
    }
  }
}

function messageNode(event, channel) {
  const own = event.sender_name === "User";
  const entry = element("article", channel === "terminal" ? "terminal-entry" : own ? "chat-message own-message" : "chat-message");
  const head = element("header", "message-header");
  const name = event.sender_name || event.agent_name || agentsInProject.find((agent) => agent.id === event.agent_id)?.name;
  head.append(element(channel === "terminal" ? "h4" : "strong", "", own ? "Вы" : name || "Агент"));
  if (channel === "team" && event.target_name) head.append(element("span", "recipient", event.target_name));
  const created = event.created_at;
  const stamp = created ? new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(created) ? created : created + "Z") : new Date();
  const time = element("time", "", timeFormat.format(stamp));
  time.dateTime = stamp.toISOString();
  head.append(time);
  const body = element("div", "message-body");
  let content = String(event.content ?? event.message ?? "");
  if (channel === "terminal") {
    content = content.replace(/[0-9#*]\uFE0F?\u20E3|(?:\p{Extended_Pictographic}|\p{Emoji_Presentation})\uFE0F?[\u{E0020}-\u{E007F}]*(?:\u200D(?=\p{Extended_Pictographic}))?/gu, "");
  }
  renderMarkdown(body, content, channel === "terminal");
  entry.append(head, body);
  return entry;
}

function refreshStream(reset = false) {
  const pane = byId(view);
  const key = {team: 0, direct: selectedAgent?.id, terminal: terminalAgent}[view];
  const entries = streams[view].get(key) || [];
  const atBottom = reset || pane.scrollHeight - pane.scrollTop - pane.clientHeight < 20;
  if (reset) pane.replaceChildren();
  while (pane.firstChild && pane.firstChild !== entries[0]?.node) pane.firstChild.remove();
  for (const item of entries) {
    item.node ??= messageNode(item.event, view);
    if (item.node.parentNode !== pane) pane.append(item.node);
  }
  if (view === "terminal") {
    byId("terminal-count").textContent = entries.length + " записей";
    byId("clear-terminal").disabled = !entries.length;
  }
  if (atBottom) pane.scrollTop = pane.scrollHeight;
}

function receiveMessage(event, channel) {
  const key = channel === "team" ? 0 : event.agent_id;
  if (key == null || deletedAgents.has(event.agent_id)) return;
  const bucket = streams[channel];
  if (!bucket.has(key)) bucket.set(key, []);
  const entries = bucket.get(key);
  if (event.id != null && entries.some((item) => item.event.id === event.id)) return;
  entries.push({event});
  if (entries.length > 500) entries.shift();
  if (view === channel) frame ??= requestAnimationFrame(() => {
    frame = undefined;
    refreshStream();
  });
}

function renderTerminalTabs() {
  if (!agentsInProject.some((agent) => agent.id === terminalAgent)) terminalAgent = agentsInProject[0]?.id;
  byId("terminal-tabs").replaceChildren(...agentsInProject.map((agent) => {
    const tab = element("button", "tab", agent.name);
    tab.type = "button";
    tab.setAttribute("aria-pressed", String(agent.id === terminalAgent));
    tab.onclick = () => {
      terminalAgent = agent.id;
      for (const button of tab.parentElement.children) button.setAttribute("aria-pressed", String(button === tab));
      refreshStream(true);
    };
    return tab;
  }));
  if (view === "terminal") refreshStream(true);
}

byId("clear-terminal").onclick = () => {
  streams.terminal.delete(terminalAgent);
  refreshStream(true);
};

const terminalFontKey = "loomis-terminal-font";
let terminalFont = Number(localStorage.getItem(terminalFontKey)) || 13;
function setTerminalFont(size) {
  terminalFont = Math.max(10, Math.min(28, size));
  byId("terminal").style.setProperty("--terminal-font", terminalFont + "px");
  byId("font-smaller").disabled = terminalFont === 10;
  byId("font-larger").disabled = terminalFont === 28;
  localStorage.setItem(terminalFontKey, String(terminalFont));
}
setTerminalFont(terminalFont);
byId("font-smaller").onclick = () => setTerminalFont(terminalFont - 1);
byId("font-larger").onclick = () => setTerminalFont(terminalFont + 1);

function choiceChip(label, checked, onChange) {
  const chip = element("label", "choice-chip");
  const input = element("input");
  input.type = "checkbox";
  input.checked = checked;
  input.onchange = () => onChange(input.checked);
  chip.append(input, label);
  return chip;
}

function renderTeamTargets() {
  const enabled = agentsInProject.filter((agent) => agent.enabled);
  const list = byId("team-targets");
  const focused = document.activeElement.closest(".choice-chip");
  const pick = (id, label, checked, change) => {
    const chip = choiceChip(label, checked, () => {
      change();
      renderTeamTargets();
    });
    chip.dataset.target = id;
    return chip;
  };
  const eligible = new Set(enabled.map((agent) => agent.id));
  for (const id of teamTargets) if (!eligible.has(id)) teamTargets.delete(id);
  list.replaceChildren(pick("all", "Все", !teamTargets.size, () => teamTargets.clear()),
    ...enabled.map(({id, name}) => pick(id, name, teamTargets.has(id), () => {
      if (teamTargets.has(id)) teamTargets.delete(id);
      else teamTargets.add(id);
    })));
  if (focused && !focused.isConnected) {
    [...list.children].find((chip) => chip.dataset.target === focused.dataset.target)?.firstChild.focus();
  }
}

function setAttachment(file) {
  attachment = file;
  byId("chat-attachment").classList.toggle("hidden", !file);
  byId("attachment-name").textContent = file?.name || "";
}

function syncComposer() {
  const team = view === "team";
  const available = project && !["draft", "council", "deleted"].includes(project.status)
    && (team ? agentsInProject.some((agent) => agent.enabled) : selectedAgent);
  const input = byId("message-input");
  input.disabled = byId("chat-attach").disabled = !available || messageBusy;
  byId("send-button").disabled = input.disabled || Boolean(recording);
  byId("chat-record").disabled = !recording && (input.disabled || microphonePending);
  byId("plan-feedback").disabled = input.disabled || Boolean(recording);
  input.placeholder = team ? "Напишите выбранным агентам…" : "Напишите агенту…";
  input.setAttribute("aria-label", team ? "Сообщение команде" : "Сообщение агенту");
}

function selectView(next) {
  if (view !== next) {
    byId("message-input").value = "";
    discardRecording();
    setAttachment();
  }
  view = next;
  all("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  for (const channel of Object.keys(streams)) byId(channel).classList.toggle("hidden", channel !== view);
  byId("terminal-controls").classList.toggle("hidden", view !== "terminal");
  byId("message-form").classList.toggle("hidden", view === "terminal");
  byId("direct-controls").classList.toggle("hidden", view !== "direct");
  byId("team-controls").classList.toggle("hidden", view !== "team");
  syncComposer();
  refreshStream(true);
}
all("[data-view]").forEach((button) => button.onclick = () => selectView(button.dataset.view));
selectView(view);

const resizer = byId("work-resizer"), output = byId("work-output");
const heightKey = "loomis-output-height";
function resizeOutput(height, save = false) {
  const size = Math.max(140, Math.min(2400, Math.round(height)));
  output.style.height = size + "px";
  resizer.setAttribute("aria-valuenow", String(size));
  if (save) localStorage.setItem(heightKey, String(size));
}
resizeOutput(Number(localStorage.getItem(heightKey)) || 320);
resizer.setAttribute("aria-valuemin", "140");
resizer.setAttribute("aria-valuemax", "2400");
let drag;
resizer.onpointerdown = (event) => {
  if (event.button !== 0 || drag) return;
  drag = {id: event.pointerId, y: event.clientY, height: output.offsetHeight};
  resizer.setPointerCapture(drag.id);
};
resizer.onpointermove = (event) => {
  if (drag?.id === event.pointerId) resizeOutput(drag.height + event.clientY - drag.y);
};
resizer.onlostpointercapture = (event) => {
  if (drag?.id !== event.pointerId) return;
  drag = undefined;
  resizeOutput(output.offsetHeight, true);
};
resizer.onkeydown = (event) => {
  if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
  event.preventDefault();
  resizeOutput(output.offsetHeight + (event.key === "ArrowDown" ? 24 : -24), true);
};

function setStatus(status) {
  const enteringDraft = status === "draft" && byId("spec-panel").classList.contains("hidden");
  if (project) project.status = status;
  byId("project-status").textContent = ({
    draft: "Черновик", planning: "Планирование", council: "Консилиум",
    awaiting_approval: "План на утверждении",
    running: "В работе", paused: "Пауза", completed: "Завершён", deleted: "Удалён",
  })[status] || status;
  const pause = document.querySelector('[data-action="pause"]');
  pause.querySelector("strong").textContent = status === "paused" ? "Продолжить" : "Пауза";
  renderProjectControls();
  if (enteringDraft) loadSpecItems(project.id, selection).catch((error) => notify(error.message));
}

function renderProjectControls() {
  const status = project?.status;
  const available = {
    draft: ["build_spec"], planning: ["council"], awaiting_approval: ["approve"],
    running: ["pause", "rollback"], paused: ["pause", "rollback"], completed: ["rollback"],
  }[status] || [];
  byId("spec-panel").classList.toggle("hidden", status !== "draft");
  byId("project-controls").classList.toggle("hidden", !available.length || status === "draft");
  all("[data-action]").forEach((button) => {
    const action = button.dataset.action;
    if (action === "regenerate_plan") return;
    const visible = available.includes(action);
    button.classList.toggle("hidden", !visible);
    button.disabled = !visible || actionBusy ||
      (action === "build_spec" && (specBusy || !byId("spec-items").childElementCount));
  });
  const rebuild = document.querySelector('[data-action="regenerate_plan"]');
  rebuild.disabled = !project || actionBusy || ["draft", "council", "deleted"].includes(status)
    || !agentsInProject.some((agent) => agent.enabled);
  syncComposer();
  byId("spec-add").disabled = byId("spec-attach").disabled = byId("spec-image-button").disabled = specBusy || actionBusy;
}

function appendSpecItem({title, content}) {
  const item = document.createElement("li");
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  const text = document.createElement("p");
  summary.textContent = title === "Текст" ? content.slice(0, 100) : title;
  text.textContent = content;
  details.append(summary, text);
  item.append(details);
  byId("spec-items").append(item);
}

async function loadSpecItems(projectId, current) {
  specBusy = true;
  renderProjectControls();
  try {
    const items = await api("projects/" + projectId + "/spec-items");
    if (current !== selection) return;
    byId("spec-items").replaceChildren();
    items.forEach(appendSpecItem);
    byId("spec-empty").classList.toggle("hidden", items.length > 0);
  } finally {
    if (current === selection) {
      specBusy = false;
      renderProjectControls();
    }
  }
}

async function addSpecItem(body) {
  if (project?.status !== "draft" || specBusy || actionBusy) return false;
  const current = selection;
  specBusy = true;
  renderProjectControls();
  try {
    const item = await api("projects/" + project.id + "/spec-items", body);
    if (current !== selection) return false;
    appendSpecItem(item);
    byId("spec-empty").classList.add("hidden");
    return true;
  } catch (error) {
    notify(error.message);
    return false;
  } finally {
    if (current === selection) {
      specBusy = false;
      renderProjectControls();
    }
  }
}

byId("spec-form").onsubmit = async (event) => {
  event.preventDefault();
  const input = byId("spec-text");
  const text = input.value.trim();
  if (text && await addSpecItem({text}) && input.value.trim() === text) input.value = "";
};
byId("spec-attach").onclick = () => byId("spec-file").click();
byId("spec-file").onchange = ({target}) => {
  const file = target.files[0];
  target.value = "";
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  addSpecItem(form);
};

function mediaForm(file, channel, caption = "", targets = []) {
  const data = new FormData();
  data.append("file", file);
  data.append("channel", channel);
  data.append("caption", caption);
  for (const id of targets) data.append("target_agent_ids", id);
  return data;
}

byId("spec-image-button").onclick = () => byId("spec-image").click();
byId("spec-image").onchange = async ({target}) => {
  const file = target.files[0];
  target.value = "";
  if (!file || project?.status !== "draft" || specBusy || actionBusy) return;
  const current = selection;
  const {id} = project;
  specBusy = true;
  renderProjectControls();
  try {
    await api("projects/" + id + "/media", mediaForm(file, "spec"));
    if (current === selection) await loadSpecItems(id, current);
  } catch (error) {
    notify(error.message);
  } finally {
    if (current === selection) {
      specBusy = false;
      renderProjectControls();
    }
  }
};

function showCommandApproval({id, command, kind}, projectId) {
  const panel = document.createElement("section");
  const heading = document.createElement("h3");
  const text = document.createElement("p");
  const buttons = document.createElement("div");
  panel.className = "command-approval glass-panel stack";
  buttons.className = "spec-buttons";
  heading.textContent = ({failover: "Подтвердите передачу задачи", step: "Подтвердите шаг"})[kind]
    || "Подтвердите команду";
  text.textContent = command;
  for (const [approved, label] of [[true, "Разрешить"], [false, "Отклонить"]]) {
    const button = document.createElement("button");
    button.className = "button secondary";
    button.textContent = label;
    button.onclick = async () => {
      for (const option of buttons.children) option.disabled = true;
      try {
        await api("projects/" + projectId + "/approvals/" + id, {approved});
        panel.remove();
      } catch (error) {
        notify(error.message);
        if (error.status === 409) panel.remove();
        for (const option of buttons.children) option.disabled = false;
      }
    };
    buttons.append(button);
  }
  panel.append(heading, text, buttons);
  byId("command-approvals").append(panel);
}

function connect(projectId, current) {
  if (current !== selection) return;
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(scheme + "//" + location.host + "/ws/" + projectId, ["loomis", token]);
  socket.onmessage = ({data}) => {
    if (current !== selection) return;
    const event = JSON.parse(data);
    if (event.type === "status") {
      statusRevision++;
      setStatus(event.status);
      if (event.status === "deleted") loadProjects().catch((error) => notify(error.message));
    }
    if (event.type === "team_chat") receiveMessage(event, "team");
    if (event.type === "direct" || event.type === "terminal") receiveMessage(event, event.type);
    if (event.type === "traffic_state") {
      traffic.set(event.agent_id, event);
      updateLight(event.agent_id);
    }
    if (event.type === "approval") showCommandApproval(event, projectId);
  };
  socket.onclose = () => {
    if (current !== selection) return;
    setTimeout(() => connect(projectId, current), 3500);
  };
}

async function selectProject(item) {
  const current = ++selection;
  discardRecording();
  setAttachment();
  teamTargets.clear();
  deletedAgents.clear();
  project = item;
  specBusy = actionBusy = false;
  byId("spec-panel").classList.add("hidden");
  byId("spec-form").reset();
  byId("spec-items").replaceChildren();
  byId("spec-empty").classList.remove("hidden");
  byId("command-approvals").replaceChildren();
  socket?.close();
  Object.values(streams).forEach((stream) => stream.clear());
  Object.keys(streams).forEach((channel) => byId(channel).replaceChildren());
  traffic.clear();
  lights.clear();
  agentsInProject = [];
  terminalAgent = undefined;
  renderTerminalTabs();
  byId("message-input").value = "";
  byId("agent-grid").replaceChildren();
  selectedAgent = undefined;
  messageBusy = false;
  renderAgentSelector([]);
  renderTeamTargets();
  byId("current-project-name").textContent = item?.name || "Выберите проект";
  renderProjectModes();
  setStatus(item?.status || "—");
  all(".project-button").forEach((button) => button.setAttribute("aria-current", button.dataset.id === String(item?.id)));
  byId("stats-button").disabled = byId("add-agent-button").disabled = !item;
  if (!item) return;
  connect(item.id, current);
  await loadAgents(item.id, current);
}

async function loadAgents(projectId, current) {
  const agents = await api("projects/" + projectId + "/agents");
  if (current !== selection) return;
  const ids = new Set(agents.map((agent) => agent.id));
  for (const agent of agentsInProject) if (!ids.has(agent.id)) removeAgentHistory(agent);
  agentsInProject = agents;
  lights.clear();
  const cards = agents.map((agent) => {
    const card = document.createElement("article");
    const name = document.createElement("strong");
    const details = document.createElement("small");
    card.className = "agent-card glass-card";
    card.tabIndex = 0;
    card.oncontextmenu = (event) => {
      openContextMenu(event, {
        kind: "agent", item: agent, path: `projects/${projectId}/agents/${agent.id}`, projectId, current,
      });
    };
    name.textContent = agent.name;
    details.textContent = [agent.provider, agent.model].filter(Boolean).join(" · ");
    const description = element("div");
    const light = element("button", "traffic-light");
    light.type = "button";
    light.onclick = () => {
      if (!agent.enabled) return;
      selectView("direct");
      selectAgent(agent);
      renderAgentSelector(agentsInProject);
      byId("message-input").focus();
    };
    description.append(name, details);
    card.append(description, light);
    lights.set(agent.id, {light, agent});
    updateLight(agent.id);
    return card;
  });
  byId("agent-grid").replaceChildren(...(cards.length ? cards : ["В проекте пока нет агентов."]));
  renderAgentSelector(agents);
  renderTeamTargets();
  renderTerminalTabs();
  renderProjectControls();
}

function updateLight(id) {
  const card = lights.get(id);
  if (!card) return;
  const {light, agent} = card;
  const state = traffic.get(id) || agent;
  const status = agent.enabled ? state.traffic_state : "error";
  const labels = {working: "Работает", asking: "Ждёт ответа в личном чате", waiting: "Ждёт своей очереди",
    error: state.last_error || "Агент недоступен"};
  light.dataset.state = status;
  light.title = labels[status] || "Состояние неизвестно";
  light.setAttribute("aria-label", agent.name + " · " + light.title);
  light.innerHTML = status === "asking"
    ? icon('<path d="M9 9a3 3 0 1 1 4 2.83c-.67.24-1 .86-1 1.67M12 17h.01"/>')
    : "";
}

const projectModes = [
  ["autonomy-mode", "autonomy_mode", "autonomous"],
  ["failover-mode", "failover_mode", "auto"],
  ["verbosity-mode", "verbosity_level", "concise"],
];
function renderProjectModes() {
  for (const [id, field, fallback] of projectModes) {
    byId(id).value = project?.[field] || fallback;
    byId(id).disabled = !project;
  }
}

for (const [id, field] of projectModes) {
  const control = byId(id);
  control.onchange = async () => {
    if (!project) return;
    const current = selection;
    control.disabled = true;
    try {
      const result = await api("projects/" + project.id, {[field]: control.value}, token, "PATCH");
      if (current === selection) project[field] = result[field];
    } catch (error) {
      notify(error.message);
    } finally {
      if (current === selection) {
        control.value = project[field];
        control.disabled = false;
      }
    }
  };
}

function setAgentMenu(open, restoreFocus = false) {
  agentMenu.hidden = !open;
  agentSelector.setAttribute("aria-expanded", String(open));
  if (open) agentMenu.querySelector('[aria-pressed="true"]')?.focus();
  if (restoreFocus) agentSelector.focus();
}

function selectAgent(agent) {
  if (selectedAgent?.id !== agent?.id && view === "direct") {
    byId("message-input").value = "";
    discardRecording();
    setAttachment();
  }
  selectedAgent = agent;
  if (agent) byId("agent-selector-label").textContent = agent.name;
  syncComposer();
  if (view === "direct") refreshStream(true);
}

function renderAgentSelector(agents) {
  const enabled = agents.filter((agent) => agent.enabled);
  const next = enabled.find((agent) => agent.id === selectedAgent?.id) || enabled[0];
  setAgentMenu(false);
  agentSelector.disabled = !next;
  agentSelector.title = next ? "" : agents.length ? "Нет активных агентов" : "Сначала подключите хотя бы одного ИИ-агента";
  selectAgent(next);
  if (!next) byId("agent-selector-label").textContent = agents.length ? "Нет активных агентов" : "Нет агентов";
  agentMenu.replaceChildren(...enabled.map((agent) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = agent.name;
    button.setAttribute("aria-pressed", String(agent === next));
    button.onclick = () => {
      agentMenu.querySelector('[aria-pressed="true"]')?.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-pressed", "true");
      selectAgent(agent);
      setAgentMenu(false, true);
    };
    return button;
  }));
}

agentSelector.onclick = () => {
  if (!agentSelector.disabled) setAgentMenu(agentMenu.hidden);
};
document.addEventListener("focusin", (event) => {
  if (!agentSelector.parentElement.contains(event.target)) setAgentMenu(false);
});

async function loadProjects(preferredId = project?.id, preserveSelection = false) {
  const session = token;
  const projects = await api("projects");
  if (!token || token !== session) return;
  const buttons = projects.map((item) => {
    const button = document.createElement("button");
    button.className = "project-button";
    button.dataset.id = item.id;
    button.textContent = item.name;
    button.oncontextmenu = (event) => {
      openContextMenu(event, {kind: "project", item, path: "projects/" + item.id});
    };
    button.onclick = () => {
      document.querySelector(".sidebar").classList.remove("open");
      selectProject(item).catch((error) => notify(error.message));
    };
    return button;
  });
  byId("project-list").replaceChildren(...(buttons.length ? buttons : ["Нет проектов"]));
  const active = preserveSelection && projects.find((item) => item.id === project?.id);
  if (active) {
    project.name = active.name;
    byId("current-project-name").textContent = active.name;
    buttons[projects.indexOf(active)].setAttribute("aria-current", "true");
    return;
  }
  await selectProject(projects.find((item) => item.id === preferredId) || projects[0]);
}

async function runProjectAction(button) {
  if (!project || button.disabled) return;
  const current = selection;
  const revision = statusRevision;
  const path = "projects/" + project.id + "/actions";
  let action = button.dataset.action;
  if (action === "pause" && project.status === "paused") action = "resume";
  actionBusy = true;
  renderProjectControls();
  try {
    if (action === "rollback" && project.status === "running") {
      const paused = await api(path, {action: "pause"});
      if (current === selection && revision === statusRevision) setStatus(paused.status);
    }
    const result = await api(path, {action});
    if (current === selection && revision === statusRevision) setStatus(result.status);
  } catch (error) {
    notify(error.message);
  } finally {
    if (current === selection) {
      actionBusy = false;
      renderProjectControls();
    }
  }
}

const rollbackButton = document.querySelector('[data-action="rollback"]');
all("[data-action]").forEach((button) => button.onclick = () => {
  if (button !== rollbackButton) {
    runProjectAction(button);
    return;
  }
  const current = selection;
  byId("rollback-pause-note").classList.toggle("hidden", project.status !== "running");
  showModal("rollback-modal");
  byId("confirm-rollback").onclick = () => {
    showModal();
    if (current === selection) runProjectAction(button);
  };
});

byId("message-input").onkeydown = (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!byId("send-button").disabled) byId("message-form").requestSubmit();
  }
};
byId("chat-attach").onclick = () => byId("chat-file").click();
byId("chat-file").onchange = ({target}) => {
  const file = target.files[0];
  target.value = "";
  if (file && !byId("chat-attach").disabled) setAttachment(file);
};
byId("remove-attachment").onclick = () => setAttachment();

function discardRecording() {
  microphonePending = undefined;
  if (!recording) return;
  const {recorder} = recording;
  recording = undefined;
  if (recorder.state !== "inactive") recorder.stop();
  recorder.stream.getTracks().forEach((track) => track.stop());
  byId("chat-record").setAttribute("aria-pressed", "false");
  byId("chat-record").title = "Записать голосовое сообщение";
}

byId("chat-record").onclick = async () => {
  if (recording) {
    if (recording.recorder.state !== "inactive") recording.recorder.stop();
    return;
  }
  const request = Symbol();
  microphonePending = request;
  syncComposer();
  let stream;
  try {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      throw new Error("Запись с микрофона недоступна в этом окне.");
    }
    const formats = [["audio/webm;codecs=opus", "webm"], ["audio/ogg;codecs=opus", "ogg"], ["audio/mp4", "m4a"]];
    const format = formats.find(([mime]) => MediaRecorder.isTypeSupported(mime));
    if (!format) throw new Error("Браузер не поддерживает форматы аудиозаписи Loomis.");
    stream = await navigator.mediaDevices.getUserMedia({audio: true});
    if (microphonePending !== request || byId("message-input").disabled) return;
    const [mimeType, extension] = format;
    const recorder = new MediaRecorder(stream, {mimeType});
    const parts = [];
    let bytes = 0;
    recorder.ondataavailable = ({data}) => {
      if (data.size) parts.push(data);
      bytes += data.size;
      if (bytes >= 4 * 1024 * 1024 && recorder.state === "recording") recorder.stop();
    };
    recorder.onstop = () => {
      stream.getTracks().forEach((track) => track.stop());
      if (recording?.recorder !== recorder) return;
      discardRecording();
      if (bytes) setAttachment(new File(parts, "Голосовое сообщение." + extension, {type: mimeType}));
      syncComposer();
    };
    recorder.onerror = () => {
      if (recording?.recorder !== recorder) return;
      discardRecording();
      syncComposer();
      notify("Не удалось записать голосовое сообщение.");
    };
    recorder.start(1000);
    recording = {recorder};
    byId("chat-record").setAttribute("aria-pressed", "true");
    byId("chat-record").title = "Остановить запись";
  } catch (error) {
    if (microphonePending === request) notify(error.message);
  } finally {
    if (!recording || recording.recorder.stream !== stream) stream?.getTracks().forEach((track) => track.stop());
    if (microphonePending === request) microphonePending = undefined;
    syncComposer();
  }
};

async function sendMessage(feedback = false) {
  const input = byId("message-input");
  const agentId = selectedAgent?.id;
  const mode = view;
  const message = input.value.trim();
  const file = attachment;
  if (!project || input.disabled || recording || mode === "terminal") return;
  if (feedback && (!message || file)) {
    notify("Для доработки плана введите текст без вложения.");
    return;
  }
  if (!message && !file) return;
  const current = selection;
  const path = "projects/" + project.id;
  const revision = statusRevision;
  const targets = mode === "team" ? [...teamTargets] : [agentId];
  const targetIds = targets.length ? targets : null;
  messageBusy = true;
  syncComposer();
  try {
    if (feedback) {
      const result = await api(path + "/council/feedback", {feedback: message, target_agent_ids: targetIds});
      if (current === selection && revision === statusRevision) setStatus(result.status);
    } else if (file) {
      await api(path + "/media", mediaForm(file, mode === "team" ? "team_chat" : "direct", message, targets));
    } else if (mode === "team") {
      await api(path + "/chat", {question: message, target_agent_ids: targetIds});
    } else {
      await api(path + "/agents/" + agentId + "/question", {question: message});
    }
    if (current === selection && mode === view && (mode === "team" || selectedAgent?.id === agentId)) {
      if (input.value.trim() === message) input.value = "";
      if (attachment === file) setAttachment();
    }
  } catch (error) {
    notify(error.message);
  } finally {
    if (current === selection) {
      messageBusy = false;
      syncComposer();
    }
  }
}

byId("message-form").onsubmit = (event) => {
  event.preventDefault();
  sendMessage();
};
byId("plan-feedback").onclick = () => sendMessage(true);

const contextMenu = byId("context-menu");
let contextTarget, contextTrigger, modalTrigger;
let modalRevision = 0;

function openContextMenu(event, target) {
  event.preventDefault();
  contextTrigger = event.currentTarget;
  contextTarget = target;
  const edit = byId("context-edit");
  edit.innerHTML = icon('<path d="m14 5 5 5M4 20l1-5L16 4a2 2 0 0 1 3 0l1 1a2 2 0 0 1 0 3L9 19l-5 1Z"/>');
  edit.append(target.kind === "project" ? "Переименовать" : "Редактировать");
  const margin = 8;
  const anchor = contextTrigger.getBoundingClientRect();
  const x = event.clientX || (event.clientY ? 0 : anchor.left);
  const y = event.clientY || (event.clientX ? 0 : anchor.bottom);
  contextMenu.style.left = Math.max(margin, Math.min(x, innerWidth - contextMenu.offsetWidth - margin)) + "px";
  contextMenu.style.top = Math.max(margin, Math.min(y, innerHeight - contextMenu.offsetHeight - margin)) + "px";
  contextMenu.inert = false;
  byId("context-edit").focus({preventScroll: true});
}

function closeContextMenu(restoreFocus = false) {
  if (contextMenu.inert) return;
  contextMenu.inert = true;
  if (restoreFocus) contextTrigger?.focus({preventScroll: true});
  contextTarget = contextTrigger = null;
}

document.addEventListener("click", (event) => {
  if (!contextMenu.contains(event.target)) closeContextMenu();
  if (!agentSelector.parentElement.contains(event.target)) setAgentMenu(false);
});
window.addEventListener("resize", () => closeContextMenu());
contextMenu.addEventListener("keydown", (event) => {
  if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const first = byId("context-edit");
  const last = byId("context-delete");
  (event.key === "Home" || (event.key !== "End" && document.activeElement === last) ? first : last).focus();
});

byId("context-edit").onclick = () => {
  const target = contextTarget;
  closeContextMenu(true);
  if (target.kind === "agent") {
    openAgentModal(target);
  } else {
    byId("rename-name").value = target.item.name;
    showModal("rename-modal");
    byId("rename-form").onsubmit = (event) => {
      event.preventDefault();
      const name = byId("rename-name").value.trim();
      if (!name) {
        notify("Введите название проекта.");
        return;
      }
      mutateFromModal(event.submitter, target, "PATCH", {name});
    };
  }
};

byId("context-delete").onclick = () => {
  const target = contextTarget;
  closeContextMenu(true);
  const kind = target.kind === "project" ? "проект" : "агента";
  byId("delete-title").textContent = `Удалить ${kind} «${target.item.name}»?`;
  showModal("delete-modal");
  byId("confirm-delete").onclick = ({currentTarget}) => mutateFromModal(currentTarget, target, "DELETE");
};

function removeAgentHistory({id, name}) {
  deletedAgents.add(id);
  teamTargets.delete(id);
  streams.direct.delete(id);
  streams.terminal.delete(id);
  const messages = streams.team.get(0) || [];
  streams.team.set(0, messages.filter(({event}) => event.agent_id !== id
    && (event.agent_id != null || (event.sender_name !== name && event.target_name !== "@" + name))));
  byId("direct").replaceChildren();
  byId("terminal").replaceChildren();
  refreshStream(true);
}

async function mutateFromModal(button, target, method, body, onError = (error) => notify(error.message)) {
  const revision = modalRevision;
  const session = token;
  button.disabled = true;
  try {
    await api(target.path, body, session, method);
    if (revision === modalRevision) showModal();
    if (session !== token) return;
    try {
      if (target.kind === "project") await loadProjects(project?.id, true);
      else if (target.current === selection) {
        if (method === "DELETE") {
          removeAgentHistory(target.item);
          agentsInProject = agentsInProject.filter((agent) => agent.id !== target.item.id);
          renderAgentSelector(agentsInProject);
          renderTeamTargets();
          renderTerminalTabs();
        }
        await loadAgents(target.projectId, target.current);
      }
    } catch (error) {
      notify(error.message);
    }
  } catch (error) {
    if (revision === modalRevision) onError(error);
    else notify(error.message);
  } finally {
    button.disabled = false;
  }
}

function showModal(id) {
  const backdrop = byId("modal-backdrop");
  if (!id && (backdrop.inert || !byId("agent-test-status").classList.contains("hidden"))) return;
  modalRevision++;
  if (id && backdrop.inert) modalTrigger = document.activeElement;
  backdrop.inert = !id;
  backdrop.classList.toggle("hidden", !id);
  all(".modal").forEach((modal) => {
    modal.inert = modal.id !== id;
    modal.classList.toggle("hidden", modal.inert);
  });
  byId("app-screen").inert = Boolean(id);
  if (id) {
    const modal = byId(id);
    const field = [...modal.querySelectorAll("input:enabled, select:enabled")]
      .find((input) => input.getClientRects().length && !input.closest("[inert]"));
    (field || modal.querySelector(".close-modal")).focus();
  } else {
    modalTrigger?.focus();
    modalTrigger = null;
  }
}

byId("add-agent-button").onclick = () => openAgentModal();
const cliProviders = new Set(["codex", "claude", "opencode", "qwen", "cli"]);
const agentForm = byId("agent-form");
const agentProvider = byId("agent-provider");
let agentTarget;

function openAgentModal(target, manual = false, preselected = []) {
  if (!project) return;
  agentTarget = target || {kind: "agent", path: "projects/" + project.id + "/agents", projectId: project.id, current: selection};
  const agent = target?.item;
  const settings = agent?.settings || {};
  agentForm.reset();
  agentProvider.querySelector("[data-custom]")?.remove();
  const provider = agent?.provider || "codex";
  if (!Array.from(agentProvider.options).some((option) => option.value === provider)) {
    const option = new Option(provider, provider);
    option.dataset.custom = "";
    agentProvider.add(option);
  }
  agentProvider.value = provider;
  byId("agent-name").value = agent?.name || "";
  byId("agent-model").value = agent?.model || "";
  byId("agent-base-url").value = settings.base_url || settings.baseUrl || "";
  byId("agent-api-key").placeholder = settings.has_api_key ? "Ключ сохранён · введите новый для замены" : "";
  byId("agent-command").value = settings.command || "";
  byId("agent-title").textContent = agent ? "Настройки агента" : "Подключить ИИ";
  byId("agent-submit").textContent = agent ? "Сохранить" : "Добавить агента";
  renderAgentFields();
  byId("agent-add-tabs").classList.toggle("hidden", Boolean(agent));
  selectAgentAddTab(Boolean(agent) || manual);
  showModal("agent-modal");
  if (!agent) loadPresetChoices("agent-presets", agentsInProject.map(({name}) => name), preselected);
}

function clearAgentErrors() {
  agentForm.querySelectorAll("[aria-invalid]").forEach((field) => field.removeAttribute("aria-invalid"));
  byId("agent-error").textContent = "";
}

function renderAgentFields() {
  const isApi = !cliProviders.has(agentProvider.value.toLowerCase());
  byId("agent-api-fields").inert = !isApi;
  byId("agent-base-url").disabled = byId("agent-api-key").disabled = !isApi;
  byId("agent-cli-fields").classList.toggle("hidden", isApi);
  byId("agent-command").disabled = isApi;
  byId("agent-model").placeholder = isApi ? "" : agentProvider.value;
  clearAgentErrors();
}
agentProvider.onchange = renderAgentFields;
agentForm.addEventListener("input", ({target}) => {
  target.removeAttribute("aria-invalid");
  if (!agentForm.querySelector("[aria-invalid]")) byId("agent-error").textContent = "";
});
all(".close-modal").forEach((button) => button.onclick = () => showModal());
byId("modal-backdrop").onclick = (event) => { if (event.target === event.currentTarget) showModal(); };
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!contextMenu.inert) closeContextMenu(true);
  else if (!agentMenu.hidden) setAgentMenu(false, true);
  else showModal();
});
byId("mobile-menu").onclick = () => document.querySelector(".sidebar").classList.toggle("open");

const agentFields = {
  name: ["agent-name", "Имя должно содержать от 2 до 100 символов."],
  model: ["agent-model", "Введите имя модели длиной до 255 символов."],
  base_url: ["agent-base-url", "Введите корректный Base URL с http:// или https://."],
  api_key: ["agent-api-key", "Введите API Key."],
  command: ["agent-command", "Введите команду запуска."],
};

function showAgentErrors(fields, message) {
  const inputs = fields.map((field) => byId(agentFields[field][0]));
  inputs.forEach((input) => input.setAttribute("aria-invalid", "true"));
  byId("agent-error").textContent = message || fields.map((field) => agentFields[field][1]).join(" ");
  inputs[0]?.focus();
}

function validBaseUrl(value) {
  if (!/^https?:\/\/[^/]/.test(value) || /[\s\\\u0000-\u001f]/.test(value)) return false;
  try {
    const url = new URL(value);
    return Boolean(url.hostname) && url.port !== "0";
  } catch {
    return false;
  }
}

agentForm.onsubmit = async (event) => {
  event.preventDefault();
  if (!agentTarget || agentTarget.current !== selection || byId("agent-submit").disabled) return;
  clearAgentErrors();
  const name = byId("agent-name").value.trim();
  const provider = agentProvider.value;
  const isCli = cliProviders.has(provider.toLowerCase());
  const model = byId("agent-model").value.trim() || (isCli ? provider : "");
  const settings = {
  };
  const errors = [];
  if ([...name].length < 2 || [...name].length > 100) errors.push("name");
  if (!model || [...model].length > 255) errors.push("model");
  if (isCli) {
    settings.command = byId("agent-command").value.trim();
    if (!settings.command) errors.push("command");
  } else {
    const keyInput = byId("agent-api-key");
    const entered = keyInput.value.trim();
    const apiKey = entered.replace(/^(?:Bearer(?:\s+|$))+/i, "").trim();
    settings.base_url = byId("agent-base-url").value.trim();
    if (!validBaseUrl(settings.base_url)) errors.push("base_url");
    if (apiKey) keyInput.value = settings.api_key = apiKey;
    else if (entered || !agentTarget.item?.settings?.has_api_key) errors.push("api_key");
  }
  if (errors.length) {
    showAgentErrors(errors);
    return;
  }
  const target = agentTarget;
  const revision = modalRevision;
  const button = byId("agent-submit");
  const status = byId("agent-test-status");
  const candidate = {name, provider, model, settings};
  const showError = (error) => {
    const fields = Array.isArray(error.detail)
      ? [...new Set(error.detail.map((item) => item.loc?.at(-1)).filter((field) => Object.hasOwn(agentFields, field)))]
      : [];
    showAgentErrors(fields, error.message);
  };
  const fields = [...agentForm.querySelectorAll("input:enabled")];
  fields.forEach((field) => { field.readOnly = true; });
  byId("agent-hub-tab").disabled = byId("agent-manual-tab").disabled = true;
  agentProvider.disabled = button.disabled = true;
  status.classList.remove("hidden");
  let failure;
  try {
    await api("projects/" + target.projectId + "/agents/test", {
      ...candidate, ...(target.item && {agent_id: target.item.id}),
    });
  } catch (error) {
    failure = error;
  } finally {
    status.classList.add("hidden");
    fields.forEach((field) => { field.readOnly = false; });
    byId("agent-hub-tab").disabled = byId("agent-manual-tab").disabled = false;
    agentProvider.disabled = button.disabled = false;
  }
  if (revision !== modalRevision || target.current !== selection) return;
  if (failure) showError(failure);
  else await mutateFromModal(button, target, target.item ? "PATCH" : "POST", candidate, showError);
};

function renderTelegramLogs() {
  byId("telegram-logs").checked = telegramLogsEnabled;
  byId("telegram-logs-state").textContent = telegramLogsEnabled ? "Вкл" : "Выкл";
}
byId("settings-button").onclick = () => {
  renderTelegramLogs();
  showModal("settings-modal");
};
byId("telegram-logs").onchange = async ({target}) => {
  const session = token;
  target.disabled = true;
  byId("telegram-logs-state").textContent = target.checked ? "Вкл" : "Выкл";
  try {
    const user = await api("auth/me", {tg_logs_enabled: target.checked}, session, "PATCH");
    if (session === token) telegramLogsEnabled = user.tg_logs_enabled;
  } catch (error) {
    if (session === token) notify(error.message);
  } finally {
    target.disabled = false;
    renderTelegramLogs();
  }
};

function selectTab(prefix, manual) {
  for (const [kind, active] of [["hub", !manual], ["manual", manual]]) {
    const tab = byId(prefix + "-" + kind + "-tab");
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-pressed", String(active));
  }
}
function selectAgentAddTab(manual) {
  selectTab("agent", manual);
  byId("agent-import-form").classList.toggle("hidden", manual);
  agentForm.classList.toggle("hidden", !manual);
}
function selectProjectAddTab(manual) {
  selectTab("project", manual);
  byId("project-presets").classList.toggle("hidden", manual);
}
byId("agent-hub-tab").onclick = () => selectAgentAddTab(false);
byId("agent-manual-tab").onclick = () => selectAgentAddTab(true);
byId("project-hub-tab").onclick = () => selectProjectAddTab(false);
byId("project-manual-tab").onclick = () => selectProjectAddTab(true);

function checkedPresets(id) {
  return [...byId(id).querySelectorAll("input:checked")].map((input) => Number(input.value));
}

async function loadPresetChoices(id, existingNames = [], preselected = []) {
  const list = byId(id);
  const revision = modalRevision;
  const importing = id === "agent-presets";
  const sync = () => {
    if (importing) byId("import-submit").disabled = !checkedPresets(id).length;
  };
  list.textContent = "Загрузка моделей…";
  sync();
  try {
    const presets = await api("hub/models");
    if (revision !== modalRevision) return;
    const available = presets.filter(({name}) => !existingNames.includes(name));
    list.replaceChildren(...available.map((preset) => {
      const label = [preset.name, preset.config_json.model].join(" · ");
      const chip = choiceChip(label, preselected.includes(preset.id), sync);
      chip.firstChild.value = preset.id;
      return chip;
    }));
    if (!available.length) list.textContent = presets.length ? "Все модели уже подключены." : "В хабе пока нет моделей.";
    sync();
  } catch (error) {
    if (revision === modalRevision) list.textContent = error.message;
  }
}
byId("new-project-button").onclick = () => {
  selectProjectAddTab(false);
  showModal("project-modal");
  loadPresetChoices("project-presets");
};
byId("project-form").onsubmit = async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const manual = byId("project-manual-tab").getAttribute("aria-pressed") === "true";
  const presetIds = manual ? [] : checkedPresets("project-presets");
  const revision = modalRevision;
  const session = token;
  const form = event.currentTarget;
  button.disabled = true;
  try {
    const created = await api("projects", {name: byId("project-name").value, description: byId("project-description").value}, session);
    let importError;
    if (presetIds.length) {
      try {
        await api("projects/" + created.id + "/agents/from-hub", {preset_ids: presetIds}, session);
      } catch (error) {
        importError = error;
      }
    }
    if (session !== token) return;
    const modalCurrent = revision === modalRevision;
    if (modalCurrent) {
      form.reset();
      showModal();
    }
    await loadProjects(created.id);
    if (importError) notify("Проект создан. " + importError.message);
    if (modalCurrent && byId("modal-backdrop").inert && project?.id === created.id && (manual || importError)) {
      openAgentModal(undefined, manual, importError ? presetIds : []);
    }
  } catch (error) {
    if (session === token) notify(error.message);
  } finally {
    button.disabled = false;
  }
};

byId("agent-import-form").onsubmit = async (event) => {
  event.preventDefault();
  const ids = checkedPresets("agent-presets");
  if (!ids.length || agentTarget?.current !== selection) return;
  const button = event.submitter;
  const target = agentTarget;
  const revision = modalRevision;
  const session = token;
  const choices = [...byId("agent-presets").querySelectorAll("input")];
  button.disabled = true;
  choices.forEach((input) => { input.disabled = true; });
  try {
    await api(target.path + "/from-hub", {preset_ids: ids}, session);
    if (revision === modalRevision) showModal();
    if (target.current === selection && session === token) await loadAgents(target.projectId, target.current);
  } catch (error) {
    if (session === token) notify(error.message);
  } finally {
    if (revision === modalRevision) {
      choices.forEach((input) => { input.disabled = false; });
      button.disabled = !checkedPresets("agent-presets").length;
    }
  }
};

const hubForm = byId("hub-form");
const hubProvider = byId("hub-provider");
hubProvider.replaceChildren(...[...agentProvider.options].map((option) => option.cloneNode(true)));
hubProvider.value = "api";

function renderHubFields() {
  const isApi = !cliProviders.has(hubProvider.value);
  byId("hub-api-fields").classList.toggle("hidden", !isApi);
  byId("hub-cli-field").classList.toggle("hidden", isApi);
  const fields = hubForm.elements;
  fields.namedItem("base_url").disabled = fields.namedItem("api_key").disabled = !isApi;
  fields.namedItem("base_url").required = fields.namedItem("api_key").required = isApi;
  fields.namedItem("command").disabled = isApi;
  fields.namedItem("command").required = !isApi && hubProvider.value !== "codex";
}
hubProvider.onchange = renderHubFields;

async function renderHubModels(revision = modalRevision) {
  const list = byId("hub-models");
  list.textContent = "Загрузка моделей…";
  try {
    const presets = await api("hub/models");
    if (revision !== modalRevision) return;
    list.replaceChildren(...presets.map((preset) => {
      const row = element("li", "hub-model");
      const copy = element("div");
      const details = [preset.adapter_type, preset.config_json.model, preset.config_json.base_url].filter(Boolean);
      copy.append(element("strong", "", preset.name), element("small", "", details.join(" · ")));
      const remove = element("button", "icon-button danger");
      remove.type = "button";
      remove.title = "Удалить модель";
      remove.setAttribute("aria-label", "Удалить модель " + preset.name);
      remove.innerHTML = icon('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7l1-3h4l1 3"/>');
      remove.onclick = async () => {
        remove.disabled = true;
        try {
          await api("hub/models/" + preset.id, undefined, token, "DELETE");
          if (revision === modalRevision) await renderHubModels(revision);
        } catch (error) {
          if (revision === modalRevision) notify(error.message);
          remove.disabled = false;
        }
      };
      row.append(copy, remove);
      return row;
    }));
    if (!presets.length) list.append(element("li", "hint", "Сохранённых моделей пока нет."));
  } catch (error) {
    if (revision === modalRevision) list.textContent = error.message;
  }
}

byId("hub-button").onclick = () => {
  hubForm.reset();
  hubProvider.value = "api";
  renderHubFields();
  showModal("hub-modal");
  renderHubModels();
};
hubForm.onsubmit = async (event) => {
  event.preventDefault();
  const data = new FormData(hubForm);
  const button = event.submitter;
  const type = data.get("provider");
  const config = {model: data.get("model").trim()};
  if (cliProviders.has(type)) {
    const command = data.get("command").trim();
    if (command) config.command_template = command;
  } else {
    config.base_url = data.get("base_url").trim();
    config.api_key = data.get("api_key").trim();
  }
  const revision = modalRevision;
  button.disabled = true;
  try {
    await api("hub/models", {
      name: data.get("name").trim(),
      adapter_type: type,
      config_json: config,
    });
    if (revision === modalRevision) {
      hubForm.reset();
      hubProvider.value = type;
      renderHubFields();
      await renderHubModels(revision);
    }
  } catch (error) {
    if (revision === modalRevision) notify(error.message);
  } finally {
    button.disabled = false;
  }
};

const statsNumber = new Intl.NumberFormat("ru");
byId("stats-button").onclick = async () => {
  if (!project) return;
  showModal("stats-modal");
  const revision = modalRevision;
  const body = byId("stats-body");
  body.textContent = "Загрузка статистики…";
  try {
    const stats = await api("projects/" + project.id + "/stats");
    if (revision !== modalRevision) return;
    const total = element("p", "stats-total");
    total.append(element("strong", "", statsNumber.format(stats.total_tokens)), " токенов по проекту");
    body.replaceChildren(total);
    if (!stats.agents.length) {
      body.append(element("p", "hint", "В проекте пока нет агентов."));
      return;
    }
    const table = element("table", "stats-table");
    const header = table.createTHead().insertRow();
    for (const title of ["Нейросеть", "Токены", "Доля выполненных задач"]) {
      const column = element("th", "", title);
      column.scope = "col";
      header.append(column);
    }
    const rows = table.createTBody();
    for (const agent of stats.agents) {
      const row = rows.insertRow();
      const name = element("th", "", agent.name);
      name.scope = "row";
      row.append(name);
      row.insertCell().textContent = statsNumber.format(agent.tokens);
      const bar = element("progress");
      bar.max = 100;
      bar.value = agent.contribution_percent;
      bar.setAttribute("aria-label", "Доля выполненных задач: " + agent.name);
      row.insertCell().append(statsNumber.format(agent.contribution_percent) + " %", bar);
    }
    body.append(table);
  } catch (error) {
    if (revision === modalRevision) body.textContent = error.message;
  }
};

byId("logout-button").onclick = () => {
  token = null;
  sessionStorage.removeItem("loomis-token");
  stopMagic();
  selectProject(undefined);
  showWorkspace(false);
};

const savedToken = token;
if (savedToken) {
  signIn(savedToken, () => token === savedToken).catch((error) => {
    if (token !== savedToken) return;
    token = null;
    sessionStorage.removeItem("loomis-token");
    if (error.status !== 401) notify(error.message);
  });
}

const orbs = [...all("#ambient-bg .orb")];
const orbDepths = [75, -55, 35];
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
let orbFrame = null;
let orbX = 0;
let orbY = 0;
let targetX = 0;
let targetY = 0;

function animateOrbs() {
  orbX += (targetX - orbX) * 0.09;
  orbY += (targetY - orbY) * 0.09;
  const moving = Math.abs(targetX - orbX) + Math.abs(targetY - orbY) > 0.001;
  orbs.forEach((orb, index) => {
    const depth = orbDepths[index];
    orb.style.transform = `translate3d(${orbX * depth}px, ${orbY * depth}px, 0)`;
  });
  orbFrame = moving ? requestAnimationFrame(animateOrbs) : null;
}

function moveOrbs(x, y) {
  if (reducedMotion.matches) return;
  targetX = x;
  targetY = y;
  if (orbFrame === null) orbFrame = requestAnimationFrame(animateOrbs);
}

window.addEventListener("pointermove", (event) => {
  moveOrbs(2 * event.clientX / innerWidth - 1, 2 * event.clientY / innerHeight - 1);
}, {passive: true});
document.documentElement.addEventListener("pointerleave", () => moveOrbs(0, 0));
reducedMotion.addEventListener("change", () => {
  if (!reducedMotion.matches) return;
  if (orbFrame !== null) cancelAnimationFrame(orbFrame);
  orbFrame = null;
  targetX = targetY = orbX = orbY = 0;
  orbs.forEach((orb) => { orb.style.transform = ""; });
});
