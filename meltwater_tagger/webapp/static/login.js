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
  setMsg("Signing in…", true);
  const { error } = await Auth.signIn($("loginEmail").value.trim(), $("loginPassword").value);
  if (error) return setMsg(error.message, false);
  window.location.href = "/";
});

$("signupForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  setMsg("Creating your account…", true);
  const { error } = await Auth.signUp($("signupEmail").value.trim(), $("signupPassword").value);
  if (error) return setMsg(error.message, false);
  setMsg("Account created — check your email to confirm, then log in.", true);
});

// already signed in? go straight to the app
Auth.getSession().then(s => { if (s) window.location.href = "/"; });
