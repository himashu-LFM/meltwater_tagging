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

let editingBrandId = null;

$("saveBrand").addEventListener("click", async () => {
  const name = $("brandName").value.trim();
  const meltwater_topic_url = $("brandTopicUrl").value.trim();
  if (!name) return;

  const url = editingBrandId ? `/api/brands/${editingBrandId}` : "/api/brands";
  const method = editingBrandId ? "PUT" : "POST";
  const r = await Auth.authedFetch(url, {
    method, headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, meltwater_topic_url }),
  });
  const data = await r.json();
  $("brandMsg").textContent = r.ok ? (editingBrandId ? "✓ Updated" : "✓ Saved") : (data.error || "Failed");
  $("brandMsg").style.color = r.ok ? "var(--pos)" : "var(--neg)";
  if (r.ok) { cancelEdit(); loadBrands(); }
});

function startEdit(brand) {
  editingBrandId = brand.id;
  $("brandName").value = brand.name || "";
  $("brandTopicUrl").value = brand.meltwater_topic_url || "";
  $("saveBrand").querySelector(".btn-label").textContent = "Update brand";
  $("cancelEditBrand").classList.remove("hidden");
  $("brandName").focus();
  $("brandName").scrollIntoView({ behavior: "smooth", block: "center" });
}

function cancelEdit() {
  editingBrandId = null;
  $("brandName").value = "";
  $("brandTopicUrl").value = "";
  $("saveBrand").querySelector(".btn-label").textContent = "Save brand";
  $("cancelEditBrand").classList.add("hidden");
}

$("cancelEditBrand").addEventListener("click", cancelEdit);

async function deleteBrand(brand) {
  if (!confirm(`Delete brand "${brand.name}"? This can't be undone.`)) return;
  const r = await Auth.authedFetch(`/api/brands/${brand.id}`, { method: "DELETE" });
  const data = await r.json();
  $("brandMsg").textContent = r.ok ? "✓ Deleted" : (data.error || "Failed");
  $("brandMsg").style.color = r.ok ? "var(--pos)" : "var(--neg)";
  if (r.ok) { if (editingBrandId === brand.id) cancelEdit(); loadBrands(); }
}

let brandsCache = [];

async function loadBrands() {
  const r = await Auth.authedFetch("/api/brands");
  const data = await r.json();
  const list = $("brandList");
  if (!data.brands) { list.innerHTML = ""; return; }
  brandsCache = data.brands;
  list.innerHTML = "<div class='brand-list-label'>Existing brands</div>" +
    data.brands.map(b => `
      <div class="brand-row">
        <div class="brand-info">
          <b>${escapeHtml(b.name)}</b>
          <span class="brand-url">${escapeHtml(b.meltwater_topic_url || "no topic URL set")}</span>
        </div>
        <div class="brand-actions">
          <button class="mini-btn" data-edit="${b.id}">Edit</button>
          <button class="mini-btn danger" data-del="${b.id}">Delete</button>
        </div>
      </div>`).join("");

  list.querySelectorAll("[data-edit]").forEach(btn =>
    btn.addEventListener("click", () => startEdit(brandsCache.find(x => x.id == btn.dataset.edit))));
  list.querySelectorAll("[data-del]").forEach(btn =>
    btn.addEventListener("click", () => deleteBrand(brandsCache.find(x => x.id == btn.dataset.del))));
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

$("logoutLink").addEventListener("click", async (e) => {
  e.preventDefault();
  await Auth.signOut();
  window.location.href = "/login";
});
