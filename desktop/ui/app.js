const byId = (id) => document.getElementById(id);
const all = (selector) => document.querySelectorAll(selector);

const sessionKey = "loomis-token";
const homeLighterKey = "loomis_home_lighter_open";
const desktopSessionAPI = window.pywebview?.api?.load_session
  ? Promise.resolve(window.pywebview.api)
  : new URLSearchParams(location.search).get("desktop") === "1"
    ? new Promise(resolve => {
      window.addEventListener("pywebviewready", () => resolve(window.pywebview.api), {once: true});
    })
    : Promise.resolve();
let sessionWrite = Promise.resolve();
let preferencesWrite = Promise.resolve();
let desktopPreferences = {};
let token = null;
let environmentPreflight;

function saveDesktopPreferences(changes) {
  Object.assign(desktopPreferences, changes);
  preferencesWrite = preferencesWrite.then(async () => {
    const bridge = await desktopSessionAPI;
    await bridge?.save_preferences(changes);
    return true;
  }).catch(error => {
    notify(error.message);
    return false;
  });
  return preferencesWrite;
}

function writeDesktopSession(accessToken) {
  sessionWrite = sessionWrite.then(async () => {
    const bridge = await desktopSessionAPI;
    if (accessToken) await bridge?.save_session(accessToken);
    else await bridge?.clear_session();
  }).catch(error => notify(error.message));
  return sessionWrite;
}
let project, socket, authPollTimer;
let selection = 0;
let authAttempt = 0;
let statusRevision = 0;
let specBusy = false;
let actionBusy = null;
const specItemMenu = byId("spec-item-context-menu");
let specMenuTarget, specEdit;

let planMode = false;
let selectedAgent;
const composers = {team: {}, direct: {}};
let specAttachments = [];
const composerNode = (channel, id) => byId(channel === "direct" ? "direct-" + id : id);
let telegramLogsEnabled = false;
let githubUsername = null;
let defaultProjectsDir = "";
let customProjectPath = false;
let telegramId = null;
let telegramCodeTimer;
let telegramLinkAttempt;
let githubDeviceTimer;
let githubDeviceFlow;
let integrationsTimer;
let integrationUpdatePending = false;
let integrationsRevision = 0;
let unlinkProvider;
let historyRequest;
let activeProjectKey;
const deletedAgents = new Set();

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

function notify(message, duration = 4500) {
  const toast = document.createElement("div");
  toast.className = "toast glass-panel";
  toast.textContent = message;
  byId("toasts").append(toast);
  setTimeout(() => toast.remove(), duration);
}

function showCursorToast(message, event) {
  const target = event?.submitter || event?.currentTarget || event?.target;
  const doc = target?.ownerDocument || document;
  const viewport = doc.defaultView;
  const container = doc.getElementById("toasts");
  container.querySelector(".cursor-toast")?.dismiss();
  const toast = doc.createElement("div");
  toast.className = "toast cursor-toast glass-panel";
  toast.textContent = message;
  container.append(toast);

  const anchor = target?.getBoundingClientRect?.();
  const hasCoordinates = Number.isFinite(event?.clientX) && Number.isFinite(event?.clientY)
    && (event.detail || event.clientX || event.clientY);
  const x = hasCoordinates ? event.clientX : anchor?.left ?? 8;
  const y = hasCoordinates ? event.clientY : anchor?.bottom ?? 8;
  toast.style.left = Math.max(8, Math.min(x, viewport.innerWidth - toast.offsetWidth - 8)) + "px";
  toast.style.top = Math.max(8, Math.min(y, viewport.innerHeight - toast.offsetHeight - 8)) + "px";

  toast.dismiss = () => {
    clearTimeout(timeout);
    doc.removeEventListener("click", toast.dismiss, true);
    toast.remove();
  };
  const timeout = setTimeout(toast.dismiss, 2500);
  doc.addEventListener("click", toast.dismiss, {capture: true, once: true});
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("loomis-theme", theme);
  byId("settings-theme").value = theme;
  byId("auth-theme").querySelector("span").textContent = theme === "light" ? "Светлая тема" : "Тёмная тема";
}
byId("auth-theme").onclick = () => {
  const theme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  setTheme(theme);
  saveDesktopPreferences({theme});
};
byId("settings-theme").onchange = ({target}) => {
  setTheme(target.value);
  saveDesktopPreferences({theme: target.value});
};
setTheme(localStorage.getItem("loomis-theme") === "light" ? "light" : "dark");
const homeLighterToggle = byId("lighter-toggle");
homeLighterToggle.setAttribute("aria-expanded", String(localStorage.getItem(homeLighterKey) === "true"));
homeLighterToggle.addEventListener("lighterchange", ({detail: open}) => {
  localStorage.setItem(homeLighterKey, String(open));
  saveDesktopPreferences({homeLighterOpen: open});
});

function showWorkspace(visible) {
  const auth = byId("auth-screen");
  const workspace = byId("app-screen");
  auth.inert = visible;
  workspace.inert = !visible;
  auth.classList.toggle("hidden", visible);
  workspace.classList.toggle("hidden", !visible);
}

let accountHasPassword = false;

function renderAccountName(user) {
  accountHasPassword = user.has_password;
  byId("user-name").dataset.userContent = "";
  byId("user-name").textContent = user.username || "Loomis";
  byId("user-avatar").textContent = byId("user-name").textContent[0].toUpperCase();
  byId("account-username").defaultValue = user.username || "";
}

async function signIn(accessToken, isCurrent = () => true) {
  const user = await api("auth/me", undefined, accessToken);
  if (!isCurrent()) return;
  token = accessToken;
  applyLanguage(user.language);
  activeProjectKey = "loomis-project:" + user.id;
  telegramLogsEnabled = user.tg_logs_enabled;
  renderIntegrations(user);
  defaultProjectsDir = user.default_projects_dir;
  renderTelegramLogs();
  localStorage.setItem(sessionKey, token);
  const saved = writeDesktopSession(token);
  renderAccountName(user);
  showWorkspace(true);
  loadHubModels();
  try {
    await loadProjects(Number(localStorage.getItem(activeProjectKey)));
  } catch (error) {
    if (token === accessToken) notify(error.message);
  }
  await saved;
}

const passwordHint = "Минимум 8 символов, хотя бы одна буква и одна цифра. Избегайте простых паролей.";
const simplePasswords = new Set(["12345678", "123456789", "qwerty123", "password1"]);

function validatePassword(password) {
  const length = Array.from(password).length;
  if (length < 8 || length > 1024 || !/\p{L}/u.test(password) ||
      !/[0-9]/.test(password) || simplePasswords.has(password.toLowerCase())) {
    throw new Error(passwordHint);
  }
}

let registering = false;
let oauthRegistration;
let signupKey;
let emailLinkKey;
let recovery = null;
let recoveryTimer;

const codeCopyTimers = new WeakMap();

function resetCodeCopy(button) {
  clearTimeout(codeCopyTimers.get(button));
  button.classList.remove("copied");
  button.title = "Нажмите, чтобы скопировать";
}

function setConfirmationCode(id, value) {
  const code = byId(id);
  resetCodeCopy(code.parentElement);
  code.textContent = value;
}

async function copyConfirmationCode(button) {
  const code = button.querySelector("code");
  const value = code.textContent;
  if (!value) return;
  try {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      const field = document.createElement("textarea");
      field.className = "clipboard-field";
      field.value = value;
      field.readOnly = true;
      const focused = document.activeElement;
      button.parentElement.append(field);
      try {
        field.select();
        if (!document.execCommand("copy")) throw new Error();
      } finally {
        field.remove();
        focused?.focus({preventScroll: true});
      }
    }
  } catch {
    notify("Не удалось скопировать код.", 1500);
    return;
  }
  if (code.textContent !== value) return;
  resetCodeCopy(button);
  button.classList.add("copied");
  button.title = "Скопировано";
  codeCopyTimers.set(button, setTimeout(() => resetCodeCopy(button), 1500));
  notify("Скопировано", 1500);
}

for (const button of document.querySelectorAll(".code-copy")) {
  button.onclick = () => copyConfirmationCode(button);
}
for (const link of [byId("auth-provider-link"), byId("github-device-link"), byId("recovery-github-link")]) {
  link.onclick = async (event) => {
    if (link.hostname !== "github.com") return;
    event.preventDefault();
    const url = link.href;
    await copyConfirmationCode(link.parentElement.querySelector(".code-copy"));
    window.open(url, "_blank", "noopener,noreferrer");
  };
}

function renderAuthForm() {
  const completing = Boolean(oauthRegistration);
  const signup = registering && !completing;
  const newPassword = signup || completing;
  byId("auth-oauth-title").hidden = !completing;
  byId("auth-username-label").textContent = newPassword ? "Логин" : "Логин или Email";
  byId("auth-email-field").hidden = !signup;
  byId("auth-email").required = signup;
  byId("auth-forgot").hidden = newPassword;
  byId("auth-mode-toggle").hidden = completing;
  byId("auth-submit").textContent = completing ? "Завершить регистрацию"
    : signup ? "Зарегистрироваться" : "Войти в Loomis";
  byId("auth-mode-toggle").textContent = signup ? "Уже есть аккаунт? Войти" : "Нет аккаунта? Зарегистрироваться";
  byId("auth-password-input").autocomplete = newPassword ? "new-password" : "current-password";
  byId("auth-password-input").minLength = newPassword ? 8 : 1;
}

function stopProviderAuth() {
  oauthRegistration = undefined;
  authAttempt++;
  clearTimeout(authPollTimer);
  byId("auth-provider-link").removeAttribute("href");
  setConfirmationCode("auth-provider-code", "");
  byId("password-form").classList.remove("hidden");
  byId("auth-providers").classList.remove("hidden");
  byId("auth-provider-status").classList.add("hidden");
  byId("auth-provider-cancel").classList.add("hidden");
  renderAuthForm();
}

byId("auth-provider-cancel").onclick = () => {
  byId("auth-error").textContent = "";
  byId("password-form").reset();
  stopProviderAuth();
};
byId("auth-mode-toggle").onclick = () => {
  registering = !registering;
  byId("auth-error").textContent = "";
  renderAuthForm();
};
byId("password-form").onsubmit = async (event) => {
  event.preventDefault();
  const controls = ["auth-submit", "auth-mode-toggle", "auth-github", "auth-telegram", "auth-provider-cancel", "auth-forgot"];
  controls.forEach((id) => byId(id).disabled = true);
  byId("auth-error").textContent = "";
  try {
    const credentials = {username: byId("auth-username").value, password: byId("auth-password-input").value};
    if (oauthRegistration) {
      if (!oauthRegistration.accessToken) {
        validatePassword(credentials.password);
        const result = await api("auth/complete-oauth", {...credentials, pending_token: oauthRegistration.pendingToken});
        oauthRegistration.accessToken = result.access_token;
      }
      await signIn(oauthRegistration.accessToken);
      event.target.reset();
      registering = false;
      stopProviderAuth();
      return;
    }
    if (registering) {
      validatePassword(credentials.password);
      const result = await api("auth/register", {
        ...credentials, email: byId("auth-email").value, language: LoomisI18n.language,
      });
      signupKey = result.key;
      byId("auth-password-input").value = "";
      event.target.classList.add("hidden");
      byId("auth-providers").classList.add("hidden");
      byId("signup-code-form").hidden = false;
      byId("signup-code").focus();
      return;
    }
    const result = await api("auth/login", credentials);
    await signIn(result.access_token);
    event.target.reset();
    registering = false;
    stopProviderAuth();
  } catch (error) {
    if (oauthRegistration && !oauthRegistration.accessToken && error.status === 410) {
      const {provider} = oauthRegistration;
      stopProviderAuth();
      void startProviderAuth(provider);
    }
    byId("auth-error").textContent = error.message;
  } finally {
    controls.forEach((id) => byId(id).disabled = false);
  }
};

function closeSignupCode() {
  signupKey = undefined;
  byId("signup-code-form").reset();
  byId("signup-code-form").hidden = true;
  byId("auth-error").textContent = "";
  stopProviderAuth();
}

byId("signup-code-back").onclick = closeSignupCode;
byId("signup-code-form").onsubmit = async (event) => {
  event.preventDefault();
  const key = signupKey;
  const submit = event.submitter;
  submit.disabled = true;
  byId("auth-error").textContent = "";
  try {
    const result = await api("auth/register/verify", {key, code: byId("signup-code").value});
    if (key !== signupKey) return;
    await signIn(result.access_token, () => key === signupKey);
    if (key !== signupKey) return;
    byId("password-form").reset();
    registering = false;
    closeSignupCode();
  } catch (error) {
    if (key === signupKey) byId("auth-error").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
};

function showRecoveryStep(step) {
  for (const id of ["recovery-start", "recovery-code-form", "recovery-provider", "recovery-password-form"]) {
    byId(id).hidden = id !== step;
  }
  const input = byId(step).querySelector("input:not(:disabled)");
  if (input && !input.closest("[hidden]")) input.focus();
}

function openRecovery(account) {
  recovery = {account, session: account ? token : undefined};
  byId(account ? "account-recovery-slot" : "auth-recovery-slot").append(byId("password-recovery"));
  byId("password-recovery").hidden = false;
  byId("recovery-identifier-field").hidden = account;
  byId("recovery-identifier").disabled = account;
  byId("recovery-start").reset();
  byId("recovery-identifier").value = account ? "" : byId("auth-username").value;
  if (account) {
    byId("account-old-password").value = "";
    byId("account-password").value = "";
    byId("account-credentials-form").hidden = true;
  } else {
    byId("auth-password-input").value = "";
    byId("auth-error").textContent = "";
    byId("password-form").classList.add("hidden");
    byId("auth-providers").classList.add("hidden");
  }
  showRecoveryStep("recovery-start");
  if (account) startRecovery();
}

function closeRecovery() {
  if (!recovery) return;
  const {account} = recovery;
  recovery = null;
  clearTimeout(recoveryTimer);
  byId("password-recovery").hidden = true;
  byId("recovery-error").textContent = "";
  byId("recovery-code-form").reset();
  byId("recovery-password-form").reset();
  if (account) {
    byId("account-credentials-form").hidden = false;
  } else {
    stopProviderAuth();
  }
}

async function startRecovery(method = "auto") {
  const attempt = recovery;
  if (!attempt || attempt.starting) return;
  attempt.starting = true;
  const githubButton = byId("recovery-use-github");
  const submit = byId("recovery-start").querySelector("button");
  submit.disabled = githubButton.disabled = true;
  byId("recovery-error").textContent = "";
  try {
    const path = attempt.account ? "auth/recovery/account/start" : "auth/recovery/start";
    const body = attempt.account ? {method} : {method, identifier: byId("recovery-identifier").value};
    const flow = await api(path, body, attempt.session);
    if (attempt !== recovery) return;
    clearTimeout(recoveryTimer);
    attempt.flow = flow;
    if (flow.method === "email") {
      showRecoveryStep("recovery-code-form");
      return;
    }
    const github = flow.method === "github";
    byId("recovery-provider-text").textContent = github
      ? "Введите код на GitHub." : "Подтвердите сброс в привязанном Telegram.";
    byId("recovery-github-code").textContent = github ? flow.user_code : "";
    byId("recovery-github-code").hidden = byId("recovery-github-link").hidden = !github;
    githubButton.hidden = !flow.github_available;
    showRecoveryStep("recovery-provider");
    pollRecovery(attempt, flow);
  } catch (error) {
    if (attempt === recovery) byId("recovery-error").textContent = error.message;
  } finally {
    attempt.starting = false;
    submit.disabled = githubButton.disabled = false;
  }
}

function pollRecovery(attempt, flow) {
  const github = flow.method === "github";
  const path = github ? "auth/recovery/github/poll" : "auth/recovery/poll";
  const body = github ? {key: flow.key, device_code: flow.device_code,
    expires_at: flow.expires_at, signature: flow.signature} : {key: flow.key};
  let interval = github ? flow.interval : 2;
  const current = () => attempt === recovery && attempt.flow === flow;
  const poll = async () => {
    if (!current()) return;
    try {
      const result = await api(path, body, attempt.session);
      if (!current()) return;
      if (result.status === "authorized") {
        attempt.grant = result.key || flow.key;
        showRecoveryStep("recovery-password-form");
        return;
      }
      if (result.status === "slow_down") interval = Math.max(interval + 5, result.interval || 0);
      else if (!["pending", "authorization_pending"].includes(result.status)) {
        throw new Error("Подтверждение недоступно или истекло. Начните заново.");
      }
      recoveryTimer = setTimeout(poll, interval * 1000);
    } catch (error) {
      if (current()) byId("recovery-error").textContent = error.message;
    }
  };
  recoveryTimer = setTimeout(poll, interval * 1000);
}

byId("auth-forgot").onclick = () => openRecovery(false);
byId("account-forgot").onclick = () => openRecovery(true);
byId("recovery-back").onclick = closeRecovery;
byId("recovery-use-github").onclick = () => startRecovery("github");
byId("recovery-start").onsubmit = (event) => {
  event.preventDefault();
  startRecovery();
};
byId("recovery-code-form").onsubmit = async (event) => {
  event.preventDefault();
  const attempt = recovery;
  if (!attempt) return;
  event.submitter.disabled = true;
  byId("recovery-error").textContent = "";
  try {
    const result = await api("auth/recovery/verify", {
      key: attempt.flow.key, code: byId("recovery-code").value,
    }, attempt.session);
    if (attempt !== recovery) return;
    attempt.grant = result.key;
    event.target.reset();
    showRecoveryStep("recovery-password-form");
  } catch (error) {
    if (attempt === recovery) byId("recovery-error").textContent = error.message;
  } finally {
    event.submitter.disabled = false;
  }
};
byId("recovery-password-form").onsubmit = async (event) => {
  event.preventDefault();
  const attempt = recovery;
  if (!attempt) return;
  const current = () => attempt === recovery && (!attempt.account || attempt.session === token);
  event.submitter.disabled = true;
  byId("recovery-error").textContent = "";
  try {
    const body = {key: attempt.grant, password: byId("recovery-password").value};
    validatePassword(body.password);
    const path = attempt.account ? "auth/recovery/account/finish" : "auth/recovery/finish";
    const result = await api(path, body, attempt.session);
    if (!current()) return;
    if (attempt.account) {
      renderAccountName(result);
      closeRecovery();
      showModal();
      notify("Пароль изменён.");
    } else {
      await signIn(result.access_token, current);
      if (current()) closeRecovery();
    }
  } catch (error) {
    if (current()) byId("recovery-error").textContent = error.message;
  } finally {
    event.submitter.disabled = false;
  }
};

function resetEmailLink() {
  emailLinkKey = undefined;
  byId("email-code-form").reset();
  byId("email-code-form").hidden = true;
  byId("email-link-form").hidden = !byId("email-status").hidden;
  byId("email-link-error").textContent = "";
}

byId("email-link-back").onclick = resetEmailLink;
byId("email-link-form").onsubmit = async (event) => {
  event.preventDefault();
  const session = token;
  const revision = modalRevision;
  event.submitter.disabled = true;
  byId("email-link-error").textContent = "";
  try {
    const result = await api("auth/email/start", {email: byId("email-link-address").value}, session);
    if (token !== session || modalRevision !== revision) return;
    emailLinkKey = result.key;
    event.target.hidden = true;
    byId("email-code-form").hidden = false;
    byId("email-link-code").focus();
  } catch (error) {
    if (token === session && modalRevision === revision) byId("email-link-error").textContent = error.message;
  } finally {
    event.submitter.disabled = false;
  }
};
byId("email-code-form").onsubmit = async (event) => {
  event.preventDefault();
  const session = token;
  const key = emailLinkKey;
  event.submitter.disabled = true;
  byId("email-link-error").textContent = "";
  try {
    const user = await api("auth/email/verify", {key, code: byId("email-link-code").value}, session);
    if (token !== session || key !== emailLinkKey) return;
    integrationsRevision++;
    resetEmailLink();
    byId("email-link-form").reset();
    renderIntegrations(user);
    notify("Электронная почта подтверждена.");
  } catch (error) {
    if (token === session && key === emailLinkKey) byId("email-link-error").textContent = error.message;
  } finally {
    event.submitter.disabled = false;
  }
};


async function startProviderAuth(provider) {
  const attempt = ++authAttempt;
  const github = provider === "github";
  const path = "auth/" + provider + "/login/";
  const current = () => attempt === authAttempt;
  const fail = (error) => {
    if (!current()) return;
    stopProviderAuth();
    byId("auth-error").textContent = error.message;
  };

  byId("auth-error").textContent = "";
  byId("password-form").reset();
  byId("password-form").classList.add("hidden");
  byId("auth-providers").classList.add("hidden");
  byId("auth-provider-cancel").classList.remove("hidden");
  try {
    const flow = await api(path + "start", {});
    if (!current()) return;
    const expires = Date.now() + flow.expires_in * 1000;
    const telegramLink = github ? null : new URL(flow.deep_link);
    const start = telegramLink?.searchParams.get("start");
    if (telegramLink && (telegramLink.origin !== "https://t.me" || !/^auth_[A-Za-z0-9_-]{43}$/.test(start))) {
      throw new Error("Ссылка входа в Telegram недоступна.");
    }
    setConfirmationCode("auth-provider-code", github ? flow.user_code : "/start " + start);
    const link = byId("auth-provider-link");
    link.href = github ? "https://github.com/login/device" : telegramLink.href;
    link.textContent = github ? "Открыть GitHub" : "Открыть Telegram";
    byId("auth-provider-copy").textContent = github
      ? "Введите этот код на GitHub."
      : "Откройте бота в Telegram для подтверждения или отправьте ему команду вручную:";
    byId("auth-provider-status").classList.remove("hidden");

    let interval = github ? flow.interval : 1;
    const credentials = github
      ? {device_code: flow.device_code, signature: flow.signature, expires_at: flow.expires_at}
      : {key: flow.key};
    const expired = github ? "Код GitHub истёк. Начните вход заново." : "Время подтверждения истекло.";
    async function poll() {
      if (!current()) return;
      try {
        if (Date.now() >= expires) throw new Error(expired);
        const result = await api(path + "poll", credentials);
        if (!current()) return;
        switch (result.status) {
          case "authorized":
            await signIn(result.access_token, current);
            if (current()) stopProviderAuth();
            return;
          case "needs_registration":
            stopProviderAuth();
            oauthRegistration = {provider, pendingToken: result.pending_token};
            byId("auth-providers").classList.add("hidden");
            renderAuthForm();
            byId("auth-username").focus();
            return;
          case "slow_down":
            interval = Math.max(interval + 5, result.interval || 0);
            break;
          case "authorization_pending":
          case "pending":
            break;
          case "expired_token":
            throw new Error(expired);
          default:
            throw new Error(github ? "Вход через GitHub отклонён." : "Вход через Telegram отклонён.");
        }
        authPollTimer = setTimeout(poll, interval * 1000);
      } catch (error) {
        fail(error);
      }
    }
    authPollTimer = setTimeout(poll, interval * 1000);
  } catch (error) {
    fail(error);
  }
}

byId("auth-github").onclick = () => startProviderAuth("github");
byId("auth-telegram").onclick = () => startProviderAuth("telegram");

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
  pre.dataset.userContent = "";
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

function tableCells(line) {
  const cells = [""];
  let separators = 0;
  const parts = line.trim().match(/\\.|\||[^\\|]+|\\/g) || [];
  for (const part of parts) {
    if (part === "|") {
      separators++;
      cells.push("");
    } else {
      cells[cells.length - 1] += part === "\\|" ? "|" : part;
    }
  }
  if (parts[0] === "|") cells.shift();
  if (parts.at(-1) === "|") cells.pop();
  return {cells: cells.map((cell) => cell.trim()), separators};
}

function markdownTable(header, divider) {
  const {cells: headings, separators} = tableCells(header);
  const {cells: markers, separators: dividerPipes} = tableCells(divider);
  if ((!separators && !dividerPipes) || !headings.length || headings.length !== markers.length
      || !markers.every((marker) => /^:?-+:?$/.test(marker))) return;

  const wrap = element("div", "markdown-table");
  wrap.dataset.userContent = "";
  wrap.tabIndex = 0;
  const table = element("table");
  const appendRow = (row, cells, tag) => {
    markers.forEach((marker, column) => {
      const cell = element(tag);
      if (tag === "th") cell.scope = "col";
      if (marker.endsWith(":")) cell.style.textAlign = marker.startsWith(":") ? "center" : "right";
      else if (marker.startsWith(":")) cell.style.textAlign = "left";
      inlineMarkdown(cell, cells[column] || "");
      row.append(cell);
    });
  };
  appendRow(table.createTHead().insertRow(), headings, "th");
  const rows = table.createTBody();
  wrap.append(table);
  return {wrap, append: (cells) => appendRow(rows.insertRow(), cells, "td")};
}

function renderMarkdown(body, text, terminal) {
  const lines = text.split(/\r?\n/);
  let list;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = line.match(/^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/);
    if (fence) {
      list = undefined;
      const code = [];
      const closing = new RegExp("^\\s*" + fence[1][0] + "{" + fence[1].length + ",}\\s*$");
      while (++i < lines.length && !closing.test(lines[i])) code.push(lines[i]);
      body.append(codeCard(fence[2], code.join("\n")));
      continue;
    }
    const item = line.match(/^\s*(?:[-+*]|\d+[.)])\s+(.+)/);
    const heading = line.match(/^(#{1,6})\s+(.+)/);
    const divider = lines[i + 1];
    const table = !terminal && !item && !heading && line.trim() && divider
      && (line.includes("|") || divider.includes("|")) && markdownTable(line, divider);
    if (table) {
      list = undefined;
      body.append(table.wrap);
      i++;
      while (lines[i + 1]?.includes("|")) {
        const next = lines[i + 1];
        if (/^\s*(?:#{1,6}\s|>|`{3,}|~{3,}|(?:[-+*]|\d+[.)])\s)/.test(next)) break;
        const row = tableCells(next);
        if (!row.separators) break;
        table.append(row.cells);
        i++;
      }
      continue;
    }
    if (item) {
      const tag = /^\s*\d/.test(line) ? "ol" : "ul";
      if (list?.localName !== tag) {
        list = element(tag);
        list.dataset.userContent = "";
        if (tag === "ol") list.start = parseInt(line, 10);
        body.append(list);
      }
      const entry = element("li");
      inlineMarkdown(entry, item[1]);
      list.append(entry);
      continue;
    }
    list = undefined;
    if (terminal) {
      const output = element("span", "", line + (i + 1 < lines.length ? "\n" : ""));
      output.dataset.userContent = "";
      body.append(output);
    } else if (line.trim()) {
      const block = element(heading ? "h" + heading[1].length : "p");
      block.dataset.userContent = "";
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
  if (channel === "terminal") {
    head.append(element("h4", "", event.step_title || "Вывод шага агента"));
  } else {
    head.append(element("strong", "", own ? "Вы" : name || "Агент"));
  }
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
  if (event.media_id != null && event.sender_name !== "User" && channel !== "terminal") {
    const thumbnail = element("img", "chat-thumbnail");
    thumbnail.alt = "Сгенерированное изображение";
    fetch(`/api/projects/${event.project_id}/media/${event.media_id}`, {
      headers: {Authorization: "Bearer " + token},
    }).then(async (response) => {
      if (!response.ok) return;
      const src = URL.createObjectURL(await response.blob());
      thumbnail.onload = thumbnail.onerror = () => URL.revokeObjectURL(src);
      thumbnail.src = src;
      body.append(thumbnail);
    }).catch(() => {});
  }
  entry.append(head, body);
  return entry;
}

function refreshStream(channel, reset = false) {
  const pane = byId(channel);
  const key = {team: 0, direct: selectedAgent?.id, terminal: terminalAgent}[channel];
  const entries = streams[channel].get(key) || [];
  const atBottom = reset || pane.scrollHeight - pane.scrollTop - pane.clientHeight < 20;
  if (reset) pane.replaceChildren();
  while (pane.firstChild && pane.firstChild !== entries[0]?.node) pane.firstChild.remove();
  for (const item of entries) {
    item.node ??= messageNode(item.event, channel);
    if (item.node.parentNode !== pane) pane.append(item.node);
  }
  if (channel === "team") byId("clear-chat").disabled = !entries.length;
  if (channel === "direct") byId("clear-direct").disabled = !entries.length;
  if (channel === "terminal") {
    byId("terminal-count").textContent = entries.length + " записей";
    byId("clear-terminal").disabled = !entries.length;
  }
  if (channel === "team") renderTeamEmpty();
  if (atBottom) pane.scrollTop = pane.scrollHeight;
}

function receiveMessage(event, channel, replay = false) {
  const key = channel === "team" ? 0 : event.agent_id;
  if (key == null || deletedAgents.has(event.agent_id)) return;
  const bucket = streams[channel];
  if (!bucket.has(key)) bucket.set(key, []);
  const entries = bucket.get(key);
  if (!replay && event.id != null && entries.some((item) => item.event.id === event.id)) return;
  entries.push({event});
  if (!replay) frame ??= requestAnimationFrame(() => {
    frame = undefined;
    refreshStream(view);
    refreshStream("direct");
  });
}

function renderTerminalTabs() {
  if (!agentsInProject.some((agent) => agent.id === terminalAgent)) terminalAgent = agentsInProject[0]?.id;
  byId("terminal-tabs").replaceChildren(...agentsInProject.map((agent) => {
    const tab = element("button", "tab", agent.name);
    tab.dataset.userContent = "";
    tab.type = "button";
    tab.setAttribute("aria-pressed", String(agent.id === terminalAgent));
    tab.onclick = () => {
      terminalAgent = agent.id;
      for (const button of tab.parentElement.children) button.setAttribute("aria-pressed", String(button === tab));
      refreshStream(view, true);
    };
    return tab;
  }));
  if (view === "terminal") refreshStream(view, true);
}

async function clearView(channel, agentId) {
  if (!project) return;
  const current = selection;
  const id = project.id;
  try {
    await api("projects/" + id + "/clear-view", {channel, agent_id: agentId});
    await loadHistory(id, current);
  } catch (error) {
    if (current === selection) notify(error.message);
  }
}
byId("clear-terminal").onclick = () => clearView("terminal", terminalAgent);
byId("clear-chat").onclick = () => clearView("team_chat");
byId("clear-direct").onclick = () => clearView("direct", selectedAgent?.id);

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

function renderAttachmentChips(container, attachments, remove, busy) {
  container.replaceChildren();
  attachments.forEach(({file}, index) => {
    const chip = element("span", "attachment-chip");
    chip.innerHTML = icon('<path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6m-6 4h6"/>');
    const name = element("span", "attachment-name");
    name.dataset.userContent = "";
    name.textContent = name.title = file.name;
    const button = element("button", "attachment-remove");
    button.type = "button";
    button.textContent = "×";
    button.setAttribute("aria-label", "Удалить: " + file.name);
    button.disabled = Boolean(busy);
    button.onclick = () => {
      remove(index);
      const buttons = container.querySelectorAll(".attachment-remove");
      (buttons[Math.min(index, buttons.length - 1)] || container.parentElement.querySelector("textarea"))?.focus();
    };
    chip.append(name, button);
    container.append(chip);
  });
}

function setAttachments(channel, attachments = []) {
  composers[channel].attachments = attachments;
  renderChatAttachments(channel);
}

function renderChatAttachments(channel) {
  const {attachments = [], busy} = composers[channel];
  renderAttachmentChips(composerNode(channel, "chat-attachment"), attachments,
    (index) => setAttachments(channel, attachments.filter((_, i) => i !== index)), busy);
}

function renderSpecAttachments() {
  renderAttachmentChips(byId("spec-attachments"), specAttachments, (index) => {
    specAttachments.splice(index, 1);
    renderSpecAttachments();
  }, specBusy || actionBusy);
}

const teamChatWorkingStatuses = new Set(["council", "running", "in_progress"]);
const teamChatWorking = "Агенты выполняют задачу — дождитесь завершения этапа или нажмите «Пауза»";

function syncComposer() {
  for (const channel of Object.keys(composers)) {
    const team = channel === "team";
    const working = team && teamChatWorkingStatuses.has(project?.status);
    if (working) discardRecording(channel);
    const {busy, recording, microphonePending} = composers[channel];
    const available = project && !working && project.status !== "deleted" && actionBusy !== "build_spec"
      && (team || (selectedAgent && !["draft", "council"].includes(project.status)));
    const input = composerNode(channel, "message-input");
    const hasPlan = Boolean(project?.has_plan);
    const editingPlan = team && planMode && hasPlan;
    if (team) {
      const modeChip = byId("plan-mode");
      const statusPill = byId("team-chat-status");
      statusPill.textContent = working ? teamChatWorking : "";
      byId("message-form").classList.toggle("agents-working", working);
      modeChip.textContent = editingPlan ? "Режим: Правка PLAN.md" : "Режим: Обсуждение";
      modeChip.setAttribute("aria-pressed", editingPlan);
      modeChip.disabled = !available || !hasPlan || busy || Boolean(recording) || Boolean(microphonePending);
      modeChip.title = hasPlan ? "Переключить режим отправки" : "Правка доступна после создания плана";
    }
    input.disabled = !available || Boolean(busy);
    composerNode(channel, "chat-attach").disabled = input.disabled || editingPlan;
    composerNode(channel, "chat-attachment").querySelectorAll("button").forEach((button) => {
      button.disabled = Boolean(busy);
    });
    const send = composerNode(channel, "send-button");
    send.disabled = input.disabled || Boolean(recording)
      || (team && !agentsInProject.some((agent) => agent.enabled && agent.agent_role_type !== "image_gen"));
    send.classList.toggle("plan-send", editingPlan);
    send.title = editingPlan ? "Отправить замечания к PLAN.md" : "Отправить";
    send.setAttribute("aria-label", send.title);
    composerNode(channel, "chat-record").disabled = !recording && (input.disabled || editingPlan || Boolean(microphonePending));
    input.placeholder = working ? "Ожидание завершения этапа..." : editingPlan ? "Напишите замечания к PLAN.md…"
      : team ? "Введите сообщение..." : "Напишите агенту…";
    input.setAttribute("aria-label", team ? "Сообщение команде" : "Сообщение агенту");
  }
}

function selectView(next) {
  view = next;
  workCenter.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  for (const channel of ["team", "terminal"]) byId(channel).classList.toggle("hidden", channel !== view);
  byId("terminal-controls").classList.toggle("hidden", view !== "terminal");
  byId("clear-chat").classList.toggle("hidden", view !== "team");
  byId("message-form").classList.toggle("hidden", view !== "team");
  byId("project-review").classList.toggle("hidden", view !== "team" || project?.status !== "review_pending");
  refreshStream(view, true);
}
const workCenter = byId("work-center");
workCenter.querySelectorAll("[data-view]").forEach((button) => button.onclick = () => selectView(button.dataset.view));
selectView(view);

function needsEnvironmentCheck() {
  return ["awaiting_approval", "paused"].includes(project?.status);
}

async function checkEnvironment(reveal = false) {
  if (!needsEnvironmentCheck()) return;
  const check = {pending: true};
  environmentPreflight = check;
  renderEnvironmentCheck();
  renderProjectControls();
  try {
    Object.assign(check, await api("projects/" + project.id + "/preflight", {}));
  } catch {
    check.error = true;
  } finally {
    if (check === environmentPreflight) {
      check.pending = false;
      if (reveal && check.ready === false && !workspaceControls.open) {
        selectWorkspaceTab(byId("workspace-cycle-tab"));
        workspaceControls.open = true;
      }
      renderEnvironmentCheck();
      renderProjectControls();
    }
  }
}

function renderEnvironmentCheck() {
  const check = environmentPreflight;
  byId("environment-preflight").classList.toggle("hidden",
    !check || !(check.pending || check.error || check.tools?.length));
  byId("environment-preflight-status").textContent = check?.pending ? "Проверяем инструменты…"
    : check?.error ? "Не удалось проверить окружение." : "";
  byId("environment-preflight-retry").disabled = Boolean(check?.pending);
  byId("environment-preflight-tools").replaceChildren(...(check?.tools || []).map(({tool, installed}) => {
    const row = document.createElement("li");
    const name = document.createElement("strong");
    const status = document.createElement("span");
    name.textContent = tool;
    name.dataset.userContent = "";
    status.className = installed ? "tool-status installed" : "tool-status";
    status.textContent = installed ? "✓ Установлен" : "✕ Не найден";
    row.append(name, status);
    if (!installed && (window.pywebview || new URLSearchParams(location.search).get("desktop") === "1")) {
      const picker = document.createElement("button");
      picker.type = "button";
      picker.className = "button secondary small";
      picker.textContent = "Указать путь";
      picker.onclick = () => openToolPathPicker(tool);
      row.append(picker);
    }
    return row;
  }));
}

function openToolPathPicker(tool) {
  showModal("tool-path-modal");
  const current = selection;
  for (const [id, folder] of [["tool-path-file", false], ["tool-path-folder", true]]) {
    byId(id).onclick = async () => {
      showModal();
      try {
        const bridge = await desktopSessionAPI;
        const path = await bridge.select_tool_path(folder);
        if (!path) return;
        const previous = desktopPreferences.customToolPaths;
        const paths = {...previous, [tool]: path};
        if (!await saveDesktopPreferences({customToolPaths: paths}) && desktopPreferences.customToolPaths === paths) {
          desktopPreferences.customToolPaths = previous;
        }
        if (current === selection) await checkEnvironment();
      } catch (error) {
        notify(error.message);
      }
    };
  }
}

byId("environment-preflight-retry").onclick = () => checkEnvironment();

function setStatus(status) {
  const previousStatus = project?.status;
  const enteringDraft = status === "draft" && byId("spec-panel").classList.contains("hidden");
  if (project) {
    project.status = status;
    byId("project-list").querySelector(`[data-id="${project.id}"]`)
      ?.toggleAttribute("data-completed", status === "completed");
    if (!["draft", "deleted", "council"].includes(status)) {
      const current = selection;
      const revision = statusRevision;
      api("projects/" + project.id).then(({has_plan}) => {
        if (current !== selection || revision !== statusRevision || project.status !== status) return;
        project.has_plan = has_plan;
        if (!has_plan) planMode = false;
        syncComposer();
      }).catch((error) => {
        if (current === selection && revision === statusRevision) notify(error.message);
      });
    }
  }
  byId("project-status").textContent = ({
    draft: "Черновик", planning: "Планирование", council: "Обсуждение плана",
    awaiting_approval: "План на утверждении",
    running: "В работе", paused: "Пауза", review_pending: "На проверке", completed: "Готово", deleted: "Удалён",
  })[status] || status;
  const step = {draft: 1, planning: 2, council: 2, awaiting_approval: 2,
    running: 3, paused: 3, review_pending: 4, completed: 4}[status];
  all("#project-stepper li").forEach((item, index) => {
    const active = index + 1 === step;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });
  const pause = document.querySelector('[data-action="pause"]');
  pause.querySelector("strong").textContent = status === "paused" ? "Продолжить" : "Пауза";
  updateAgentLights();
  environmentPreflight = undefined;
  if (needsEnvironmentCheck()) checkEnvironment(status === "awaiting_approval");
  else renderEnvironmentCheck();
  renderProjectControls();
  renderGitPush();
  if (gitCommitsVisible()) loadGitCommits();
  syncCompletionScreen();
  byId("project-review").classList.toggle("hidden", view !== "team" || status !== "review_pending");
  if (status === "review_pending" && (previousStatus !== status || !byId("project-review-files").childElementCount)) {
    loadReviewFiles(project.id, selection);
  }
  if (enteringDraft) loadSpecItems(project.id, selection).catch((error) => notify(error.message));
}

function renderProjectControls() {
  const status = project?.status;
  const available = {
    draft: ["build_spec"], planning: ["council"], ready_to_plan: ["council"], awaiting_approval: ["approve"],
    running: ["pause", "rollback"], paused: ["pause", "rollback"], completed: ["rollback"],
  }[status] || [];
  const collectingSpec = status === "draft" && actionBusy !== "build_spec";
  byId("spec-panel").classList.toggle("hidden", !collectingSpec);
  workCenter.hidden = !project || collectingSpec;
  const hasCycleActions = available.length && status !== "draft";
  byId("project-controls").classList.toggle("hidden", !hasCycleActions);
  byId("cycle-empty").hidden = Boolean(hasCycleActions);
  all("[data-action]").forEach((button) => {
    const action = button.dataset.action;
    if (action === "regenerate_plan") return;
    const visible = available.includes(action);
    button.classList.toggle("hidden", !visible);
    button.disabled = !visible || actionBusy ||
      ((action === "approve" || (action === "pause" && status === "paused")) && !environmentPreflight?.ready) ||
      (action === "build_spec" && (specBusy || !byId("spec-items").childElementCount));
  });
  const rebuild = document.querySelector('[data-action="regenerate_plan"]');
  rebuild.disabled = !project || actionBusy || ["draft", "council", "deleted"].includes(status)
    || !agentsInProject.some((agent) => agent.enabled);
  syncComposer();
  byId("spec-add").disabled = byId("spec-cancel-edit").disabled = specBusy || actionBusy;
  byId("spec-attach").disabled = byId("spec-image-button").disabled = specBusy || actionBusy || Boolean(specEdit);
  byId("spec-attachments").querySelectorAll("button").forEach((button) => {
    button.disabled = specBusy || actionBusy || Boolean(specEdit);
  });
  if (status !== "draft" || specBusy || actionBusy) closeSpecItemMenu();
  renderTeamEmpty();
}

function renderTeamEmpty() {
  const card = byId("team-empty");
  const visible = Boolean(project && view === "team" && !streams.team.get(0)?.length);
  card.classList.toggle("hidden", !visible);
  if (view === "team") byId("team").classList.toggle("hidden", visible);
  if (!visible) return;
  const button = byId("team-empty-action");
  const hasAgents = agentsInProject.some((agent) => agent.enabled);
  const status = project.status;
  let copy, label, action;
  if (!hasAgents) {
    copy = "Подключите ИИ-агента, чтобы обсудить задачу и подготовить план.";
    label = "Добавить агента";
    action = () => openAgentModal();
  } else if (status === "draft") {
    copy = "Добавьте материалы для сборки ТЗ.";
    label = "Перейти к ТЗ";
    action = () => byId("spec-text").focus();
  } else if (["planning", "ready_to_plan"].includes(status)) {
    copy = "Начните обсуждение, чтобы агенты подготовили план по ТЗ.";
    label = "Составить план";
    action = () => document.querySelector('[data-action="council"]').click();
  } else if (status === "awaiting_approval") {
    copy = "План готов. Утвердите его, чтобы начать разработку.";
    label = "Запустить разработку";
    action = () => document.querySelector('[data-action="approve"]').click();
  } else {
    copy = status === "council" ? "Агенты готовят план. Сообщения появятся здесь." : "Задайте агентам вопрос о проекте.";
    label = "Написать агентам";
    action = () => byId("message-input").focus();
  }
  byId("team-empty-copy").textContent = copy;
  button.textContent = label;
  button.onclick = action;
  button.disabled = actionBusy || (status === "awaiting_approval" && hasAgents && !environmentPreflight?.ready)
    || (status === "council" && hasAgents && byId("message-input").disabled);
}

function createSpecItem({id, title, content}) {
  const item = document.createElement("li");
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  const text = document.createElement("p");
  summary.textContent = title === "Текст" ? content.slice(0, 100) : title;
  text.textContent = content;
  details.append(summary, text);
  item.append(details);
  item.dataset.userContent = "";
  if (title === "Текст") {
    item.className = "spec-text-item";
    summary.setAttribute("aria-haspopup", "menu");
    item.oncontextmenu = (event) => {
      event.preventDefault();
      if (project?.status !== "draft" || specBusy || actionBusy) return;
      closeContextMenu();
      closeProjectContextMenu();
      specMenuTarget = {id, content, item, summary};
      positionContextMenu(specItemMenu, event, summary);
      specItemMenu.inert = false;
      byId("spec-item-edit").focus({preventScroll: true});
    };
  }
  return item;
}

function appendSpecItem(data) {
  byId("spec-items").append(createSpecItem(data));
}

function closeSpecItemMenu(restoreFocus = false) {
  const target = specMenuTarget;
  specMenuTarget = null;
  specItemMenu.inert = true;
  if (restoreFocus) target?.summary.focus({preventScroll: true});
}

function setSpecEdit(target) {
  const input = byId("spec-text");
  const draft = specEdit?.draft ?? input.value;
  specEdit = target ? {...target, draft} : null;
  input.value = target ? target.content : draft;
  byId("spec-add").textContent = target ? "Сохранить изменения" : "Добавить в ТЗ";
  byId("spec-cancel-edit").hidden = !target;
}

async function mutateSpecItem(target, method, body) {
  const current = selection;
  const path = `projects/${project.id}/spec-items/${target.id}`;
  specBusy = true;
  byId("spec-text").readOnly = true;
  renderProjectControls();
  try {
    const updated = await api(path, body, token, method);
    if (current !== selection) return;
    if (updated) {
      const replacement = createSpecItem(updated);
      replacement.firstElementChild.open = target.item.firstElementChild.open;
      target.item.replaceWith(replacement);
    } else {
      target.item.remove();
      byId("spec-empty").classList.toggle("hidden", Boolean(byId("spec-items").childElementCount));
    }
    if (specEdit?.id === target.id) setSpecEdit(null);
  } catch (error) {
    notify(error.message);
  } finally {
    if (current === selection) {
      specBusy = false;
      byId("spec-text").readOnly = false;
      renderProjectControls();
    }
  }
}

byId("spec-item-edit").onclick = () => {
  if (!specMenuTarget) return;
  setSpecEdit(specMenuTarget);
  closeSpecItemMenu();
  renderProjectControls();
  byId("spec-text").focus();
};
byId("spec-item-delete").onclick = () => {
  const target = specMenuTarget;
  if (!target) return;
  closeSpecItemMenu();
  return mutateSpecItem(target, "DELETE");
};
byId("spec-cancel-edit").onclick = () => {
  setSpecEdit(null);
  renderProjectControls();
  byId("spec-text").focus();
};
specItemMenu.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    closeSpecItemMenu(true);
  } else if (["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const [first, last] = specItemMenu.querySelectorAll("button");
    (event.key === "Home" || (event.key !== "End" && document.activeElement === last) ? first : last).focus();
  }
});
specItemMenu.addEventListener("focusout", (event) => {
  if (!specItemMenu.contains(event.relatedTarget)) closeSpecItemMenu();
});
document.addEventListener("pointerdown", (event) => {
  if (!specItemMenu.contains(event.target)) closeSpecItemMenu();
});
window.addEventListener("resize", () => closeSpecItemMenu());

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

function mediaForm(file, channel, caption = "", targets = []) {
  const data = new FormData();
  data.append("file", file);
  data.append("channel", channel);
  data.append("caption", caption);
  for (const id of targets) data.append("target_agent_ids", id);
  return data;
}

byId("spec-form").onsubmit = async (event) => {
  event.preventDefault();
  const input = byId("spec-text");
  const message = input.value.trim();
  if (project?.status !== "draft" || specBusy || actionBusy || (!message && !specAttachments.length)) return;
  if (specEdit) {
    if (message) await mutateSpecItem(specEdit, "PATCH", {text: message});
    return;
  }
  const current = selection;
  const {id} = project;
  const path = "projects/" + id;
  let refreshItems = false;
  specBusy = true;
  renderProjectControls();
  try {
    if (message) {
      const item = await api(path + "/spec-items", {text: message});
      if (current !== selection) return;
      appendSpecItem(item);
      byId("spec-empty").classList.add("hidden");
      if (input.value.trim() === message) input.value = "";
    }
    for (const attachment of [...specAttachments]) {
      if (current !== selection || project.status !== "draft") break;
      const {file, image} = attachment;
      if (image) {
        await api(path + "/media", mediaForm(file, "spec"));
        refreshItems = true;
      } else {
        const body = new FormData();
        body.append("file", file);
        const item = await api(path + "/spec-items", body);
        if (current !== selection) return;
        appendSpecItem(item);
        byId("spec-empty").classList.add("hidden");
      }
      if (current !== selection) return;
      specAttachments.shift();
      renderSpecAttachments();
    }
  } catch (error) {
    notify(error.message);
  } finally {
    if (current === selection) {
      if (refreshItems && project.status === "draft") {
        await loadSpecItems(id, current).catch((error) => notify(error.message));
      } else {
        specBusy = false;
        renderProjectControls();
      }
    }
  }
};

function specFileExtension(file) {
  return file.name.match(/\.[^.]+$/)?.[0].toLowerCase();
}

const specFileExtensions = new Set(byId("spec-file").accept.split(","));
const specDropImageExtensions = new Set([".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"]);

for (const [picker, trigger, image] of [
  ["spec-file", "spec-attach", false],
  ["spec-image", "spec-image-button", true],
]) {
  byId(trigger).onclick = () => byId(picker).click();
  byId(picker).onchange = ({target}) => {
    const selected = Array.from(target.files);
    target.value = "";
    if (project?.status !== "draft" || specBusy || actionBusy || specEdit) return;
    const files = selected.filter((file) => image || specFileExtensions.has(specFileExtension(file)));
    if (files.length !== selected.length) notify("Этот формат файла не поддерживается.");
    specAttachments.push(...files.map((file) => ({file, image})));
    renderSpecAttachments();
  };
}

const specPanel = byId("spec-panel");
specPanel.addEventListener("dragover", (event) => { if (event.dataTransfer?.types.includes("Files")) { event.preventDefault(); specPanel.classList.add("spec-drop-active"); } });
specPanel.addEventListener("dragleave", (event) => { if (!specPanel.contains(event.relatedTarget)) specPanel.classList.remove("spec-drop-active"); });
specPanel.addEventListener("drop", (event) => {
  event.preventDefault(); specPanel.classList.remove("spec-drop-active");
  if (project?.status !== "draft" || specBusy || actionBusy || specEdit) return;
  const dropped = Array.from(event.dataTransfer.files);
  const files = dropped.map((file) => {
    const extension = specFileExtension(file);
    const image = specDropImageExtensions.has(extension);
    return image || specFileExtensions.has(extension) ? {file, image} : null;
  }).filter(Boolean);
  if (files.length !== dropped.length) notify("Этот формат файла не поддерживается.");
  if (files.length) {
    specAttachments.push(...files);
    renderSpecAttachments();
  }
});

function showCommandApproval({id, command, kind}, projectId) {
  if (Array.from(byId("command-approvals").children).some((panel) => panel.dataset.approvalId === id)) return;
  const panel = document.createElement("section");
  const heading = document.createElement("h3");
  const text = document.createElement("p");
  const buttons = document.createElement("div");
  panel.className = "command-approval glass-panel stack";
  panel.dataset.approvalId = id;
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

function receiveHistoryEvent(event, replay = false) {
  const channel = event.channel || event.type;
  receiveMessage(event, channel === "team_chat" ? "team" : channel, replay);
}

async function loadHistory(projectId, current) {
  if (current !== selection) return;
  const request = {live: []};
  historyRequest = request;
  try {
    const {messages} = await api("projects/" + projectId + "/history");
    if (current !== selection || historyRequest !== request) return;
    Object.values(streams).forEach((stream) => stream.clear());
    const events = new Map([...messages, ...request.live].map((event) => [event.id, event]));
    for (const event of [...events.values()].sort((left, right) => left.id - right.id)) {
      receiveHistoryEvent(event, true);
    }
    refreshStream(view, true);
    refreshStream("direct", true);
  } finally {
    if (historyRequest === request) historyRequest = undefined;
  }
}

function refreshHistory(projectId, current) {
  loadHistory(projectId, current).catch((error) => {
    if (current === selection) notify(error.message);
  });
}

function connect(projectId, current, reconnecting = false) {
  if (current !== selection) return;
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(scheme + "//" + location.host + "/ws/" + projectId, ["loomis", token]);
  let closedApprovals = new Set();
  const connection = socket;
  socket.onopen = () => {
    refreshHistory(projectId, current);
    if (reconnecting) loadAgents(projectId, current).catch((error) => {
      if (current === selection && connection === socket) notify(error.message);
    });
    api("projects/" + projectId + "/approvals")
      .then((approvals) => {
        if (current !== selection || connection !== socket) return;
        for (const approval of approvals) {
          if (!closedApprovals.has(approval.id)) showCommandApproval(approval, projectId);
        }
      })
      .catch((error) => {
        if (current === selection && connection === socket) notify(error.message);
      })
      .finally(() => { closedApprovals = null; });
  };
  socket.onmessage = ({data}) => {
    if (current !== selection || connection !== socket) return;
    const event = JSON.parse(data);
    if (event.type === "status") {
      statusRevision++;
      setStatus(event.status);
      if (event.status === "deleted") loadProjects().catch((error) => notify(error.message));
    }
    if (["team_chat", "direct", "terminal"].includes(event.type)) {
      historyRequest?.live.push(event);
      receiveHistoryEvent(event);
    }
    if (event.type === "history_changed") {
      refreshHistory(projectId, current);
      loadGitCommits();
    }
    if (event.type === "git_commit") loadGitCommits();
    if (event.type === "approval_closed") {
      closedApprovals?.add(event.id);
      Array.from(byId("command-approvals").children)
        .find((panel) => panel.dataset.approvalId === event.id)?.remove();
    }
    if (event.type === "traffic_state") {
      traffic.set(event.agent_id, event);
      updateAgentLights();
    }
    if (event.type === "approval") showCommandApproval(event, projectId);
  };
  socket.onclose = () => {
    if (current !== selection) return;
    setTimeout(() => connect(projectId, current, true), 3500);
  };
}

let clearPageTransition;

function playPageTransition(className, duration, ...targets) {
  const restarting = targets.some((target) => target.classList.contains(className));
  clearPageTransition?.();
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  // Commit the removal before reapplying the same animation.
  if (restarting) {
    for (const target of targets) void target.offsetWidth;
  }
  for (const target of targets) target.classList.add(className);
  const cleanup = () => {
    clearTimeout(timer);
    for (const target of targets) target.classList.remove(className);
    clearPageTransition = undefined;
  };
  const timer = setTimeout(cleanup, duration);
  clearPageTransition = cleanup;
}

async function selectProject(item) {
  const switchingProject = project && item && project.id !== item.id
    && byId("hub-page").classList.contains("hidden");
  const current = ++selection;
  closeContextMenu();
  byId("git-commits").replaceChildren();
  byId("project-review-files").replaceChildren();
  if (item) localStorage.setItem(activeProjectKey, item.id);
  else if (token) localStorage.removeItem(activeProjectKey);
  historyRequest = undefined;
  for (const channel of Object.keys(composers)) {
    discardRecording(channel);
    setAttachments(channel);
    composerNode(channel, "message-input").value = "";
    composers[channel].busy = false;
  }
  byId("workspace-controls").open = false;
  selectWorkspaceTab(workspaceTabs[0]);
  planMode = false;
  deletedAgents.clear();
  project = item;
  delete byId("completion-scene").dataset.chat;
  syncCompletionScreen();
  if (item) showHubPage(false);
  specBusy = false;
  actionBusy = null;
  closeSpecItemMenu();
  if (specEdit) setSpecEdit(null);
  byId("spec-text").readOnly = false;
  byId("spec-panel").classList.add("hidden");
  byId("spec-form").reset();
  specAttachments = [];
  renderSpecAttachments();
  byId("spec-items").replaceChildren();
  byId("spec-empty").classList.remove("hidden");
  byId("command-approvals").replaceChildren();
  socket?.close();
  socket = undefined;
  Object.values(streams).forEach((stream) => stream.clear());
  Object.keys(streams).forEach((channel) => byId(channel).replaceChildren());
  traffic.clear();
  lights.clear();
  agentsInProject = [];
  terminalAgent = undefined;
  renderTerminalTabs();
  byId("agent-grid").replaceChildren();
  selectedAgent = undefined;
  renderDirectAgents([]);
  renderProjectTitle();
  selectView("team");
  setStatus(item?.status || "—");
  if (switchingProject) {
    playPageTransition("project-tab-switch", 140, byId("project-work-area"), byId("completion-scene"));
  }
  all(".project-button").forEach((button) => button.setAttribute("aria-current", button.dataset.id === String(item?.id)));
  byId("stats-button").disabled = byId("add-agent-button").disabled = !item;
  if (!item) return;
  const revision = statusRevision;
  connect(item.id, current);
  await Promise.all([
    loadAgents(item.id, current),
    loadHistory(item.id, current),
    api(`projects/${item.id}`).then((latest) => {
      if (selection === current && statusRevision === revision && latest.status !== project.status) {
        setStatus(latest.status);
      }
    }),
  ]);
}

async function loadAgents(projectId, current) {
  const previousTraffic = new Map(traffic);
  const agents = await api("projects/" + projectId + "/agents");
  if (current !== selection) return;
  for (const [id, event] of previousTraffic) {
    if (traffic.get(id) === event) traffic.delete(id);
  }
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
    name.dataset.userContent = "";
    name.textContent = agent.name;
    if (agent.agent_role_type === "image_gen") name.append(imageBadge());
    details.textContent = [agent.provider, agent.model].filter(Boolean).join(" · ");
    const description = element("div");
    const light = element("button", "traffic-light");
    light.type = "button";
    light.onclick = () => {
      if (!agent.enabled) return;
      selectAgent(agent);
      renderDirectAgents(agentsInProject);
      setDirectOpen(true);
      byId("direct-message-input").focus();
    };
    description.append(name, details);
    const retry = element("button", "icon-button retry-agent");
    retry.innerHTML = icon('<path d="M4 11a8 8 0 1 1 2 6m-2-6V5m0 6h6"/>');
    retry.setAttribute("aria-label", "Перезапустить");
    retry.type = "button";
    retry.onclick = async () => {
      const current = selection;
      retry.dataset.pending = "";
      retry.disabled = true;
      try {
        await api("projects/" + projectId + "/agents/" + agent.id + "/retry", {});
        await loadAgents(projectId, current);
      } catch (error) {
        if (current === selection) notify(error.message);
      } finally {
        delete retry.dataset.pending;
        if (current === selection) updateLight(agent.id);
      }
    };
    const signals = element("div", "agent-signals");
    signals.append(light, retry);
    card.append(description, signals);
    lights.set(agent.id, {light, agent, retry});
    return card;
  });
  byId("agent-grid").replaceChildren(...(cards.length ? cards : ["В проекте пока нет агентов."]));
  updateAgentLights();
  renderDirectAgents(agents);
  renderTerminalTabs();
  renderProjectControls();
}

function updateAgentLights() {
  const showRetry = canShowRetry();
  for (const id of lights.keys()) updateLight(id, showRetry);
}

function canShowRetry() {
  if (!["paused", "error"].includes(project?.status)) return false;
  if (project.failover_mode !== "auto") return true;
  return agentsInProject.every((member) =>
    !member.enabled || (traffic.get(member.id) || member).traffic_state === "error");
}

function updateLight(id, showRetry = canShowRetry()) {
  const card = lights.get(id);
  if (!card) return;
  const {light, agent, retry} = card;
  const state = traffic.get(id) || agent;
  const status = agent.enabled ? state.traffic_state : "error";
  const labels = {working: "Работает", asking: "Ждёт ответа в личном чате", waiting: "Ждёт своей очереди",
    error: state.last_error || "Агент недоступен"};
  const canRetry = state.can_retry ?? agent.can_retry;
  retry.hidden = !agent.enabled || status !== "error" || !showRetry;
  retry.disabled = retry.hidden || "pending" in retry.dataset || !canRetry;
  retry.title = canRetry ? "Перезапустить" : "Нет сохранённого контекста для перезапуска";
  light.dataset.state = status;
  const label = labels[status] || "Состояние неизвестно";
  light.title = label;
  if (selectedAgent?.id === id) renderDirectAgentLight(status, label);
  light.setAttribute("aria-label", agent.name + " · " + light.title);
  light.innerHTML = status === "asking"
    ? icon('<path d="M9 9a3 3 0 1 1 4 2.83c-.67.24-1 .86-1 1.67M12 17h.01"/>')
    : "";
}

let directOpen = !matchMedia("(max-width: 1160px)").matches;

const directResizer = byId("direct-resizer");
const directWidthKey = "loomis-direct-width";
let directWidth;
let directDrag;

function setDirectWidth(width) {
  directWidth = Math.max(280, Math.min(560, Math.round(width)));
  byId("project-page").style.setProperty("--direct-width", directWidth + "px");
  directResizer.setAttribute("aria-valuenow", directWidth);
}

setDirectWidth(Number(localStorage.getItem(directWidthKey)) || 350);
directResizer.onpointerdown = (event) => {
  if (event.button !== 0 || directDrag) return;
  event.preventDefault();
  directResizer.focus();
  directResizer.setPointerCapture(event.pointerId);
  directDrag = {id: event.pointerId, x: event.clientX, width: byId("direct-panel").getBoundingClientRect().width};
  document.body.classList.add("resizing-direct");
};
directResizer.onpointermove = (event) => {
  if (directDrag?.id === event.pointerId) setDirectWidth(directDrag.width + directDrag.x - event.clientX);
};
directResizer.onpointerup = directResizer.onpointercancel = directResizer.onlostpointercapture = (event) => {
  if (directDrag?.id !== event.pointerId) return;
  directDrag = undefined;
  document.body.classList.remove("resizing-direct");
  localStorage.setItem(directWidthKey, directWidth);
  saveDesktopPreferences({direct_width: directWidth});
};
directResizer.onkeydown = (event) => {
  const width = {ArrowLeft: directWidth + 10, ArrowRight: directWidth - 10, Home: 280, End: 560}[event.key];
  if (width === undefined) return;
  event.preventDefault();
  setDirectWidth(width);
  localStorage.setItem(directWidthKey, directWidth);
  saveDesktopPreferences({direct_width: directWidth});
};

function setDirectOpen(open) {
  directOpen = open;
  const panel = byId("direct-panel");
  const wasHidden = panel.classList.contains("hidden");
  panel.classList.toggle("hidden", !project || !open);
  const button = byId("open-direct");
  button.classList.toggle("hidden", !project);
  button.setAttribute("aria-expanded", String(Boolean(project && open)));
  if (project && open && wasHidden) refreshStream("direct", true);
}

byId("open-direct").onclick = () => {
  setDirectOpen(!directOpen);
  if (directOpen) byId("close-direct").focus();
};
byId("close-direct").onclick = () => {
  setDirectOpen(false);
  byId("open-direct").focus();
};

function selectAgent(agent) {
  if (selectedAgent?.id !== agent?.id) {
    byId("direct-message-input").value = "";
    discardRecording("direct");
    setAttachments("direct");
  }
  selectedAgent = agent;
  syncComposer();
  refreshStream("direct", true);
}

const directAgentSelect = byId("direct-agent-select");

function renderDirectAgentLight(status, label) {
  const light = byId("direct-agent-light");
  light.dataset.state = status;
  light.title = label;
  light.setAttribute("aria-label", label);
}

function showDirectAgent(agent) {
  selectAgent(agent);
  byId("direct-agent-name").textContent = agent?.name || "";
  if (agent) updateLight(agent.id);
}

function renderDirectAgents(agents) {
  const enabled = agents.filter((agent) => agent.enabled);
  const next = enabled.find((agent) => agent.id === selectedAgent?.id) || enabled[0];
  directAgentSelect.replaceChildren(...enabled.map((agent) => {
    const option = new Option(agent.name, agent.id);
    option.dataset.userContent = "";
    return option;
  }));
  directAgentSelect.value = next?.id ?? "";
  byId("direct-agent-picker").hidden = !next;
  byId("direct-empty").classList.toggle("hidden", Boolean(next));
  showDirectAgent(next);
}

directAgentSelect.onchange = () => {
  showDirectAgent(agentsInProject.find((agent) => agent.enabled && String(agent.id) === directAgentSelect.value));
};

function projectAcronym(name) {
  const capitals = name.match(/\p{Lu}/gu) || [];
  const initials = capitals.length >= 2
    ? name.match(/[\p{Lu}\p{Nd}]/gu)
    : (name.match(/[\p{L}\p{Nd}]/gu) || []);
  return initials.slice(0, 3).join("").toUpperCase() || "—";
}

let dashboardUsageRequest = 0;
async function loadDashboardUsage() {
  const session = token;
  const request = ++dashboardUsageRequest;
  try {
    const usage = await api("projects/dashboard-usage");
    if (session !== token || request !== dashboardUsageRequest) return;
    byId("welcome-tokens").textContent = usage.total_tokens.toLocaleString("ru-RU");
  } catch (error) {
    if (session === token && request === dashboardUsageRequest) notify(error.message);
  }
}

async function loadProjects(preferredId = project?.id, preserveSelection = false) {
  const session = token;
  const current = selection;
  const projects = await api("projects");
  if (!token || token !== session) return;
  byId("welcome-projects").textContent = projects.length;
  byId("welcome-completed").textContent = projects.filter((item) => item.status === "completed").length;
  loadDashboardUsage();
  byId("welcome-dashboard").dataset.projectCount = projects.length;
  const buttons = projects.map((item) => {
    const button = document.createElement("button");
    button.className = "project-button project-item";
    button.dataset.id = item.id;
    button.dataset.userContent = "";
    button.type = "button";
    button.title = item.name;
    button.setAttribute("aria-label", item.name);
    button.toggleAttribute("data-completed", item.status === "completed");
    button.append(element("span", "project-name", item.name),
      element("span", "project-acronym", projectAcronym(item.name)));
    button.lastElementChild.setAttribute("aria-hidden", "true");
    button.insertAdjacentHTML("beforeend",
      '<svg class="project-completed" viewBox="0 0 16 16" aria-hidden="true"><path d="m3 8 3 3 7-7"/></svg>');
    button.addEventListener("contextmenu", (event) => openProjectContextMenu(event, item));
    button.onclick = () => {
      document.querySelector(".sidebar").classList.remove("open");
      if (project?.id === item.id) {
        showHubPage(false);
        refreshHistory(item.id, selection);
        return;
      }
      selectProject(item).catch((error) => notify(error.message));
    };
    return button;
  });
  closeProjectContextMenu();
  byId("project-list").replaceChildren(...(buttons.length ? buttons : [element("span", "project-list-empty", "Нет проектов")]));
  const active = preserveSelection && projects.find((item) => item.id === project?.id);
  if (active) {
    project.name = active.name;
    project.workspace_path = active.workspace_path;
    project.git_repo_url = active.git_repo_url;
    renderProjectTitle();
    buttons[projects.indexOf(active)].setAttribute("aria-current", "true");
    return;
  }
  if (current === selection) await selectProject(projects.find((item) => item.id === preferredId));
}

async function runProjectAction(button) {
  if (!project || button.disabled) return;
  const current = selection;
  const revision = statusRevision;
  const path = "projects/" + project.id + "/actions";
  let action = button.dataset.action;
  if (action === "pause" && project.status === "paused") action = "resume";
  actionBusy = action;
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
    if (current === selection && ["approve", "resume"].includes(action)) checkEnvironment();
  } finally {
    if (current === selection) {
      actionBusy = null;
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

for (const channel of Object.keys(composers)) {
  const control = (id) => composerNode(channel, id);
  const input = control("message-input");
  const resizeHandle = control("message-resizer");
  resizeHandle.onpointerdown = (event) => {
    if (event.button !== 0 || resizeHandle.onpointermove) return;
    event.preventDefault();
    const startY = event.clientY;
    const startHeight = input.getBoundingClientRect().height;
    resizeHandle.setPointerCapture(event.pointerId);
    resizeHandle.onpointermove = (move) => {
      if (move.pointerId !== event.pointerId) return;
      input.style.height = Math.max(44, Math.min(280, startHeight + startY - move.clientY)) + "px";
    };
  };
  resizeHandle.onlostpointercapture = () => { resizeHandle.onpointermove = null; };
  control("message-input").onkeydown = (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!control("send-button").disabled) control("message-form").requestSubmit();
    }
  };
  control("chat-attach").onclick = () => control("chat-file").click();
  control("chat-file").onchange = ({target}) => {
    const files = Array.from(target.files, (file) => ({file}));
    target.value = "";
    if (files.length && !control("chat-attach").disabled) {
      setAttachments(channel, [...(composers[channel].attachments || []), ...files]);
    }
  };
  control("chat-record").onclick = () => recordMessage(channel);
  control("message-form").onsubmit = (event) => {
    event.preventDefault();
    sendMessage(channel, event);
  };
}

function discardRecording(channel) {
  const state = composers[channel];
  state.microphonePending = undefined;
  if (!state.recording) return;
  const {recorder} = state.recording;
  state.recording = undefined;
  if (recorder.state !== "inactive") recorder.stop();
  recorder.stream.getTracks().forEach((track) => track.stop());
  const button = composerNode(channel, "chat-record");
  button.setAttribute("aria-pressed", "false");
  button.title = "Записать голосовое сообщение";
}

async function recordMessage(channel) {
  const state = composers[channel];
  const button = composerNode(channel, "chat-record");
  if (state.recording) {
    if (state.recording.recorder.state !== "inactive") state.recording.recorder.stop();
    return;
  }
  const request = Symbol();
  state.microphonePending = request;
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
    if (state.microphonePending !== request || composerNode(channel, "message-input").disabled) return;
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
      if (state.recording?.recorder !== recorder) return;
      discardRecording(channel);
      if (bytes) {
        const file = new File(parts, "Голосовое сообщение." + extension, {type: mimeType});
        setAttachments(channel, [...(state.attachments || []), {file}]);
      }
      syncComposer();
    };
    recorder.onerror = () => {
      if (state.recording?.recorder !== recorder) return;
      discardRecording(channel);
      syncComposer();
      notify("Не удалось записать голосовое сообщение.");
    };
    recorder.start(1000);
    state.recording = {recorder};
    button.setAttribute("aria-pressed", "true");
    button.title = "Остановить запись";
  } catch (error) {
    if (state.microphonePending === request) notify(error.message);
  } finally {
    if (!state.recording || state.recording.recorder.stream !== stream) stream?.getTracks().forEach((track) => track.stop());
    if (state.microphonePending === request) state.microphonePending = undefined;
    syncComposer();
  }
}

async function sendMessage(channel, event) {
  const input = composerNode(channel, "message-input");
  const agentId = selectedAgent?.id;
  const state = composers[channel];
  const feedback = channel === "team" && planMode && project?.has_plan;
  const message = input.value.trim();
  const {attachments = [], recording} = state;
  const hasFiles = attachments.length > 0;
  if (!project || composerNode(channel, "send-button").disabled || recording) return;
  if (feedback && (!message || hasFiles)) {
    showCursorToast("Для доработки плана введите текст без вложения.", event);
    return;
  }
  if (!message && !hasFiles) return;
  const current = selection;
  const path = "projects/" + project.id;
  const revision = statusRevision;
  const targets = channel === "direct" ? [agentId] : [];
  state.busy = true;
  syncComposer();
  try {
    if (feedback) {
      const result = await api(path + "/council/feedback", {feedback: message});
      if (current === selection && revision === statusRevision) setStatus(result.status);
    } else if (hasFiles) {
      for (const attachment of attachments) {
        if (current !== selection || !state.attachments.includes(attachment)) return;
        await api(path + "/media", mediaForm(attachment.file, channel === "team" ? "team_chat" : "direct", message, targets));
        if (current === selection && (channel === "team" || selectedAgent?.id === agentId)) {
          setAttachments(channel, state.attachments.filter((item) => item !== attachment));
        }
      }
    } else if (channel === "team") {
      await api(path + "/chat", {question: message});
    } else {
      await api(path + "/agents/" + agentId + "/question", {question: message});
    }
    if (current === selection && (channel === "team" || selectedAgent?.id === agentId)) {
      if (input.value.trim() === message) input.value = "";
    }
  } catch (error) {
    notify(error.message);
  } finally {
    if (current === selection) {
      state.busy = false;
      syncComposer();
    }
  }
}

byId("plan-mode").onclick = () => {
  planMode = !planMode;
  syncComposer();
};

const contextMenu = byId("context-menu");
let contextTarget, contextTrigger, modalTrigger;
let modalRevision = 0;

function openContextMenu(event, target) {
  event.preventDefault();
  closeSpecItemMenu();
  contextTrigger = event.currentTarget;
  contextTarget = target;
  closeProjectContextMenu();
  const edit = byId("context-edit");
  edit.innerHTML = icon('<path d="m14 5 5 5M4 20l1-5L16 4a2 2 0 0 1 3 0l1 1a2 2 0 0 1 0 3L9 19l-5 1Z"/>');
  edit.append("Редактировать");
  const commitMenu = target.kind === "commit";
  edit.hidden = byId("context-delete").hidden = commitMenu;
  const rollback = byId("context-rollback-commit");
  const copy = byId("context-copy-commit");
  rollback.hidden = copy.hidden = !commitMenu;
  rollback.disabled = gitRollbackPending || ["running", "council"].includes(project?.status);
  positionContextMenu(contextMenu, event, contextTrigger);
  contextMenu.inert = false;
  contextMenu.querySelector("button:not([hidden]):enabled").focus({preventScroll: true});
}

function positionContextMenu(menu, event, trigger) {
  const margin = 8;
  const anchor = trigger.getBoundingClientRect();
  const x = event.clientX || (event.clientY ? 0 : anchor.left);
  const y = event.clientY || (event.clientX ? 0 : anchor.bottom);
  menu.style.left = Math.max(margin, Math.min(x, innerWidth - menu.offsetWidth - margin)) + "px";
  menu.style.top = Math.max(margin, Math.min(y, innerHeight - menu.offsetHeight - margin)) + "px";
}

function closeContextMenu(restoreFocus = false) {
  if (contextMenu.inert) return;
  contextMenu.inert = true;
  if (restoreFocus) contextTrigger?.focus({preventScroll: true});
  contextTarget = contextTrigger = null;
}

document.addEventListener("click", (event) => {
  if (!contextMenu.contains(event.target)) closeContextMenu();
});
window.addEventListener("resize", () => closeContextMenu());
contextMenu.addEventListener("keydown", (event) => {
  if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const [first, last = first] = contextMenu.querySelectorAll("button:not([hidden]):enabled");
  (event.key === "Home" || (event.key !== "End" && document.activeElement === last) ? first : last).focus();
});

byId("context-edit").onclick = () => {
  const target = contextTarget;
  closeContextMenu(true);
  openAgentModal(target);
};

function renameProject(target) {
  byId("rename-name").value = target.item.name;
  showModal("rename-modal");
  byId("rename-form").onsubmit = (event) => {
    event.preventDefault();
    const name = byId("rename-name").value.trim();
    if (!name) {
      showCursorToast("Введите название проекта.", event);
      return;
    }
    mutateFromModal(event.submitter, target, "PATCH", {name});
  };
}

byId("context-delete").onclick = () => {
  const target = contextTarget;
  closeContextMenu(true);
  confirmContextDeletion(target);
};

function confirmContextDeletion(target) {
  const isProject = target.kind === "project";
  const kind = isProject ? "проект" : "агента";
  const repo = isProject && githubUsername && target.item.github_repo_url
    ? new URL(target.item.github_repo_url.trim()).pathname.replace(/^\/|\/+$/g, "").replace(/\.git$/, "")
    : "";
  const checkbox = byId("delete-github-repo");
  const localCheckbox = byId("delete-local-folder");
  checkbox.checked = false;
  localCheckbox.checked = false;
  byId("delete-local-option").classList.toggle("hidden", !isProject);
  byId("delete-local-path").textContent = isProject ? (target.item.workspace_path || "") : "";
  byId("delete-github-option").classList.toggle("hidden", !repo);
  byId("delete-github-name").textContent = repo;
  byId("delete-title").textContent = `Удалить ${kind} «${target.item.name}»?`;
  showModal("delete-modal");
  byId("confirm-delete").onclick = ({currentTarget}) => {
    const path = isProject ? `${target.path}?delete_github_repo=${Boolean(repo && checkbox.checked)}&delete_local_folder=${Boolean(localCheckbox.checked)}` : target.path;
    mutateFromModal(currentTarget, {...target, path}, "DELETE");
  };
}

function removeAgentHistory({id, name}) {
  deletedAgents.add(id);
  streams.direct.delete(id);
  streams.terminal.delete(id);
  const messages = streams.team.get(0) || [];
  streams.team.set(0, messages.filter(({event}) => event.agent_id !== id
    && (event.agent_id != null || (event.sender_name !== name && event.target_name !== "@" + name))));
  byId("direct").replaceChildren();
  byId("terminal").replaceChildren();
  refreshStream(view, true);
  refreshStream("direct", true);
}

async function mutateFromModal(button, target, method, body, onError = (error) => notify(error.message)) {
  const revision = modalRevision;
  const session = token;
  button.disabled = true;
  try {
    const result = await api(target.path, body, session, method);
    if (revision === modalRevision) showModal();
    if (session !== token) return;
    if (target.kind === "project" && method === "DELETE") {
      notify(result?.github_repo_deleted ? "Проект и репозиторий GitHub удалены." : "Проект удалён.");
      if (result?.warning) notify(result.warning);
    }
    try {
      if (target.kind === "project") await loadProjects(project?.id, true);
      else if (target.current === selection) {
        if (method === "DELETE") {
          removeAgentHistory(target.item);
          agentsInProject = agentsInProject.filter((agent) => agent.id !== target.item.id);
          renderDirectAgents(agentsInProject);
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
  if (id !== "settings-modal") resetEmailLink();
  if (id !== "account-credentials-modal") {
    byId("account-password").value = "";
    byId("account-old-password").value = "";
    if (recovery?.account) closeRecovery();
  }
  modalRevision++;
  clearInterval(integrationsTimer);
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
const cliCommands = {
  codex: "codex exec --json --sandbox workspace-write -",
  claude: "claude -p",
  opencode: "opencode run",
  qwen: "qwen",
};
const agentForm = byId("agent-form");
const agentProvider = byId("agent-provider");
const agentRole = byId("agent-role");

function imageBadge() {
  const badge = element("span", "image-agent-badge", "IMG");
  badge.title = "Генератор изображений";
  return badge;
}

function syncRoleProvider(role, provider) {
  const image = role.value === "image_gen";
  for (const option of provider.options) {
    option.disabled = image && cliProviders.has(option.value.toLowerCase());
  }
  if (provider.selectedOptions[0]?.disabled) provider.value = "api";
}
let agentTarget;

function openAgentModal(target, manual = false, preselected = []) {
  if (!project) return;
  agentTarget = target || {kind: "agent", path: "projects/" + project.id + "/agents", projectId: project.id, current: selection};
  const agent = target?.item;
  const settings = agent?.settings || {};
  agentForm.reset();
  agentRole.disabled = agentProvider.disabled = false;
  byId("agent-test-status").classList.add("hidden");
  byId("agent-preset-search").value = "";
  agentProvider.querySelector("[data-custom]")?.remove();
  const provider = agent?.provider || "codex";
  if (!Array.from(agentProvider.options).some((option) => option.value === provider)) {
    const option = new Option(provider, provider);
    option.dataset.custom = "";
    agentProvider.add(option);
  }
  agentProvider.value = provider;
  agentRole.value = agent?.agent_role_type || "conversational";
  byId("agent-name").value = agent?.name || "";
  byId("agent-model").value = agent?.model || "";
  byId("agent-base-url").value = settings.base_url || settings.baseUrl || "";
  byId("agent-api-key").placeholder = settings.has_api_key ? "Ключ сохранён · введите новый для замены" : "";
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
  syncRoleProvider(agentRole, agentProvider);
  const isApi = !cliProviders.has(agentProvider.value.toLowerCase());
  byId("agent-api-fields").inert = !isApi;
  byId("agent-base-url").disabled = byId("agent-api-key").disabled = !isApi;
  byId("agent-model").placeholder = isApi ? "" : agentProvider.value;
  clearAgentErrors();
}
agentProvider.onchange = agentRole.onchange = renderAgentFields;
agentForm.addEventListener("input", ({target}) => {
  target.removeAttribute("aria-invalid");
  if (!agentForm.querySelector("[aria-invalid]")) byId("agent-error").textContent = "";
});
all(".close-modal").forEach((button) => button.onclick = () => showModal());
byId("modal-backdrop").onclick = (event) => { if (event.target === event.currentTarget) showModal(); };
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || byId("unlink-dialog").open) return;
  if (!contextMenu.inert) closeContextMenu(true);
  else showModal();
});
const sidebarToggle = byId("sidebar-toggle");
const sidebarRailKey = "loomis-sidebar-rail";

function setSidebarRail(collapsed) {
  byId("app-screen").classList.toggle("icon-rail", collapsed);
  sidebarToggle.title = collapsed ? "Развернуть" : "Свернуть в значки";
  sidebarToggle.setAttribute("aria-label", sidebarToggle.title);
  sidebarToggle.setAttribute("aria-expanded", String(!collapsed));
}

setSidebarRail(localStorage.getItem(sidebarRailKey) === "true");
sidebarToggle.onclick = () => {
  const collapsed = sidebarToggle.getAttribute("aria-expanded") === "true";
  setSidebarRail(collapsed);
  localStorage.setItem(sidebarRailKey, String(collapsed));
  saveDesktopPreferences({sidebar_collapsed: collapsed});
};

byId("hub-mobile-menu").onclick = byId("mobile-menu").onclick = () => document.querySelector(".sidebar").classList.toggle("open");

const agentFields = {
  name: ["agent-name", "Имя должно содержать от 2 до 100 символов."],
  model: ["agent-model", "Введите имя модели длиной до 255 символов."],
  base_url: ["agent-base-url", "Введите корректный Base URL с http:// или https://."],
  api_key: ["agent-api-key", "Введите API Key."],
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
  const settings = {};
  const errors = [];
  if ([...name].length < 2 || [...name].length > 100) errors.push("name");
  if (!model || [...model].length > 255) errors.push("model");
  if (isCli) {
    const command = cliCommands[provider.toLowerCase()];
    if (command && (agentTarget.item?.provider !== provider || !agentTarget.item.settings?.command)) {
      settings.command = command;
    }
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
  const candidate = {name, provider, model, settings, agent_role_type: agentRole.value};
  const showError = (error) => {
    const fields = Array.isArray(error.detail)
      ? [...new Set(error.detail.map((item) => item.loc?.at(-1)).filter((field) => Object.hasOwn(agentFields, field)))]
      : [];
    showAgentErrors(fields, error.message);
  };
  const fields = [...agentForm.querySelectorAll("input:enabled")];
  fields.forEach((field) => { field.readOnly = true; });
  byId("agent-hub-tab").disabled = byId("agent-manual-tab").disabled = true;
  agentProvider.disabled = agentRole.disabled = button.disabled = true;
  status.classList.remove("hidden");
  let failure;
  try {
    if (candidate.agent_role_type !== "image_gen") {
      await api("projects/" + target.projectId + "/agents/test", {
        ...candidate, ...(target.item && {agent_id: target.item.id}),
      });
    }
  } catch (error) {
    failure = error;
  } finally {
    status.classList.add("hidden");
    fields.forEach((field) => { field.readOnly = false; });
    byId("agent-hub-tab").disabled = byId("agent-manual-tab").disabled = false;
    agentProvider.disabled = agentRole.disabled = button.disabled = false;
  }
  if (revision !== modalRevision || target.current !== selection) return;
  if (failure) {
    showError(failure);
    return;
  }
  if (candidate.agent_role_type === "image_gen") {
    status.classList.remove("hidden");
    agentRole.disabled = agentProvider.disabled = true;
  }
  try {
    await mutateFromModal(button, target, target.item ? "PATCH" : "POST", candidate, showError);
  } finally {
    if (revision === modalRevision) {
      status.classList.add("hidden");
      agentRole.disabled = agentProvider.disabled = false;
    }
  }
};

function renderTelegramLogs() {
  byId("telegram-logs").checked = telegramLogsEnabled;
  byId("telegram-logs-state").textContent = telegramLogsEnabled ? "Вкл" : "Выкл";
}

function renderProjectTitle() {
  for (const id of ["project-heading", "project-status", "project-settings-button",
    "stats-button", "project-stepper", "project-work-area"]) {
    byId(id).classList.toggle("hidden", !project);
  }
  document.querySelector('[data-action="regenerate_plan"]').classList.toggle("hidden", !project);
  byId("welcome-dashboard").classList.toggle("hidden", Boolean(project));
  setDirectOpen(directOpen);
  renderGitPush();
  byId("project-settings-button").disabled = !project;
  const projectName = byId("current-project-name");
  projectName.toggleAttribute("data-user-content", Boolean(project));
  projectName.textContent = project?.name || "Выберите проект";
  byId("current-project-workspace").textContent = project ? "Папка проекта на ПК: " + project.workspace_path : "";
  byId("current-project-git").textContent = project?.git_repo_url ? "Git-репозиторий: " + project.git_repo_url : "";
}

function openDashboard() {
  showHubPage(false);
  selectProject();
  if (byId("sidebar").classList.contains("open")) {
    byId("sidebar").classList.remove("open");
    byId("mobile-menu").focus();
  }
  loadProjects().catch((error) => notify(error.message));
}

byId("brand-home").onclick = openDashboard;

const workspaceControls = byId("workspace-controls");
const workspaceTabs = [...all(".workspace-tabs .tab")];
const gitPanel = byId("git-panel");

function gitCommitsVisible() {
  return workspaceControls.open && !gitPanel.hidden;
}

function selectWorkspaceTab(activeTab) {
  for (const tab of workspaceTabs) {
    const active = tab === activeTab;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    byId(tab.getAttribute("aria-controls")).hidden = !active;
  }
  if (gitCommitsVisible()) loadGitCommits();
  else if (contextTarget?.kind === "commit") closeContextMenu();
}

for (const [index, tab] of workspaceTabs.entries()) {
  tab.onclick = () => selectWorkspaceTab(tab);
  tab.onkeydown = (event) => {
    const count = workspaceTabs.length;
    const next = {
      ArrowLeft: (index + count - 1) % count, ArrowRight: (index + 1) % count,
      Home: 0, End: count - 1,
    }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    selectWorkspaceTab(workspaceTabs[next]);
    workspaceTabs[next].focus();
  };
}
let gitRequest = 0;
let gitPushPending = false;
let gitRollbackPending = false;

function renderGitPush() {
  const push = byId("git-push");
  push.classList.toggle("hidden", project?.auto_push_mode !== "manual_finish");
  push.disabled = gitPushPending || !project?.git_repo_url || project.status !== "completed";
  push.title = !project?.git_repo_url ? "Укажите Git-репозиторий в настройках проекта."
    : push.disabled && !gitPushPending ? "Доступно после завершения проекта." : "";
}

byId("git-push").onclick = async () => {
  if (!project || gitPushPending) return;
  const current = selection;
  gitPushPending = true;
  renderGitPush();
  try {
    await api("projects/" + project.id + "/push", {});
    if (current === selection) notify("Коммиты отправлены.");
  } catch (error) {
    if (current === selection) notify(error.message);
  } finally {
    gitPushPending = false;
    renderGitPush();
  }
};

workspaceControls.ontoggle = () => {
  if (gitCommitsVisible()) loadGitCommits();
  else if (contextTarget?.kind === "commit") closeContextMenu();
};

async function loadGitCommits() {
  if (!project || !gitCommitsVisible()) return;
  const current = selection;
  const path = "projects/" + project.id;
  const request = ++gitRequest;
  const list = byId("git-commits");
  if (!list.childElementCount) list.replaceChildren(element("p", "", "Загрузка коммитов…"));
  try {
    const commits = await api(path + "/commits");
    if (current !== selection || request !== gitRequest || !gitCommitsVisible()) return;
    if (contextTarget?.kind === "commit") closeContextMenu();
    list.replaceChildren(...(commits.length ? commits.map((commit) => {
      const row = element("div", "git-commit glass-card");
      row.tabIndex = 0;
      row.oncontextmenu = (event) => openContextMenu(event, {kind: "commit", commit, path, current});
      row.append(
        element("strong", "", commit.short_hash + " · " + commit.message.split("\n", 1)[0]),
        element("small", "", commit.author + " · " + new Date(commit.created_at).toLocaleString())
      );
      return row;
    }) : [element("p", "", "Коммитов пока нет.")]));
  } catch (error) {
    if (current === selection && request === gitRequest && gitCommitsVisible()) list.replaceChildren(element("p", "", error.message));
  }
}

byId("context-copy-commit").onclick = async () => {
  const {commit} = contextTarget;
  closeContextMenu(true);
  try {
    await navigator.clipboard.writeText(commit.hash);
    notify("Хэш коммита скопирован.");
  } catch {
    notify("Не удалось скопировать хэш коммита.");
  }
};

byId("context-rollback-commit").onclick = async () => {
  const {commit, path, current} = contextTarget;
  closeContextMenu(true);
  if (current !== selection || gitRollbackPending ||
    !confirm(LoomisI18n.translate("Откатить файлы и контекст ИИ до коммита " + commit.short_hash + "?"))) return;
  gitRollbackPending = true;
  try {
    const result = await api(path + "/commits/" + commit.hash + "/rollback", {});
    if (current !== selection) return;
    closeContextMenu();
    setStatus(result.status);
    refreshHistory(project.id, current);
    await loadAgents(project.id, current);
    if (!result.context_restored) notify("Для этого коммита история восстановлена по дате. План нужно утвердить заново.");
  } catch (error) {
    if (current === selection) notify(error.message);
  } finally {
    gitRollbackPending = false;
  }
};

for (const button of all("[data-folder-input]")) {
  button.onclick = async (event) => {
    const field = byId(button.dataset.folderInput);
    const revision = modalRevision;
    button.disabled = true;
    try {
      const path = await window.pywebview?.api?.select_folder?.();
      if (revision === modalRevision && path) {
        field.value = path;
        if (field.id === "project-workspace") customProjectPath = true;
      }
    } catch {
      if (revision === modalRevision) showCursorToast("Введите путь к папке вручную.", event);
    } finally {
      button.disabled = false;
      if (revision === modalRevision) field.focus();
    }
  };
}

const locationForm = byId("project-location-form");
const projectsDirField = byId("default-projects-dir");

function selectSettingsTab(tab) {
  for (const button of all(".settings-tabs .tab")) {
    const selected = button === tab;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", selected);
    button.tabIndex = selected ? 0 : -1;
    byId(button.getAttribute("aria-controls")).hidden = !selected;
  }
  clearInterval(integrationsTimer);
  if (tab.id === "integrations-tab") {
    refreshIntegrations();
    integrationsTimer = setInterval(() => {
      if (!telegramLinkAttempt) refreshIntegrations();
    }, 3000);
  }
}

const settingsTabs = [...all(".settings-tabs .tab")];
for (const tab of settingsTabs) {
  tab.onclick = () => selectSettingsTab(tab);
  tab.onkeydown = (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const index = settingsTabs.indexOf(tab);
    const offset = event.key === "ArrowLeft" ? -1 : 1;
    const next = event.key === "Home" ? settingsTabs[0]
      : event.key === "End" ? settingsTabs.at(-1)
      : settingsTabs[(index + offset + settingsTabs.length) % settingsTabs.length];
    selectSettingsTab(next);
    next.focus();
  };
}

function applyLanguage(language) {
  LoomisI18n.setLanguage(language);
  for (const card of all("[data-language]")) {
    card.setAttribute("aria-pressed", card.dataset.language === LoomisI18n.language);
  }
}

let languageWrite;
for (const card of all("[data-language]")) {
  card.onclick = async () => {
    const session = token;
    const previous = LoomisI18n.language;
    const language = card.dataset.language;
    if (!session || previous === language) return;
    applyLanguage(language);

    if (languageWrite?.session === session) {
      languageWrite.desired = language;
      return;
    }
    const write = {session, saved: previous, desired: language};
    languageWrite = write;
    try {
      while (token === session && write.saved !== write.desired) {
        const requested = write.desired;
        try {
          await api("auth/me", {language: requested}, session, "PATCH");
          write.saved = requested;
        } catch {
          if (token === session && write.desired === requested) {
            applyLanguage(write.saved);
            notify("Не удалось сохранить язык.");
            return;
          }
        }
      }
    } finally {
      if (languageWrite === write) languageWrite = undefined;
    }
  };
}

function openSettings(tabId) {
  projectsDirField.value = defaultProjectsDir;
  renderTelegramLogs();
  showModal("settings-modal");
  const tab = byId(tabId);
  selectSettingsTab(tab);
  if (tabId === "integrations-tab") tab.focus();
}

function openAccountCredentials() {
  byId("account-credentials-form").reset();
  byId("account-old-password-field").hidden = !accountHasPassword;
  byId("account-forgot").hidden = !accountHasPassword;
  byId("account-credentials-error").textContent = "";
  showModal("account-credentials-modal");
}

byId("account-credentials-button").onclick = () => openAccountCredentials();
byId("account-credentials-form").onsubmit = async (event) => {
  event.preventDefault();
  const save = byId("account-credentials-save");
  const errorField = byId("account-credentials-error");
  const session = token;
  const revision = modalRevision;
  const username = byId("account-username");
  const body = {};
  if (username.value !== username.defaultValue) body.username = username.value;
  const password = byId("account-password").value;
  if (password) {
    body.password = password;
    if (accountHasPassword) body.old_password = byId("account-old-password").value;
  }
  save.disabled = true;
  errorField.textContent = "";
  try {
    if (password) validatePassword(password);
    const user = await api("auth/credentials", body, session, "PATCH");
    if (token !== session) return;
    renderAccountName(user);
    if (modalRevision === revision) showModal();
    notify("Данные для входа сохранены.");
  } catch (error) {
    if (token === session && modalRevision === revision) errorField.textContent = error.message;
  } finally {
    save.disabled = false;
  }
};

byId("settings-button").onclick = () => openSettings("general-tab");
for (const provider of ["telegram", "github", "email"]) {
  byId(provider + "-alert").onclick = () => openSettings("integrations-tab");
}

byId("projects-dir-form").onsubmit = async (event) => {
  event.preventDefault();
  const session = token;
  const button = event.submitter;
  button.disabled = true;
  try {
    const user = await api("auth/me", {default_projects_dir: projectsDirField.value}, session, "PATCH");
    if (session !== token) return;
    defaultProjectsDir = user.default_projects_dir;
    projectsDirField.value = defaultProjectsDir;
    fillDefaultProjectPath();
    notify("Общая папка сохранена");
  } catch (error) {
    if (session === token) notify(error.message);
  } finally {
    button.disabled = false;
  }
};

byId("project-settings-button").onclick = () => {
  if (!project) return;
  locationForm.reset();
  byId("settings-workspace").value = project?.workspace_path ?? "";
  byId("settings-git").value = project?.git_repo_url ?? "";
  renderProjectSettings(project);
  renderGitHubFields();
  showModal("project-settings-modal");
  refreshIntegrations();
};

byId("settings-delete-project").onclick = () => {
  if (project) confirmContextDeletion({kind: "project", item: project, path: "projects/" + project.id});
};

function renderIntegrations(user) {
  const verified = Boolean(user.email_verified && user.email);
  byId("email-alert").hidden = verified;
  const emailStatus = byId("email-status");
  emailStatus.hidden = !verified;
  emailStatus.textContent = verified ? user.email : "";
  byId("email-link-form").hidden = verified || Boolean(emailLinkKey);
  byId("email-code-form").hidden = verified || !emailLinkKey;
  telegramId = user.telegram_id;
  githubUsername = user.github_username;
  for (const [provider, account, avatarUrl] of [
    ["telegram", telegramId && (user.telegram_username || String(telegramId)), user.telegram_avatar_url],
    ["github", githubUsername && "@" + githubUsername, user.github_avatar_url],
  ]) {
    const linked = Boolean(account);
    byId(provider + "-alert").hidden = linked;
    byId(provider + "-card").classList.toggle("linked", linked);
    const status = byId(provider + "-status");
    const displayName = linked ? account : "Не привязан";
    if (status.textContent !== displayName) status.textContent = displayName;
    renderIntegrationAvatar(provider, account, avatarUrl);
    byId(provider + "-connect").hidden = linked;
    byId(provider + "-disconnect").hidden = !linked;
  }
  if (telegramId) showTelegramCode();
  if (githubUsername) stopGitHubDevice();
  renderGitHubFields();
}

function renderIntegrationAvatar(provider, account, url) {
  const avatar = byId(provider + "-avatar");
  const name = account || "";
  const source = name && url ? url : "";
  avatar.hidden = !name;
  if (avatar.dataset.name === name && avatar.dataset.source === source) return;
  avatar.dataset.name = name;
  avatar.dataset.source = source;
  avatar.textContent = [...name.replace(/^@/, "")].slice(0, 2).join("").toUpperCase();
  if (source) {
    const image = element("img");
    image.alt = "";
    image.referrerPolicy = "no-referrer";
    image.onload = () => {
      if (avatar.dataset.name === name && avatar.dataset.source === source) avatar.replaceChildren(image);
    };
    image.src = source;
  }
}

function renderGitHubFields() {
  const linked = Boolean(githubUsername);
  for (const prefix of ["project", "settings"]) {
    const checkbox = byId(prefix + "-new-repo");
    checkbox.disabled = !linked;
    if (checkbox.disabled) checkbox.checked = false;
    byId(prefix + "-git").readOnly = !linked;
    byId(prefix + "-git").disabled = checkbox.checked;
    byId(prefix + "-visibility-field").classList.toggle("hidden", !checkbox.checked);
  }
}

async function refreshIntegrations(background = false) {
  if (integrationUpdatePending) return;
  const session = token;
  const revision = modalRevision;
  const credentialsRevision = integrationsRevision;
  try {
    const user = await api("auth/me", undefined, session);
    if (session === token && revision === modalRevision && credentialsRevision === integrationsRevision) {
      renderIntegrations(user);
    }
  } catch (error) {
    if (!background && session === token && revision === modalRevision) {
      clearInterval(integrationsTimer);
      notify(error.message);
    }
  }
}

byId("project-new-repo").onchange = byId("settings-new-repo").onchange = renderGitHubFields;
for (const prefix of ["project", "settings"]) {
  const field = byId(prefix + "-git");
  const warn = (event) => showCursorToast("Привяжите GitHub в настройках", event);
  field.onclick = (event) => {
    if (field.readOnly) warn(event);
  };
  field.onkeydown = (event) => {
    if (!field.readOnly || !(event.key.length === 1 || ["Enter", "Backspace", "Delete"].includes(event.key))) return;
    if ((event.ctrlKey || event.metaKey) && ["a", "c"].includes(event.key.toLowerCase())) return;
    event.preventDefault();
    warn(event);
  };
  for (const type of ["beforeinput", "paste", "drop"]) {
    field.addEventListener(type, (event) => {
      if (field.readOnly) event.preventDefault();
    });
  }
}

function showTelegramCode(result) {
  clearTimeout(telegramCodeTimer);
  telegramLinkAttempt = result;
  setConfirmationCode("telegram-link-code", result ? "/link " + result.code : "");
  byId("telegram-instructions").hidden = !result;
  if (result) {
    byId("telegram-link").href = result.deep_link;
    telegramCodeTimer = setTimeout(showTelegramCode, result.expires_in * 1000);
  } else {
    byId("telegram-link").removeAttribute("href");
  }
}

function stopGitHubDevice() {
  clearTimeout(githubDeviceTimer);
  githubDeviceFlow = undefined;
  byId("github-device-instructions").hidden = true;
  setConfirmationCode("github-device-code", "");
}

byId("telegram-connect").onclick = (event) => {
  const button = event.currentTarget;
  updateIntegration(button, async (session) => {
    const result = await api("auth/generate-link-code", undefined, session, "POST");
    if (session !== token) return;
    showTelegramCode(result);
    const check = async () => {
      if (session !== token || telegramLinkAttempt !== result) return;
      await refreshIntegrations(true);
      if (telegramLinkAttempt === result) setTimeout(check, 3000);
    };
    setTimeout(check, 3000);
  }, event);
};

byId("github-connect").onclick = (event) => {
  if (githubDeviceFlow) return;
  updateIntegration(event.currentTarget, async (session) => {
    const flow = await api("auth/github/device/start", {}, session);
    if (session !== token) return;
    githubDeviceFlow = flow;
    setConfirmationCode("github-device-code", flow.user_code);
    byId("github-device-instructions").hidden = false;
    const poll = async () => {
      if (session !== token || githubDeviceFlow !== flow) return;
      try {
        const result = await api("auth/github/device/poll", {
          device_code: flow.device_code, signature: flow.signature, expires_at: flow.expires_at,
        }, session);
        if (session !== token || githubDeviceFlow !== flow) return;
        if (result.status === "authorized") {
          integrationsRevision++;
          renderIntegrations(result.user);
        } else if (result.status === "authorization_pending" || result.status === "slow_down") {
          if (result.status === "slow_down") flow.interval = Math.max(flow.interval + 5, result.interval || 0);
          githubDeviceTimer = setTimeout(poll, flow.interval * 1000);
        } else {
          throw new Error(result.status === "access_denied" ? "Вход через GitHub отклонён." : "Код GitHub истёк. Начните вход заново.");
        }
      } catch (error) {
        if (session !== token || githubDeviceFlow !== flow) return;
        stopGitHubDevice();
        notify(error.message);
      }
    };
    githubDeviceTimer = setTimeout(poll, flow.interval * 1000);
  }, event);
};

const unlinkDialog = byId("unlink-dialog");
for (const [provider, title] of [["telegram", "Telegram"], ["github", "GitHub"]]) {
  byId(provider + "-disconnect").onclick = () => {
    unlinkProvider = provider;
    byId("unlink-title").textContent = "Отвязать " + title;
    unlinkDialog.showModal();
  };
}
byId("unlink-cancel").onclick = () => unlinkDialog.close();
byId("unlink-form").onsubmit = (event) => {
  event.preventDefault();
  unlinkDialog.close();
  const provider = unlinkProvider;
  updateIntegration(byId(provider + "-disconnect"), async (session) => {
    const user = provider === "telegram"
      ? await api("auth/telegram/unlink", undefined, session, "POST")
      : await api("auth/github", undefined, session, "DELETE");
    if (session === token) renderIntegrations(user);
  }, event);
};

async function updateIntegration(button, action, event) {
  if (integrationUpdatePending) return;
  const session = token;
  const revision = modalRevision;
  integrationUpdatePending = true;
  integrationsRevision++;
  button.disabled = true;
  try {
    await action(session);
  } catch (error) {
    if (session === token && revision === modalRevision) showCursorToast(error.message, event);
  } finally {
    integrationUpdatePending = false;
    button.disabled = false;
  }
}

const projectSettings = {
  autonomy_mode: ["autonomy-mode", "autonomous"],
  failover_mode: ["failover-mode", "auto"],
  verbosity_level: ["verbosity-mode", "concise"],
  deadline_minutes: ["deadline", 10],
  commit_mode: ["commit-mode", "sequential"],
  auto_push_mode: ["auto-push-mode", "after_each"],
};
const projectPreferenceKeys = {
  autonomy_mode: "work_mode",
  verbosity_level: "report_verbosity",
};

function rememberProjectSettings(settings) {
  const defaultProjectPrefs = {...desktopPreferences.defaultProjectPrefs};
  for (const [key, value] of Object.entries(settings)) {
    const [id] = projectSettings[key];
    if (byId("project-" + id).validity.valid) {
      defaultProjectPrefs[projectPreferenceKeys[key] ?? key] = value;
    }
  }
  return saveDesktopPreferences({defaultProjectPrefs});
}

function renderProjectSettings(item) {
  for (const [key, [id, fallback]] of Object.entries(projectSettings)) {
    byId("settings-" + id).value = item[key] ?? fallback;
  }
}

function readProjectSettings(prefix) {
  return Object.fromEntries(Object.entries(projectSettings).map(([key, [id]]) => {
    const field = byId(prefix + "-" + id);
    return [key, field.type === "number" ? field.valueAsNumber : field.value];
  }));
}

function projectLocation(prefix) {
  const path = byId(prefix + "-workspace").value;
  const creating = byId(prefix + "-new-repo").checked;
  return {
    workspace_path: path.trim() ? path : null,
    ...(githubUsername && {git_repo_url: creating ? null : byId(prefix + "-git").value.trim() || null}),
    ...(creating && {github_visibility: byId(prefix + "-visibility").value}),
  };
}

locationForm.onsubmit = async (event) => {
  event.preventDefault();
  if (!project) return;
  const target = project;
  const revision = modalRevision;
  const button = event.submitter;
  const updates = {...projectLocation("settings"), ...readProjectSettings("settings")};
  for (const key of Object.keys(updates)) {
    if (updates[key] === target[key]) delete updates[key];
  }
  if (!Object.keys(updates).length) return;
  button.disabled = true;
  try {
    const saved = await api("projects/" + target.id, updates, token, "PATCH");
    if (project !== target) return;
    target.workspace_path = saved.workspace_path;
    target.git_repo_url = saved.git_repo_url;
    target.github_repo_url = saved.github_repo_url;
    for (const key of Object.keys(projectSettings)) {
      target[key] = saved[key];
    }
    renderProjectTitle();
    if (revision === modalRevision) {
      locationForm.reset();
      byId("settings-workspace").value = saved.workspace_path;
      byId("settings-git").value = saved.git_repo_url ?? "";
      renderProjectSettings(saved);
      renderGitHubFields();
    }
  } catch (error) {
    if (project === target) notify(error.message);
  } finally {
    button.disabled = false;
  }
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

function matchesPreset({name, config_json: {model}}, query) {
  const term = query.trim().toLocaleLowerCase("ru");
  return [name, model].some((value) => value.toLocaleLowerCase("ru").includes(term));
}

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
      const label = [preset.name, preset.config_json.model,
        ...(preset.agent_role_type === "image_gen" ? ["IMG"] : [])].join(" · ");
      const chip = choiceChip(label, preselected.includes(preset.id), sync);
      chip.firstChild.value = preset.id;
      if (importing) {
        chip.dataset.name = preset.name;
        chip.dataset.model = preset.config_json.model;
      }
      return chip;
    }));
    if (importing) byId("agent-preset-search").oninput();
    if (!available.length) list.textContent = presets.length ? "Все модели уже подключены." : "В хабе пока нет моделей.";
    sync();
  } catch (error) {
    if (revision === modalRevision) list.textContent = error.message;
  }
}
byId("agent-preset-search").oninput = ({target} = {target: byId("agent-preset-search")}) => {
  for (const row of byId("agent-presets").children) {
    const {name, model} = row.dataset;
    row.classList.toggle("hidden", !matchesPreset({name, config_json: {model}}, target.value));
  }
};
function fillDefaultProjectPath() {
  if (customProjectPath) return;
  const name = byId("project-name").value.trim();
  let folder = Array.from(name.replace(/[<>:"/\\|?*\x00-\x1f]+/g, "-").replace(/^[ .]+|[ .]+$/g, ""))
    .slice(0, 120).join("").replace(/[ .]+$/g, "") || "project";
  if (/^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])$/i.test(folder.split(".")[0])) folder = "_" + folder;
  const separator = defaultProjectsDir.includes("\\") ? "\\" : "/";
  const root = defaultProjectsDir.endsWith(separator) ? defaultProjectsDir : defaultProjectsDir + separator;
  byId("project-workspace").value = name && defaultProjectsDir ? root + folder : "";
}

byId("project-name").oninput = fillDefaultProjectPath;
byId("project-workspace").oninput = ({target}) => {
  customProjectPath = Boolean(target.value.trim());
};
byId("project-workspace").onblur = fillDefaultProjectPath;

const projectTemplates = {
  web: ["Веб-приложение", "Цель приложения:\nПользователи и их сценарии:\nЭкраны и API:\nМодель данных:\nКритерии готовности:"],
  bot: ["Telegram-бот", "Назначение бота:\nКоманды и сценарии диалога:\nУведомления:\nХранение данных:\nКритерии готовности:"],
  script: ["Скрипт автоматизации", "Задача:\nИсточники и форматы данных:\nПравила обработки:\nФормат результата:\nРасписание запуска:"],
  repo: ["Доработка репозитория", "Репозиторий и ветка:\nТекущее поведение:\nНеобходимые изменения:\nОграничения:\nКак проверить результат:"],
};

function openProjectModal(template) {
  const [name, spec] = (template || ["", ""]).map(LoomisI18n.translate);
  byId("project-name").value = name;
  byId("project-template").value = spec;
  byId("project-template-field").classList.toggle("hidden", !template);
  for (const [key, [id, fallback]] of Object.entries(projectSettings)) {
    const field = byId("project-" + id);
    const saved = desktopPreferences.defaultProjectPrefs?.[projectPreferenceKeys[key] ?? key];
    field.value = saved ?? fallback;
    if (!field.value || !field.validity.valid) field.value = fallback;
  }
  selectProjectAddTab(false);
  fillDefaultProjectPath();
  renderGitHubFields();
  showModal("project-modal");
  refreshIntegrations();
  loadPresetChoices("project-presets");
}
byId("new-project-button").onclick = byId("welcome-create").onclick = () => openProjectModal();
all("[data-template]").forEach((button) => {
  button.onclick = () => openProjectModal(projectTemplates[button.dataset.template]);
});
for (const [id] of Object.values(projectSettings)) {
  byId("project-" + id).addEventListener("change", () => rememberProjectSettings(readProjectSettings("project")));
}
byId("project-form").onsubmit = async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const manual = byId("project-manual-tab").getAttribute("aria-pressed") === "true";
  const presetIds = manual ? [] : checkedPresets("project-presets");
  const revision = modalRevision;
  const session = token;
  const form = event.currentTarget;
  const spec = byId("project-template").value.trim();
  const settings = readProjectSettings("project");
  const saved = rememberProjectSettings(settings);
  const payload = {
    name: byId("project-name").value,
    ...settings,
    ...projectLocation("project"),
    ...(!customProjectPath && {workspace_path: null}),
  };
  button.disabled = true;
  try {
    await saved;
    const created = await api("projects", payload, session);
    let importError, specError;
    if (spec) {
      try {
        await api("projects/" + created.id + "/spec-items", {text: spec}, session);
      } catch (error) {
        specError = error;
      }
    }
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
      customProjectPath = false;
      showModal();
    }
    await loadProjects(created.id);
    if (specError) {
      if (project?.id === created.id) byId("spec-text").value = spec;
      notify("Проект создан. Не удалось сохранить шаблон ТЗ: " + specError.message);
    }
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

const hubRole = byId("hub-role");

function renderHubFields() {
  syncRoleProvider(hubRole, hubProvider);
  const isApi = !cliProviders.has(hubProvider.value.toLowerCase());
  byId("hub-api-fields").classList.toggle("hidden", !isApi);
  const fields = hubForm.elements;
  fields.namedItem("base_url").disabled = fields.namedItem("api_key").disabled = !isApi;
  fields.namedItem("base_url").required = isApi;
  fields.namedItem("api_key").required = isApi && !editingPreset?.config_json.has_api_key;
}
hubProvider.onchange = hubRole.onchange = renderHubFields;

let editingPreset;
let hubPresets = [];
let hubRatings = new Map();
let hubRequest = 0;
const hubSearch = byId("hub-search");
const hubSort = byId("hub-sort");
const hubStatus = byId("hub-status");
const hubCollator = new Intl.Collator("ru");
const hubDateFormat = new Intl.DateTimeFormat("ru", {dateStyle: "short", timeStyle: "short"});
const hubComparators = {
  tokens: (a, b) => b.total_tokens_used - a.total_tokens_used,
  name: (a, b) => hubCollator.compare(a.name, b.name),
  date: (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || b.id - a.id,
};

function openHubForm(preset) {
  editingPreset = preset;
  const config = preset?.config_json;
  hubForm.reset();
  const type = preset?.adapter_type || "api";
  hubProvider.querySelector("[data-custom]")?.remove();
  if (![...hubProvider.options].some((option) => option.value === type)) {
    const option = new Option(type, type);
    option.dataset.custom = "";
    hubProvider.add(option);
  }
  hubProvider.value = type;
  hubRole.value = preset?.agent_role_type || "conversational";
  const fields = hubForm.elements;
  fields.namedItem("name").value = preset?.name || "";
  fields.namedItem("model").value = config?.model || "";
  fields.namedItem("base_url").value = config?.base_url || "";
  fields.namedItem("api_key").placeholder = config?.has_api_key ? "Ключ сохранён" : "";
  byId("hub-modal-title").textContent = preset ? "Настройки модели" : "Добавить модель в Хаб";
  renderHubFields();
  showModal("hub-modal");
}

function hubModelButton(label, paths, danger = false) {
  const button = element("button", danger ? "icon-button danger" : "icon-button");
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.innerHTML = icon(paths);
  return button;
}

function renderHubModels() {
  const matches = hubPresets.filter((preset) => matchesPreset(preset, hubSearch.value));
  matches.sort(hubComparators[hubSort.value]);
  hubStatus.textContent = matches.length ? "" : hubPresets.length ? "Модели не найдены." : "В Хабе пока нет моделей.";
  byId("hub-models").replaceChildren(...matches.map((preset) => {
    const card = element("li", "hub-model glass-panel");
    const actions = element("div", "hub-model-actions");
    const edit = hubModelButton("Редактировать " + preset.name, '<path d="m14 6 4 4M5 19l4-1 11-11-4-4L5 14z"/>');
    const remove = hubModelButton("Удалить " + preset.name,
      '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7l1-3h4l1 3"/>', true);
    edit.onclick = () => openHubForm(preset);
    remove.onclick = async () => {
      if (!confirm(LoomisI18n.translate("Удалить модель «" + preset.name + "» из Хаба?"))) return;
      remove.disabled = true;
      try {
        await api("hub/models/" + preset.id, undefined, token, "DELETE");
        await loadHubModels();
      } catch (error) {
        notify(error.message);
      } finally {
        remove.disabled = false;
      }
    };
    const createdAt = Date.parse(preset.created_at);
    if (Number.isFinite(createdAt)) {
      const added = element("time", "hub-model-added", "Добавлено: " + hubDateFormat.format(createdAt));
      added.dateTime = preset.created_at;
      actions.append(added);
    }
    actions.append(edit, remove);
    const provider = [...hubProvider.options].find((option) => option.value === preset.adapter_type)?.text || preset.adapter_type;
    const usage = element("div", "hub-usage");
    const rating = hubRatings.get(preset.model_key) ?? 100;
    const badge = element("span", "hub-rating", Math.round(rating) + "%");
    badge.title = hubRatings.has(preset.model_key) ? "Надёжность модели" : "Начальный рейтинг модели";
    usage.append(element("strong", "", statsNumber.format(preset.total_tokens_used)),
      element("small", "", "Токенов за всё время"));
    const heading = element("h3", "", preset.name);
    heading.dataset.userContent = "";
    if (preset.agent_role_type === "image_gen") heading.append(imageBadge());
    heading.append(badge);
    card.append(heading,
      element("small", "hub-model-detail", provider + " / " + preset.config_json.model), usage, actions);
    return card;
  }));
}

async function loadHubModels() {
  const revision = ++hubRequest;
  const session = token;
  hubStatus.textContent = "Загрузка моделей…";
  try {
    const [presets, leaderboard] = await Promise.all([
      api("hub/models"), api("hub/leaderboard"),
    ]);
    if (revision !== hubRequest || session !== token) return;
    hubPresets = presets;
    byId("welcome-models").textContent = presets.length;
    hubRatings = new Map(leaderboard.map((entry) => [entry.model_key, entry.reliability_score]));
    renderHubModels();
  } catch (error) {
    if (revision === hubRequest && session === token) hubStatus.textContent = error.message;
  }
}

function showHubPage(open) {
  const projectPage = byId("project-page");
  const hubPage = byId("hub-page");
  const modeChanged = open === hubPage.classList.contains("hidden");
  projectPage.classList.toggle("hidden", open);
  hubPage.classList.toggle("hidden", !open);
  if (modeChanged) playPageTransition("hub-projects-morph", 200, open ? hubPage : projectPage);
  const button = byId("hub-button");
  button.classList.toggle("shows-projects", open);
  button.title = open ? "Проекты" : "Хаб нейросетей";
  button.setAttribute("aria-label", button.title);
}

hubSearch.oninput = hubSort.onchange = renderHubModels;
byId("hub-button").onclick = () => {
  const open = byId("hub-page").classList.contains("hidden");
  const sidebar = document.querySelector(".sidebar");
  const mobile = sidebar.classList.contains("open");
  sidebar.classList.remove("open");
  showHubPage(open);
  if (open) loadHubModels();
  if (mobile) byId(open ? "hub-mobile-menu" : "mobile-menu").focus();
};
byId("hub-add").onclick = () => openHubForm();
hubForm.onsubmit = async (event) => {
  event.preventDefault();
  const data = new FormData(hubForm);
  const button = event.submitter;
  const type = data.get("provider");
  const provider = type.toLowerCase();
  const config = {model: data.get("model").trim()};
  if (cliProviders.has(provider)) {
    const command = cliCommands[provider];
    if (command && (editingPreset?.adapter_type !== type || (!editingPreset.config_json.command_template && provider !== "codex"))) {
      config.command_template = command;
    }
  } else {
    config.base_url = data.get("base_url").trim();
    const key = data.get("api_key").trim();
    if (key) config.api_key = key;
  }
  const revision = modalRevision;
  const session = token;
  const preset = editingPreset;
  button.disabled = true;
  try {
    await api(preset ? "hub/models/" + preset.id : "hub/models", {
      name: data.get("name").trim(),
      adapter_type: type,
      agent_role_type: data.get("agent_role_type"),
      config_json: config,
    }, session, preset ? "PATCH" : "POST");
    if (revision === modalRevision) showModal();
    if (session === token) await loadHubModels();
  } catch (error) {
    if (session === token) notify(error.message);
  } finally {
    button.disabled = false;
  }
};

async function loadReviewFiles(id, current) {
  const list = byId("project-review-files");
  list.replaceChildren(element("li", "", "Загрузка файлов…"));
  try {
    const paths = await api(`projects/${id}/review-files`);
    if (current !== selection || project.status !== "review_pending") return;
    list.replaceChildren(...paths.map((path) => {
      const item = element("li", "", path);
      item.dataset.userContent = "";
      return item;
    }));
    if (!paths.length) list.append(element("li", "", "Готовых файлов пока нет."));
  } catch (error) {
    if (current === selection && project.status === "review_pending") {
      list.replaceChildren(element("li", "", error.message));
    }
  }
}

function openProjectFolder() {
  return api(`projects/${project.id}/open-folder`, undefined, token, "POST").catch((error) => notify(error.message));
}

function statsFormat(value) {
  return value == null ? "—" : new Intl.NumberFormat(document.documentElement.lang).format(value);
}

function durationFormat(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  const total = Math.round(seconds);
  const parts = [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60];
  return parts.map((part) => String(part).padStart(2, "0")).join(":");
}

async function completionStats(id) {
  const [stats, commits] = await Promise.all([
    api(`projects/${id}/stats`),
    api(`projects/${id}/commits`).catch(() => null),
  ]);
  return {stats, commits};
}

function renderCompletionTeam(target, {agents, completed_steps}) {
  const names = agents.map(({name}) => name);
  const visibleNames = names.slice(0, 3).join(", ");
  const others = names.length > 3 ? ` +${names.length - 3}` : "";
  const roster = element("span", "completion-team-roster", visibleNames + others);
  roster.dataset.userContent = "";
  roster.title = names.join(", ");
  target.replaceChildren(
    element("span", "", "Агентов"), ` ${statsFormat(agents.length)} / `,
    element("span", "", "Шагов"), ` ${statsFormat(completed_steps)}`,
    roster,
  );
}

const completionLighterPrefix = "loomis_lighter_closed_";

function closedCompletionLighterIds() {
  const saved = desktopPreferences.closedCompletionLighters;
  if (Array.isArray(saved)) return saved.map(String);
  return Object.keys(localStorage)
    .filter(key => key.startsWith(completionLighterPrefix) && localStorage.getItem(key) === "1")
    .map(key => key.slice(completionLighterPrefix.length));
}

function rememberCompletionLighter(id, closed) {
  const ids = new Set(closedCompletionLighterIds());
  const key = completionLighterPrefix + id;
  if (closed) {
    ids.add(id);
    localStorage.setItem(key, "1");
  } else {
    ids.delete(id);
    localStorage.removeItem(key);
  }
  return saveDesktopPreferences({closedCompletionLighters: [...ids]});
}

function syncCompletionScreen() {
  const scene = byId("completion-scene");
  const active = project?.status === "completed";
  const id = active ? String(project.id) : "";
  const page = byId("project-page");
  const changed = scene.dataset.project !== id;
  scene.dataset.project = id;
  const showChat = active && scene.dataset.chat === id;
  const showScene = active && !showChat;
  byId("back-to-completion").hidden = !showChat;
  scene.inert = !showScene;
  page.classList.toggle("is-completed", showScene);
  for (const panel of page.children) {
    if (panel !== scene) panel.inert = showScene;
  }
  if (active) byId("completion-name").textContent = project.name;
  if (!changed) return;
  delete scene.dataset.chat;
  scene.classList.remove("completion-lit", "completion-out");
  const closed = active && closedCompletionLighterIds().includes(id);
  scene.classList.toggle("completion-closed", closed);
  const actions = byId("completion-actions");
  actions.inert = !closed;
  byId("completion-extinguish").disabled = closed;
  if (!active || closed) return;
  const current = selection;
  for (const metric of ["time", "commits", "tokens", "team"]) byId("completion-" + metric).textContent = "—";
  completionStats(project.id).then(({stats, commits}) => {
    if (current !== selection || scene.dataset.project !== id) return;
    byId("completion-time").textContent = durationFormat(stats.duration_seconds);
    byId("completion-commits").textContent = `${statsFormat(commits?.length)} / ${statsFormat(stats.completed_steps)}`;
    byId("completion-tokens").textContent = statsFormat(stats.total_tokens);
    renderCompletionTeam(byId("completion-team"), stats);
  }).catch((error) => {
    if (scene.dataset.project === id) notify(error.message);
  });
}

function confirmCompletionAction(reopen) {
  const id = project.id;
  const button = byId("completion-confirm-button");
  byId("completion-confirm-title").textContent = reopen ? "Вернуться к проекту?" : "Завершить проект?";
  byId("completion-confirm-description").textContent = reopen
    ? "Вернуться на этап перед завершением и продолжить разработку?"
    : "После подтверждения откроется экран итогов, но при необходимости вы сможете вернуться к доработке.";
  button.textContent = reopen ? "Вернуться к проекту" : "Подтвердить завершение";
  button.disabled = false;
  showModal("completion-confirm-modal");
  const revision = modalRevision;
  const current = selection;
  const statusVersion = statusRevision;
  button.onclick = async () => {
    button.disabled = true;
    try {
      const {status} = await api(`projects/${id}/actions`, {action: reopen ? "reopen_project" : "confirm_completion"});
      if (reopen) await rememberCompletionLighter(String(id), false);
      if (revision === modalRevision) showModal();
      if (current === selection && statusVersion === statusRevision) setStatus(status);
      if (reopen && current === selection && project.status === "review_pending") {
        selectView("team");
        byId("message-input").focus();
      }
    } catch (error) {
      if (revision === modalRevision) {
        button.disabled = false;
        notify(error.message);
      }
    }
  };
}

byId("review-folder").onclick = openProjectFolder;
byId("review-continue").onclick = () => byId("message-input").focus();
byId("review-complete").onclick = () => confirmCompletionAction(false);
byId("completion-scene").addEventListener("lighterclosed", (event) => {
  rememberCompletionLighter(event.currentTarget.dataset.project, true);
});
byId("completion-home").onclick = openDashboard;
byId("completion-stats").onclick = () => byId("stats-button").onclick();
byId("completion-return").onclick = () => confirmCompletionAction(true);
byId("completion-chat")?.addEventListener("click", () => {
  const scene = byId("completion-scene");
  scene.dataset.chat = scene.dataset.project;
  syncCompletionScreen();
  selectView("team");
  byId("message-input").focus();
});
byId("back-to-completion").onclick = () => {
  delete byId("completion-scene").dataset.chat;
  syncCompletionScreen();
  byId("completion-chat")?.focus();
};

const statsNumber = new Intl.NumberFormat("ru");
byId("stats-button").onclick = async () => {
  if (!project) return;
  showModal("stats-modal");
  const revision = modalRevision;
  const body = byId("stats-body");
  body.textContent = "Загрузка статистики…";
  try {
    const selected = project;
    const current = selection;
    const completed = selected.status === "completed";
    const {stats, commits} = completed ? await completionStats(selected.id)
      : {stats: await api(`projects/${selected.id}/stats`)};
    if (revision !== modalRevision || current !== selection) return;
    const total = element("p", "stats-total");
    total.append(element("strong", "", statsNumber.format(stats.total_tokens)), " токенов по проекту");
    body.replaceChildren(total);
    if (completed) {
      total.replaceChildren(element("strong", "", statsFormat(stats.total_tokens)), " ", element("span", "", "Токены"));
      const summary = element("dl", "completion-stats-summary");
      for (const [label, value] of [
        ["Было потрачено времени", durationFormat(stats.duration_seconds)],
        ["Выполнено коммитов / шагов", `${statsFormat(commits?.length)} / ${statsFormat(stats.completed_steps)}`],
      ]) summary.append(element("dt", "", label), element("dd", "", value));
      const team = element("dd");
      renderCompletionTeam(team, stats);
      summary.append(element("dt", "", "Команда проекта"), team);
      const links = element("div", "review-actions");
      const folder = element("button", "button secondary", "Открыть в Проводнике");
      folder.type = "button";
      folder.onclick = openProjectFolder;
      folder.title = selected.workspace_path || "";
      links.append(folder);
      const repository = selected.git_repo_url && URL.canParse(selected.git_repo_url)
        ? new URL(selected.git_repo_url) : null;
      if (repository?.protocol === "https:" && repository.hostname === "github.com") {
        const github = element("a", "button secondary", "GitHub");
        github.href = repository.href;
        github.target = "_blank";
        github.rel = "noopener noreferrer";
        links.append(github);
      }
      if (selected.auto_push_mode === "manual_finish" && selected.git_repo_url) {
        const push = element("button", "button secondary", "Push в Git");
        push.type = "button";
        push.onclick = async () => {
          push.disabled = true;
          try {
            if (current === selection) await byId("git-push").onclick();
          } finally {
            push.disabled = false;
          }
        };
        links.append(push);
      }
      body.append(summary, links);
      if (commits?.length) {
        const history = element("details", "completion-stats-history");
        const list = element("ol");
        for (const commit of commits) {
          const item = element("li");
          item.dataset.userContent = "";
          item.append(element("code", "", commit.short_hash), " ", commit.message);
          list.append(item);
        }
        history.append(element("summary", "", "История коммитов"), list);
        body.append(history);
      }
    }
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
      const name = element("th", "", completed && agent.model ? `${agent.name} · ${agent.model}` : agent.name);
      name.dataset.userContent = "";
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
  if (token) showModal("logout-modal");
};

byId("logout-confirm").onclick = async () => {
  if (!token) return;
  showModal();
  token = null;
  applyLanguage("ru");
  githubUsername = null;
  defaultProjectsDir = "";
  telegramId = null;
  showTelegramCode();
  stopGitHubDevice();
  hubPresets = [];
  hubRatings.clear();
  hubSearch.value = "";
  renderHubModels();
  showHubPage(false);
  localStorage.removeItem(sessionKey);
  const cleared = writeDesktopSession(null);
  stopProviderAuth();
  selectProject(undefined);
  for (const metric of ["models", "projects", "tokens", "completed"]) byId("welcome-" + metric).textContent = "—";
  delete byId("welcome-dashboard").dataset.projectCount;
  showWorkspace(false);
  await cleared;
};

async function restoreSession() {
  try {
    const bridge = await desktopSessionAPI;
    try {
      const saved = await bridge?.load_preferences();
      desktopPreferences = {...saved, ...desktopPreferences};
      const {theme, direct_width, sidebar_collapsed, homeLighterOpen, closedCompletionLighters} = desktopPreferences;
      if (theme === "dark" || theme === "light") setTheme(theme);
      if (Number.isFinite(direct_width)) setDirectWidth(direct_width);
      if (typeof sidebar_collapsed === "boolean") setSidebarRail(sidebar_collapsed);
      if (typeof homeLighterOpen === "boolean") {
        localStorage.setItem(homeLighterKey, String(homeLighterOpen));
        homeLighterToggle.setAttribute("aria-expanded", String(homeLighterOpen));
        homeLighterToggle.dispatchEvent(new CustomEvent("lighterrestore", {detail: homeLighterOpen}));
      } else if (localStorage.getItem(homeLighterKey) !== null && bridge) {
        saveDesktopPreferences({homeLighterOpen: localStorage.getItem(homeLighterKey) === "true"});
      }
      if (!Array.isArray(closedCompletionLighters) && bridge) {
        const ids = closedCompletionLighterIds();
        if (ids.length) saveDesktopPreferences({closedCompletionLighters: ids});
      }
    } catch (error) {
      notify(error.message);
    }
    const savedToken = localStorage.getItem(sessionKey) || await bridge?.load_session();
    if (savedToken) await signIn(savedToken);
  } catch (error) {
    if (error.status === 401) {
      localStorage.removeItem(sessionKey);
      await writeDesktopSession(null);
    } else {
      notify(error.message);
    }
  } finally {
    byId("auth-screen").dataset.sessionChecked = "";
  }
}
const projectContextMenu = byId("project-context-menu");
const projectGitHubAction = projectContextMenu.querySelector('[data-project-action="github"]');
let projectMenuTarget, projectMenuTrigger, projectMenuRepo;

function openProjectContextMenu(event, item) {
  event.preventDefault();
  closeSpecItemMenu();
  closeContextMenu();
  projectMenuTrigger = event.currentTarget;
  projectMenuTarget = {kind: "project", item, path: "projects/" + item.id};
  const repo = item.github_repo_url || item.git_repo_url;
  const url = repo ? URL.parse(repo) : null;
  projectMenuRepo = url?.protocol === "https:" && url.hostname === "github.com" ? url.href : null;
  projectGitHubAction.disabled = !projectMenuRepo;
  positionContextMenu(projectContextMenu, event, projectMenuTrigger);
  projectContextMenu.inert = false;
  projectContextMenu.querySelector("button").focus({preventScroll: true});
}

function closeProjectContextMenu(restoreFocus = false) {
  projectContextMenu.inert = true;
  if (restoreFocus) projectMenuTrigger?.focus({preventScroll: true});
  projectMenuTarget = projectMenuTrigger = projectMenuRepo = null;
}

projectContextMenu.addEventListener("click", (event) => {
  const action = event.target.closest("[data-project-action]")?.dataset.projectAction;
  if (!action || !projectMenuTarget) return;
  const target = projectMenuTarget;
  const repo = projectMenuRepo;
  closeProjectContextMenu(true);
  if (action === "folder") {
    api(target.path + "/open-folder", undefined, token, "POST").catch((error) => notify(error.message));
  } else if (action === "github" && repo) {
    window.open(repo, "_blank", "noopener,noreferrer");
  } else if (action === "rename") {
    renameProject(target);
  } else if (action === "delete") {
    confirmContextDeletion(target);
  }
});

projectContextMenu.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    closeProjectContextMenu(true);
  }
});
projectContextMenu.addEventListener("focusout", (event) => {
  if (!projectContextMenu.contains(event.relatedTarget)) closeProjectContextMenu();
});
document.addEventListener("pointerdown", (event) => {
  if (!projectContextMenu.contains(event.target)) closeProjectContextMenu();
});
window.addEventListener("resize", () => closeProjectContextMenu());

restoreSession();
