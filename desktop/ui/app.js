const byId = (id) => document.getElementById(id);
const all = (selector) => document.querySelectorAll(selector);

let token = sessionStorage.getItem("loomis-token");
let project, socket, magicTimer;
let selection = 0;
let magicAttempt = 0;

async function api(path, body, accessToken = token) {
  const headers = {};
  if (accessToken) headers.Authorization = "Bearer " + accessToken;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch("/api/" + path, {
    headers, method: body === undefined ? "GET" : "POST", body: JSON.stringify(body),
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
  if (project) project.status = status;
  byId("project-status").textContent = ({
    draft: "Черновик", planning: "Планирование", council: "Консилиум",
    running: "В работе", paused: "Пауза", completed: "Завершён", deleted: "Удалён",
  })[status] || status;
  const pause = document.querySelector('[data-action="pause"]');
  pause.querySelector("strong").textContent = status === "paused" ? "Продолжить" : "Пауза";
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
      setStatus(event.status);
      if (event.status === "deleted") loadProjects().catch((error) => notify(error.message));
    }
    if (event.type === "log") terminalLine(event.message);
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
  all("[data-action], #add-agent-button").forEach((button) => { button.disabled = !item; });
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
  if (!project) return;
  const current = selection;
  let action = button.dataset.action;
  if (action === "pause" && project.status === "paused") action = "resume";
  button.disabled = true;
  try {
    const result = await api("projects/" + project.id + "/actions", {action});
    if (current === selection) setStatus(result.status);
  } catch (error) {
    notify(error.message);
  } finally {
    if (current === selection) button.disabled = false;
  }
}

const rollbackButton = document.querySelector('[data-action="rollback"]');
all("[data-action]").forEach((button) => button.onclick = () => {
  if (button !== rollbackButton) {
    runProjectAction(button);
    return;
  }
  const current = selection;
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
    await api("projects/" + projectId + "/agents", {
      name: byId("agent-name").value, provider: byId("agent-provider").value, model: byId("agent-model").value,
    });
    event.target.reset();
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
