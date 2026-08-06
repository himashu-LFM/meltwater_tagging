const $ = (id) => document.getElementById(id);

// tabs
document.querySelectorAll(".auth-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".auth-tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    const isLogin = tab.dataset.tab === "login";
    $("loginForm").classList.toggle("hidden", !isLogin);
    $("signupForm").classList.toggle("hidden", isLogin);
    $("resetForm").classList.add("hidden");
    $("authMsg").textContent = "";
  });
});

function setMsg(text, ok) {
  const el = $("authMsg");
  el.textContent = text;
  el.className = "auth-msg " + (ok ? "ok" : "err");
}

$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const t = Toast.loading("Signing you in…");
  const { error } = await Auth.signIn($("loginEmail").value.trim(), $("loginPassword").value);
  if (error) { t.error(error.message); return setMsg(error.message, false); }
  t.success("Welcome back! Redirecting…");
  setTimeout(() => (window.location.href = "/"), 500);
});

$("signupForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const t = Toast.loading("Creating your account…");
  const email = $("signupEmail").value.trim();
  const { error } = await Auth.signUp(email, $("signupPassword").value);
  if (error) { t.error(error.message); return setMsg(error.message, false); }
  // Fire-and-forget welcome email (never blocks signup if it fails).
  fetch("/api/auth/welcome", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  }).catch(() => {});
  t.success("Account created — you can log in now.", "Welcome!");
  setMsg("Account created — you can log in now. A welcome email is on its way.", true);
});

// --- Forgot / reset password -----------------------------------------------
$("forgotLink").addEventListener("click", () => {
  $("loginForm").classList.add("hidden");
  $("signupForm").classList.add("hidden");
  $("resetForm").classList.remove("hidden");
  $("resetStep1").classList.remove("hidden");
  $("resetStep2").classList.add("hidden");
  $("resetEmail").value = $("loginEmail").value.trim();
  setMsg("", true);
});

$("backToLogin").addEventListener("click", () => {
  $("resetForm").classList.add("hidden");
  $("loginForm").classList.remove("hidden");
  setMsg("", true);
});

$("sendCodeBtn").addEventListener("click", async () => {
  const email = $("resetEmail").value.trim();
  if (!email || !email.includes("@")) return setMsg("Enter a valid email.", false);
  const t = Toast.loading("Checking your email…");
  try {
    const r = await fetch("/api/auth/forgot-password", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { t.error(d.error || "Could not send the code."); return setMsg(d.error || "Could not send the code.", false); }
    t.success("Code sent — check your email.");
    setMsg("", true);
    $("resetEmailEcho").textContent = email;
    $("resetStep1").classList.add("hidden");
    $("resetStep2").classList.remove("hidden");
  } catch (e) {
    t.error("Network error — try again.");
  }
});

$("resetBtn").addEventListener("click", async () => {
  const email = $("resetEmail").value.trim();
  const code = $("resetCode").value.trim();
  const new_password = $("resetNewPassword").value;
  if (!code) return setMsg("Enter the 6-digit code.", false);
  if (new_password.length < 6) return setMsg("Password must be at least 6 characters.", false);
  const t = Toast.loading("Resetting your password…");
  try {
    const r = await fetch("/api/auth/reset-password", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, code, new_password }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { t.error(d.error || "Could not reset password."); return setMsg(d.error || "Could not reset password.", false); }
    t.success("Password updated — you can log in now.");
    setMsg("Password updated — you can log in now.", true);
    // back to login, prefill email
    $("resetForm").classList.add("hidden");
    $("loginForm").classList.remove("hidden");
    $("loginEmail").value = email;
    $("loginPassword").value = "";
  } catch (e) {
    t.error("Network error — try again.");
  }
});

// already signed in? go straight to the app
Auth.getSession().then(s => { if (s) window.location.href = "/"; });
