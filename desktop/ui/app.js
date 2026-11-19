const byId = (id) => document.getElementById(id);
const all = (selector) => document.querySelectorAll(selector);

let token = sessionStorage.getItem("loomis-token");
let project, socket, magicTimer;
let selection = 0;
let magicAttempt = 0;
let statusRevision = 0;
let specBusy = false;
let actionBusy = false;

async function api(path, body, accessToken = token) {
  const headers = {};
  const multipart = body instanceof FormData;
  if (accessToken) headers.Authorization = "Bearer " + accessToken;
  if (body !== undefined && !multipart) headers["Content-Type"] = "application/json";
  const response = await fetch("/api/" + path, {
    headers, method: body === undefined ? "GET" : "POST", body: multipart ? body : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join(", ") : data.detail || "Ошибка запроса");
    error.status = response.status;
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
    button.textContent = theme === "dark" ? "☾" : "☀";
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

function connection(online) {
  byId("connection-chip").lastElementChild.textContent = online ? "В сети" : "Отключён";
  byId("connection-chip").firstElementChild.classList.toggle("online", online);
}

const log = byId("terminal");
const terminalCount = byId("terminal-count");
const logLimit = 500;
const pendingLogs = new Array(logLimit);
const logTime = new Intl.DateTimeFormat(undefined, {hour: "numeric", minute: "numeric", second: "numeric"});
let logFrame = null;
let logCursor = 0;
let logCount = 0;

function terminalLine(message) {
  pendingLogs[logCursor] = {message, time: Date.now()};
  logCursor = (logCursor + 1) % logLimit;
  logCount = Math.min(logCount + 1, logLimit);
  if (logFrame === null) logFrame = requestAnimationFrame(flushLogs);
}

function flushLogs() {
  logFrame = null;
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 20;
  const fragment = document.createDocumentFragment();
  let index = (logCursor - logCount + logLimit) % logLimit;
  for (let i = 0; i < logCount; i++) {
    const {message, time} = pendingLogs[index];
    const line = document.createElement("div");
    line.textContent = logTime.format(time) + "  " + message;
    fragment.append(line);
    pendingLogs[index] = undefined;
    index = (index + 1) % logLimit;
  }
  const overflow = log.childElementCount + logCount - logLimit;
  if (overflow === log.childElementCount) {
    log.replaceChildren(fragment);
  } else {
    for (let i = 0; i < overflow; i++) log.firstElementChild.remove();
    log.append(fragment);
  }
  logCount = 0;
  terminalCount.textContent = log.childElementCount + " строк";
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function clearTerminal() {
  if (logFrame !== null) cancelAnimationFrame(logFrame);
  logFrame = null;
  logCount = 0;
  logCursor = 0;
  pendingLogs.fill(undefined);
  log.replaceChildren();
  terminalCount.textContent = "0 строк";
}
byId("clear-terminal").onclick = clearTerminal;

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
    const visible = available.includes(action);
    button.classList.toggle("hidden", !visible);
    button.disabled = !visible || actionBusy ||
      (action === "build_spec" && (specBusy || !byId("spec-items").childElementCount));
  });
  byId("spec-add").disabled = byId("spec-attach").disabled = specBusy || actionBusy;
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

function showCommandApproval({id, command}, projectId) {
  const panel = document.createElement("section");
  const heading = document.createElement("h3");
  const text = document.createElement("p");
  const buttons = document.createElement("div");
  panel.className = "command-approval glass-panel stack";
  buttons.className = "spec-buttons";
  heading.textContent = "Подтвердите команду";
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
  socket.onopen = () => { if (current === selection) connection(true); };
  socket.onmessage = ({data}) => {
    if (current !== selection) return;
    const event = JSON.parse(data);
    if (event.type === "status") {
      statusRevision++;
      setStatus(event.status);
      if (event.status === "deleted") loadProjects().catch((error) => notify(error.message));
    }
    if (event.type === "log") terminalLine(event.message);
    if (event.type === "approval") showCommandApproval(event, projectId);
  };
  socket.onclose = () => {
    if (current !== selection) return;
    connection(false);
    setTimeout(() => connect(projectId, current), 3500);
  };
}

async function selectProject(item) {
  const current = ++selection;
  project = item;
  specBusy = actionBusy = false;
  byId("spec-panel").classList.add("hidden");
  byId("spec-form").reset();
  byId("spec-items").replaceChildren();
  byId("spec-empty").classList.remove("hidden");
  byId("command-approvals").replaceChildren();
  socket?.close();
  connection(false);
  clearTerminal();
  byId("agent-grid").replaceChildren();
  byId("agent-select").replaceChildren();
  byId("send-button").disabled = true;
  byId("current-project-name").textContent = item?.name || "Выберите проект";
  byId("terminal-channel").textContent = item ? "/ws/" + item.id : "/ws";
  setStatus(item?.status || "—");
  all(".project-button").forEach((button) => button.setAttribute("aria-current", button.dataset.id === String(item?.id)));
  byId("add-agent-button").disabled = !item;
  if (!item) return;
  connect(item.id, current);
  await loadAgents(item.id, current);
}

async function loadAgents(projectId, current) {
  const agents = await api("projects/" + projectId + "/agents");
  if (current !== selection) return;
  const cards = agents.map((agent) => {
    const card = document.createElement("article");
    const name = document.createElement("strong");
    const details = document.createElement("small");
    card.className = "agent-card glass-card";
    name.textContent = agent.name;
    details.textContent = [agent.provider, agent.model, agent.enabled ? "Включён" : "Выключен"].join(" · ");
    card.append(name, details);
    return card;
  });
  byId("agent-grid").replaceChildren(...(cards.length ? cards : ["В проекте пока нет агентов."]));
  const options = agents.filter((agent) => agent.enabled).map((agent) => new Option(agent.name, agent.id));
  byId("agent-select").replaceChildren(...options);
  byId("send-button").disabled = !options.length;
}

async function loadProjects(preferredId = project?.id) {
  const session = token;
  const projects = await api("projects");
  if (!token || token !== session) return;
  const buttons = projects.map((item) => {
    const button = document.createElement("button");
    button.className = "project-button";
    button.dataset.id = item.id;
    button.textContent = item.name;
    button.onclick = () => {
      document.querySelector(".sidebar").classList.remove("open");
      selectProject(item).catch((error) => notify(error.message));
    };
    return button;
  });
  byId("project-list").replaceChildren(...(buttons.length ? buttons : ["Нет проектов"]));
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
byId("message-form").onsubmit = async (event) => {
  event.preventDefault();
  const input = byId("message-input");
  const agentId = byId("agent-select").value;
  const question = input.value.trim();
  if (!project || !agentId || !question) return;
  const current = selection;
  byId("send-button").disabled = true;
  try {
    const result = await api("projects/" + project.id + "/agents/" + agentId + "/question", {question});
    if (current === selection) {
      if (input.value.trim() === question) input.value = "";
      terminalLine([result.message, result.summary, result.answer].find(Boolean) || "Агент обработал запрос.");
    }
  } catch (error) {
    notify(error.message);
  } finally {
    if (current === selection) byId("send-button").disabled = false;
  }
};

let modalTrigger;
function showModal(id) {
  const backdrop = byId("modal-backdrop");
  if (!id && backdrop.inert) return;
  if (id && backdrop.inert) modalTrigger = document.activeElement;
  backdrop.inert = !id;
  backdrop.classList.toggle("hidden", !id);
  all(".modal").forEach((modal) => {
    modal.inert = modal.id !== id;
    modal.classList.toggle("hidden", modal.inert);
  });
  byId("app-screen").inert = Boolean(id);
  if (id) {
    byId(id).querySelector("input, .modal-actions .close-modal").focus();
  } else {
    modalTrigger?.focus();
    modalTrigger = null;
  }
}
byId("new-project-button").onclick = byId("sidebar-create").onclick = () => showModal("project-modal");
byId("add-agent-button").onclick = () => showModal("agent-modal");
function renderAgentFields() {
  const isApi = byId("agent-provider").value === "api";
  byId("agent-api-fields").inert = !isApi;
  byId("agent-base-url").disabled = byId("agent-api-key").disabled = !isApi;
  byId("agent-base-url").required = isApi;
}
byId("agent-provider").onchange = renderAgentFields;
all(".close-modal").forEach((button) => button.onclick = () => showModal());
byId("modal-backdrop").onclick = (event) => { if (event.target === event.currentTarget) showModal(); };
document.addEventListener("keydown", (event) => { if (event.key === "Escape") showModal(); });
byId("mobile-menu").onclick = () => document.querySelector(".sidebar").classList.toggle("open");

byId("project-form").onsubmit = async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const created = await api("projects", {name: byId("project-name").value, description: byId("project-description").value});
    event.target.reset();
    showModal();
    await loadProjects(created.id);
  } catch (error) {
    notify(error.message);
  } finally {
    button.disabled = false;
  }
};
byId("agent-form").onsubmit = async (event) => {
  event.preventDefault();
  if (!project) return;
  const current = selection;
  const projectId = project.id;
  const button = event.submitter;
  button.disabled = true;
  try {
    const provider = byId("agent-provider").value;
    const settings = {};
    if (provider === "api") {
      settings.base_url = byId("agent-base-url").value.trim();
      settings.api_key = byId("agent-api-key").value.trim();
    }
    await api("projects/" + projectId + "/agents", {
      name: byId("agent-name").value, provider, model: byId("agent-model").value, settings,
    });
    event.target.reset();
    renderAgentFields();
    showModal();
    await loadAgents(projectId, current);
  } catch (error) {
    notify(error.message);
  } finally {
    button.disabled = false;
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
