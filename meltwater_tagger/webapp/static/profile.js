const $ = (id) => document.getElementById(id);

// --- bookmarklet: one-click Meltwater session grabber, no DevTools needed ---
const BOOKMARKLET_SRC = `(function(){
  var k = Object.keys(localStorage).find(function(x){return x.indexOf('@@auth0spajs@@')===0;});
  if(!k){alert('Could not find your Meltwater session on this page. Make sure you are logged into app.meltwater.com in this tab, then click the bookmark again.');return;}
  var v = localStorage.getItem(k);
  function done(ok){
    alert((ok ? 'Copied to your clipboard! ' : 'Copy the value from the box that just opened (Ctrl+A, then Ctrl+C). ') +
      '(' + v.length + ' characters)\\n\\nNow go to the Sentiment Tagger Profile page, paste it into "Meltwater session", and click Save.');
  }
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(v).then(function(){done(true);}).catch(function(){prompt('Copy this value:', v);done(false);});
  } else {
    prompt('Copy this value:', v);
    done(false);
  }
})();`;

(function initBookmarklet() {
  const href = "javascript:" + encodeURIComponent(BOOKMARKLET_SRC);
  const link = $("bookmarkletLink");
  if (link) link.setAttribute("href", href);
  const codeBlock = $("bookmarkletCode");
  if (codeBlock) codeBlock.textContent = href;
})();

(async () => {
  const session = await Auth.requireAuthOrRedirect();
  if (!session) return;
  $("whoami").textContent = `Signed in as ${Auth.userEmail()}`;

  const mw = await (await Auth.authedFetch("/api/profile/meltwater")).json();
  if (mw.credentials) $("mwEmail").value = mw.credentials.meltwater_email || "";
  lockPasswordField(!!(mw.credentials && mw.credentials.meltwater_email));
  setMwLoginStatus(!!(mw.credentials && mw.credentials.meltwater_email), mw.credentials && mw.credentials.updated_at);

  const rs = await (await Auth.authedFetch("/api/profile/reddit")).json();
  if (rs.session) $("redditCookie").placeholder = "•••••••• (saved — paste a new value to replace)";

  // Session-injection card is currently disabled in the HTML — only touch its
  // elements if they exist (kept for if the card is ever re-enabled).
  if ($("mwSessionValue")) {
    const mws = await (await Auth.authedFetch("/api/profile/meltwater-session")).json();
    if (mws.session) $("mwSessionValue").placeholder = "•••••••• (saved — paste a new value to replace)";
    refreshMwSessionStatus();
  }

  refreshRedditStatus();
})();

function setMwLoginStatus(saved, updatedAt) {
  const pill = $("mwLoginStatus"), txt = $("mwLoginStatusText");
  if (!pill) return;
  if (saved) {
    pill.className = "status-pill active";
    txt.textContent = updatedAt
      ? `Saved — ${new Date(updatedAt).toLocaleDateString()}`
      : "Saved";
  } else {
    pill.className = "status-pill none";
    txt.textContent = "Not set — Apply to Meltwater won't work yet";
  }
}

async function refreshMwSessionStatus() {
  const pill = $("mwSessionStatus"), txt = $("mwSessionStatusText");
  if (!pill) return;
  pill.className = "status-pill checking";
  txt.textContent = "checking…";
  try {
    const data = await (await Auth.authedFetch("/api/profile/meltwater-session/status")).json();
    if (data.state === "active") {
      const hrs = Math.max(0, Math.round(data.seconds_remaining / 3600));
      pill.className = "status-pill active"; txt.textContent = `Active — ~${hrs}h left`;
    } else if (data.state === "expired") {
      pill.className = "status-pill expired"; txt.textContent = "Expired — please update";
    } else if (data.state === "unknown") {
      pill.className = "status-pill expired"; txt.textContent = "Could not read expiry";
    } else {
      pill.className = "status-pill none"; txt.textContent = "Not set";
    }
  } catch {
    pill.className = "status-pill none"; txt.textContent = "Unknown";
  }
}

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

// --- Meltwater password field: masked + locked once saved, "Edit" to change it ---
function lockPasswordField(hasSaved) {
  const field = $("mwPassword"), edit = $("mwPwEdit"), eye = $("mwPwToggle");
  if (hasSaved) {
    field.value = "••••••••";
    field.readOnly = true;
    field.type = "password";
    edit.classList.remove("hidden");
    eye.classList.add("hidden");
  } else {
    field.value = "";
    field.readOnly = false;
    field.type = "password";
    edit.classList.add("hidden");
    eye.classList.remove("hidden");
  }
}

$("mwPwEdit").addEventListener("click", () => {
  const field = $("mwPassword");
  field.value = "";
  field.readOnly = false;
  field.type = "password";
  field.focus();
  $("mwPwEdit").classList.add("hidden");
  $("mwPwToggle").classList.remove("hidden");
});

$("mwPwToggle").addEventListener("click", () => {
  const field = $("mwPassword");
  field.type = field.type === "password" ? "text" : "password";
  $("mwPwToggle").textContent = field.type === "password" ? "👁" : "🙈";
});

$("saveMw").addEventListener("click", async () => {
  const email = $("mwEmail").value.trim();
  const field = $("mwPassword");
  // A locked/untouched field still shows the masked placeholder dots — don't
  // send those as the real password, only send what was actually typed.
  const password = field.readOnly ? "" : field.value;
  const r = await Auth.authedFetch("/api/profile/meltwater", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const data = await r.json();
  if (r.ok) {
    Toast.success("Meltwater login saved.", "Credentials updated");
    lockPasswordField(!!email);
    setMwLoginStatus(!!email, new Date().toISOString());
  }
  else Toast.error(data.error || "Could not save your Meltwater login.");
});

if ($("saveMwSession")) $("saveMwSession").addEventListener("click", async () => {
  const storage_value = $("mwSessionValue").value.trim();
  if (!storage_value) return Toast.error("Paste the Local Storage value first.");
  const r = await Auth.authedFetch("/api/profile/meltwater-session", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ storage_value }),
  });
  const data = await r.json();
  if (r.ok) {
    Toast.success("Meltwater session saved — Apply to Meltwater will use it directly.", "Saved");
    $("mwSessionValue").value = "";
    refreshMwSessionStatus();
  } else Toast.error(data.error || "Could not save the Meltwater session.");
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
