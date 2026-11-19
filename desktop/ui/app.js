const byId = (id) => document.getElementById(id);
const all = (selector) => document.querySelectorAll(selector);

let token = sessionStorage.getItem("loomis-token");
let project, socket, magicTimer;
let selection = 0;
let magicAttempt = 0;

async function api(path, body) {
  const headers = {};
  if (token) headers.Authorization = "Bearer " + token;
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
  toast.className = "toast glass";
  toast.textContent = message;
  byId("toasts").append(toast);
  setTimeout(() => toast.remove(), 4500);
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("loomis-theme", theme);
  all(".theme-switch").forEach((button) => {
    button.textContent = theme === "dark" ? "Dark" : "Light";
    button.setAttribute("aria-pressed", theme === "dark");
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
    button.setAttribute("aria-selected", button === tab);
  });
  all(".auth-pane").forEach((pane) => pane.classList.toggle("active", pane.id === "auth-" + tab.dataset.authTab));
});

async function signIn(accessToken) {
  token = accessToken;
  try {
    const user = await api("auth/me");
    sessionStorage.setItem("loomis-token", token);
    byId("user-name").textContent = user.username || "Loomis";
    byId("user-avatar").textContent = byId("user-name").textContent[0].toUpperCase();
    byId("telegram-dot").classList.toggle("online", Boolean(user.telegram_id));
    byId("telegram-label").textContent = user.telegram_id ? "Привязан" : "Не привязан";
    byId("auth-screen").classList.add("hidden");
    byId("app-screen").classList.remove("hidden");
    try {
      await loadProjects();
    } catch (error) {
      notify(error.message);
    }
  } catch (error) {
    token = null;
    sessionStorage.removeItem("loomis-token");
    throw error;
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
          await signIn(status.access_token);
          stopMagic();
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

function terminalLine(message) {
  const log = byId("terminal");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 20;
  const line = document.createElement("div");
  line.textContent = new Date().toLocaleTimeString() + "  " + message;
  log.append(line);
  if (log.childElementCount > 500) log.firstElementChild.remove();
  byId("terminal-count").textContent = log.childElementCount + " строк";
  if (atBottom) log.scrollTop = log.scrollHeight;
}

byId("clear-terminal").onclick = () => {
  byId("terminal").replaceChildren();
  byId("terminal-count").textContent = "0 строк";
};

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
  byId("clear-terminal").click();
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
    card.className = "agent-card glass";
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
  const projects = await api("projects");
  if (!token) return;
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

all("[data-action]").forEach((button) => button.onclick = async () => {
  if (!project) return;
  const current = selection;
  let action = button.dataset.action;
  if (action === "pause" && project.status === "paused") action = "resume";
  if (action === "rollback" && !window.confirm("Откатить последний Git-шаг проекта?")) return;
  button.disabled = true;
  try {
    const result = await api("projects/" + project.id + "/actions", {action});
    if (current === selection) setStatus(result.status);
  } catch (error) {
    notify(error.message);
  } finally {
    if (current === selection) button.disabled = false;
  }
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

function showModal(id) {
  byId("modal-backdrop").classList.toggle("hidden", !id);
  all(".modal").forEach((modal) => modal.classList.toggle("hidden", modal.id !== id));
  if (id) byId(id).querySelector("input").focus();
}
byId("new-project-button").onclick = byId("sidebar-create").onclick = () => showModal("project-modal");
byId("add-agent-button").onclick = () => showModal("agent-modal");
all(".close-modal").forEach((button) => button.onclick = () => showModal());
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
  selectProject(undefined);
  byId("app-screen").classList.add("hidden");
  byId("auth-screen").classList.remove("hidden");
};

if (token) signIn(token).catch((error) => {
  if (error.status !== 401) notify(error.message);
});
