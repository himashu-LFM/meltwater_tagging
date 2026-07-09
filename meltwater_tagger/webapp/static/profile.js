const $ = (id) => document.getElementById(id);

(async () => {
  const session = await Auth.requireAuthOrRedirect();
  if (!session) return;
  $("whoami").textContent = `Signed in as ${Auth.userEmail()}`;

  const mw = await (await Auth.authedFetch("/api/profile/meltwater")).json();
  if (mw.credentials) $("mwEmail").value = mw.credentials.meltwater_email || "";

  const rs = await (await Auth.authedFetch("/api/profile/reddit")).json();
  if (rs.session) $("redditCookie").placeholder = "•••••••• (saved — paste a new value to replace)";

  refreshRedditStatus();
})();

async function refreshRedditStatus() {
  const pill = $("redditStatus"), txt = $("redditStatusText");
  pill.className = "status-pill checking";
  txt.textContent = "checking…";
  try {
    const data = await (await Auth.authedFetch("/api/profile/reddit/status")).json();
    if (data.state === "active") { pill.className = "status-pill active"; txt.textContent = "Session active"; }
    else if (data.state === "expired") { pill.className = "status-pill expired"; txt.textContent = "Expired — please update"; }
    else { pill.className = "status-pill none"; txt.textContent = "Not set"; }
  } catch {
    pill.className = "status-pill none"; txt.textContent = "Unknown";
  }
}

$("saveMw").addEventListener("click", async () => {
  const email = $("mwEmail").value.trim();
  const password = $("mwPassword").value;
  const r = await Auth.authedFetch("/api/profile/meltwater", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const data = await r.json();
  if (r.ok) { Toast.success("Meltwater login saved.", "Credentials updated"); $("mwPassword").value = ""; }
  else Toast.error(data.error || "Could not save your Meltwater login.");
});

$("saveReddit").addEventListener("click", async () => {
  const cookie_value = $("redditCookie").value.trim();
  const r = await Auth.authedFetch("/api/profile/reddit", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cookie_value }),
  });
  const data = await r.json();
  if (r.ok) {
    Toast.success("Reddit session cookie saved.", "Saved");
    $("redditCookie").value = "";
    refreshRedditStatus();
  } else Toast.error(data.error || "Could not save the Reddit cookie.");
});

$("logoutLink").addEventListener("click", async (e) => {
  e.preventDefault();
  await Auth.signOut();
  window.location.href = "/login";
});
