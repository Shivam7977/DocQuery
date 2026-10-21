const API_BASE = "";
const GOOGLE_CLIENT_ID = "156200876984-2egnbobauansfgn11lj06vtobqruhe3l.apps.googleusercontent.com";

let mode = "login";
let pendingEmail = "";
let resetEmail = "";

const tabLogin = document.getElementById("tab-login");
const tabSignup = document.getElementById("tab-signup");
const authForm = document.getElementById("auth-form");
const authEmail = document.getElementById("auth-email");
const authPassword = document.getElementById("auth-password");
const authError = document.getElementById("auth-error");
const authSubmit = document.getElementById("auth-submit");

const stepForm = document.getElementById("auth-step-form");
const stepVerify = document.getElementById("auth-step-verify");
const verifyForm = document.getElementById("verify-form");
const verifyCode = document.getElementById("verify-code");
const verifyError = document.getElementById("verify-error");
const verifyEmailDisplay = document.getElementById("verify-email-display");

const guestBtn = document.getElementById("guest-btn");

// Forgot / reset password elements
const forgotLink = document.getElementById("forgot-link");
const stepForgot = document.getElementById("auth-step-forgot");
const forgotForm = document.getElementById("forgot-form");
const forgotEmail = document.getElementById("forgot-email");
const forgotError = document.getElementById("forgot-error");
const forgotBackLink = document.getElementById("forgot-back-link");

const stepReset = document.getElementById("auth-step-reset");
const resetForm = document.getElementById("reset-form");
const resetCode = document.getElementById("reset-code");
const resetNewPassword = document.getElementById("reset-new-password");
const resetError = document.getElementById("reset-error");
const resetEmailDisplay = document.getElementById("reset-email-display");
const resetBackLink = document.getElementById("reset-back-link");

tabLogin.addEventListener("click", () => setMode("login"));
tabSignup.addEventListener("click", () => setMode("signup"));

function setMode(newMode) {
  mode = newMode;
  tabLogin.classList.toggle("is-active", mode === "login");
  tabSignup.classList.toggle("is-active", mode === "signup");
  authSubmit.textContent = mode === "login" ? "Log in" : "Sign up";
  authPassword.autocomplete = mode === "login" ? "current-password" : "new-password";
  hideError(authError);
}

function showError(el, message) {
  el.textContent = message;
  el.hidden = false;
}
function hideError(el) {
  el.hidden = true;
}

// Shows exactly one of the four auth-card steps at a time, hides the rest.
function showStep(step) {
  stepForm.hidden = step !== "form";
  stepVerify.hidden = step !== "verify";
  stepForgot.hidden = step !== "forgot";
  stepReset.hidden = step !== "reset";
}

async function apiPost(path, body) {
  const res = await fetch(API_BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
    credentials: "include",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "Something went wrong.");
  return data;
}

function goToAppWithCookie() {
  localStorage.setItem("docquery_authenticated", "true");
  localStorage.removeItem("docquery_token");
  localStorage.removeItem("docquery_chat_session_id");
  localStorage.removeItem("docquery_guest_mode");
  window.location.href = "app.html";
}

authForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  hideError(authError);
  const email = authEmail.value.trim();
  const password = authPassword.value;

  try {
    if (mode === "login") {
      await apiPost("/auth/login", { email, password });
      goToAppWithCookie();
    } else {
      await apiPost("/auth/signup", { email, password });
      pendingEmail = email;
      verifyEmailDisplay.textContent = email;
      showStep("verify");
      verifyCode.focus();
    }
  } catch (err) {
    showError(authError, err.message);
  }
});

verifyForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  hideError(verifyError);
  try {
    await apiPost("/auth/verify-signup", {
      email: pendingEmail,
      code: verifyCode.value.trim(),
    });
    goToAppWithCookie();
  } catch (err) {
    showError(verifyError, err.message);
  }
});

guestBtn.addEventListener("click", async () => {
  // No backend call here — no chat_session_id (and therefore no DB row)
  // gets created until the guest actually uploads a document.
  await apiPost("/auth/logout").catch(() => {});
  localStorage.removeItem("docquery_authenticated");
  localStorage.removeItem("docquery_token");
  localStorage.removeItem("docquery_chat_session_id");
  localStorage.setItem("docquery_guest_mode", "true");
  window.location.href = "app.html";
});

// --- Forgot / reset password ---

forgotLink.addEventListener("click", () => {
  hideError(authError);
  forgotEmail.value = authEmail.value.trim();  // carry over what they'd already typed, if anything
  showStep("forgot");
  forgotEmail.focus();
});

forgotBackLink.addEventListener("click", () => showStep("form"));
resetBackLink.addEventListener("click", () => showStep("form"));

forgotForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  hideError(forgotError);
  const email = forgotEmail.value.trim();
  try {
    // Backend always returns the same generic message here whether or not
    // the account exists (anti-enumeration) — so this always advances to
    // the code-entry step regardless.
    await apiPost("/auth/forgot-password", { email });
    resetEmail = email;
    resetEmailDisplay.textContent = email;
    showStep("reset");
    resetCode.focus();
  } catch (err) {
    showError(forgotError, err.message);
  }
});

resetForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  hideError(resetError);
  try {
    await apiPost("/auth/reset-password", {
      email: resetEmail,
      code: resetCode.value.trim(),
      new_password: resetNewPassword.value,
    });
    goToAppWithCookie();
  } catch (err) {
    showError(resetError, err.message);
  }
});

window.onload = () => {
  if (!window.google) return;
  google.accounts.id.initialize({
    client_id: GOOGLE_CLIENT_ID,
    callback: handleGoogleCredential,
  });
  google.accounts.id.renderButton(document.getElementById("google-btn"), {
    theme: "outline",
    size: "large",
    width: 320,
  });
};

async function handleGoogleCredential(response) {
  try {
    await apiPost("/auth/google", { id_token: response.credential });
    goToAppWithCookie();
  } catch (err) {
    showError(authError, err.message);
  }
}
