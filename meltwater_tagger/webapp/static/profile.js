const $ = (id) => document.getElementById(id);

(async () => {
  const session = await Auth.requireAuthOrRedirect();
  if (!session) return;
  $("whoami").textContent = `Signed in as ${Auth.userEmail()}`;

  const mw = await (await Auth.authedFetch("/api/profile/meltwater")).json();
  if (mw.credentials) $("mwEmail").value = mw.credentials.meltwater_email || "";

  const rs = await (await Auth.authedFetch("/api/profile/reddit")).json();
  if (rs.session) $("redditCookie").placeholder = "•••••••• (saved — paste a new value to replace)";

  loadBrands();
})();

$("saveMw").addEventListener("click", async () => {
  const email = $("mwEmail").value.trim();
  const password = $("mwPassword").value;
  const r = await Auth.authedFetch("/api/profile/meltwater", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const data = await r.json();
  $("mwMsg").textContent = r.ok ? "✓ Saved" : (data.error || "Failed");
  $("mwMsg").style.color = r.ok ? "var(--pos)" : "var(--neg)";
  if (r.ok) $("mwPassword").value = "";
});

$("saveReddit").addEventListener("click", async () => {
  const cookie_value = $("redditCookie").value.trim();
  const r = await Auth.authedFetch("/api/profile/reddit", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cookie_value }),
  });
  const data = await r.json();
  $("redditMsg").textContent = r.ok ? "✓ Saved" : (data.error || "Failed");
  $("redditMsg").style.color = r.ok ? "var(--pos)" : "var(--neg)";
  if (r.ok) $("redditCookie").value = "";
});

$("saveBrand").addEventListener("click", async () => {
  const name = $("brandName").value.trim();
  const meltwater_topic_url = $("brandTopicUrl").value.trim();
  if (!name) return;
  const r = await Auth.authedFetch("/api/brands", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, meltwater_topic_url }),
  });
  const data = await r.json();
  $("brandMsg").textContent = r.ok ? "✓ Saved" : (data.error || "Failed");
  $("brandMsg").style.color = r.ok ? "var(--pos)" : "var(--neg)";
  if (r.ok) { $("brandName").value = ""; $("brandTopicUrl").value = ""; loadBrands(); }
});

async function loadBrands() {
  const r = await Auth.authedFetch("/api/brands");
  const data = await r.json();
  const list = $("brandList");
  if (!data.brands) { list.innerHTML = ""; return; }
  list.innerHTML = "<div class='field-label' style='margin-bottom:10px'>Existing brands</div>" +
    data.brands.map(b => `
      <div style="display:flex;justify-content:space-between;align-items:center;
                  padding:10px 0;border-bottom:1px solid var(--stroke);font-size:14px">
        <span><b>${escapeHtml(b.name)}</b></span>
        <span style="color:var(--muted);max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          ${escapeHtml(b.meltwater_topic_url || "no topic URL set")}
        </span>
      </div>`).join("");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

$("logoutLink").addEventListener("click", async (e) => {
  e.preventDefault();
  await Auth.signOut();
  window.location.href = "/login";
});
