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

const log = byId("terminal");
const terminalCount = byId("terminal-count");
const logLimit = 500;
const pendingLogs = new Array(logLimit);
const logTime = new Intl.DateTimeFormat(undefined, {hour: "numeric", minute: "numeric", second: "numeric"});
let logFrame = null;
let logCursor = 0;
let logCount = 0;

function terminalLine(message) {
  const text = String(message).replace(/[0-9#*]\uFE0F?\u20E3|(?:\p{Extended_Pictographic}|\p{Emoji_Presentation})\uFE0F?[\u{E0020}-\u{E007F}]*(?:\u200D(?=\p{Extended_Pictographic}))?/gu, "");
  pendingLogs[logCursor] = {message: text, time: Date.now()};
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
  clearTerminal();
  byId("agent-grid").replaceChildren();
  selectedAgent = undefined;
  messageBusy = false;
  renderAgentSelector([]);
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
    card.tabIndex = 0;
    card.oncontextmenu = (event) => {
      openContextMenu(event, {
        kind: "agent", item: agent, path: `projects/${projectId}/agents/${agent.id}`, projectId, current,
      });
    };
    name.textContent = agent.name;
    details.textContent = [agent.provider, agent.model, agent.enabled ? "Включён" : "Выключен"].join(" · ");
    card.append(name, details);
    return card;
  });
  byId("agent-grid").replaceChildren(...(cards.length ? cards : ["В проекте пока нет агентов."]));
  renderAgentSelector(agents);
}

function setAgentMenu(open, restoreFocus = false) {
  agentMenu.hidden = !open;
  agentSelector.setAttribute("aria-expanded", String(open));
  if (open) agentMenu.querySelector('[aria-pressed="true"]')?.focus();
  if (restoreFocus) agentSelector.focus();
}

function selectAgent(agent) {
  selectedAgent = agent;
  if (agent) byId("agent-selector-label").textContent = agent.name;
  byId("message-input").disabled = !agent;
  byId("send-button").disabled = !agent || messageBusy;
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
byId("message-form").onsubmit = async (event) => {
  event.preventDefault();
  const input = byId("message-input");
  const agentId = selectedAgent?.id;
  const question = input.value.trim();
  if (!project || !agentId || !question || messageBusy) return;
  const current = selection;
  messageBusy = true;
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
    if (current === selection) {
      messageBusy = false;
      byId("send-button").disabled = !selectedAgent;
    }
  }
};

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
      else if (target.current === selection) await loadAgents(target.projectId, target.current);
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
  if (!id && backdrop.inert) return;
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
    byId(id).querySelector("input, .modal-actions .close-modal").focus();
  } else {
    modalTrigger?.focus();
    modalTrigger = null;
  }
}
byId("new-project-button").onclick = () => showModal("project-modal");
byId("add-agent-button").onclick = () => openAgentModal();
const cliProviders = new Set(["codex", "claude", "opencode", "qwen", "cli"]);
const agentForm = byId("agent-form");
const agentProvider = byId("agent-provider");
let agentTarget;

function openAgentModal(target) {
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
  showModal("agent-modal");
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

agentForm.onsubmit = (event) => {
  event.preventDefault();
  if (!agentTarget || agentTarget.current !== selection) return;
  clearAgentErrors();
  const name = byId("agent-name").value.trim();
  const provider = agentProvider.value;
  const isCli = cliProviders.has(provider.toLowerCase());
  const model = byId("agent-model").value.trim() || (isCli ? provider : "");
  const settings = {};
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
  mutateFromModal(event.submitter, agentTarget, agentTarget.item ? "PATCH" : "POST", {name, provider, model, settings}, (error) => {
    const fields = Array.isArray(error.detail)
      ? [...new Set(error.detail.map((item) => item.loc?.at(-1)).filter((field) => Object.hasOwn(agentFields, field)))]
      : [];
    showAgentErrors(fields, error.message);
  });
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
