const $ = (id) => document.getElementById(id);

// tabs
document.querySelectorAll(".auth-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".auth-tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    const isLogin = tab.dataset.tab === "login";
    $("loginForm").classList.toggle("hidden", !isLogin);
    $("signupForm").classList.toggle("hidden", isLogin);
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
  const { error } = await Auth.signUp($("signupEmail").value.trim(), $("signupPassword").value);
  if (error) { t.error(error.message); return setMsg(error.message, false); }
  t.success("Account created — check your email to confirm, then log in.", "Almost there");
  setMsg("Account created — check your email to confirm, then log in.", true);
});

// already signed in? go straight to the app
Auth.getSession().then(s => { if (s) window.location.href = "/"; });
