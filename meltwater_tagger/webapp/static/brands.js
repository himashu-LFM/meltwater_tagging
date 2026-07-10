const $ = (id) => document.getElementById(id);

const SENTIMENTS = ["positive", "negative", "neutral"];
let brands = [];
let selected = null;

(async () => {
  const s = await Auth.requireAuthOrRedirect();
  if (!s) return;
  await loadBrands();
})();

async function loadBrands(selectId) {
  const r = await Auth.authedFetch("/api/brands");
  const data = await r.json();
  brands = data.brands || [];
  const list = $("brandList");
  if (!brands.length) {
    list.innerHTML = `<div style="color:var(--muted);font-size:13px;padding:8px 0">No brands yet.</div>`;
  } else {
    list.innerHTML = brands.map(b => `
      <button class="brand-pick ${selected && selected.id === b.id ? 'active' : ''}" data-id="${b.id}">
        ${escapeHtml(b.name)}
      </button>`).join("");
    list.querySelectorAll(".brand-pick").forEach(btn =>
      btn.addEventListener("click", () => selectBrand(+btn.dataset.id)));
  }
  const toSelect = selectId || (selected && selected.id) || (brands[0] && brands[0].id);
  if (toSelect) selectBrand(toSelect);
  else showEmpty();
}

function showEmpty() {
  selected = null;
  $("configForm").classList.add("hidden");
  $("configEmpty").classList.remove("hidden");
}

async function selectBrand(id) {
  selected = brands.find(b => b.id === id);
  if (!selected) return showEmpty();
  document.querySelectorAll(".brand-pick").forEach(b =>
    b.classList.toggle("active", +b.dataset.id === id));

  $("configEmpty").classList.add("hidden");
  $("configForm").classList.remove("hidden");
  $("cfgName").value = selected.name || "";
  $("cfgTopicUrl").value = selected.meltwater_topic_url || "";
  $("cfgRollup").value = (selected.roll_up_terms || []).join(", ");
  $("cfgMsg").textContent = "";

  // load my personal topic URL override
  $("myTopicUrl").value = "";
  Auth.authedFetch(`/api/brands/${id}/my-topic-url`).then(r => r.json()).then(d => {
    $("myTopicUrl").value = d.topic_url || "";
  });

  // load tags/rules
  const r = await Auth.authedFetch(`/api/brands/${id}/tags`);
  const data = await r.json();
  const byS = {};
  (data.tags || []).forEach(t => { byS[t.sentiment] = t; });

  $("tagCards").innerHTML = SENTIMENTS.map(s => {
    const t = byS[s] || {};
    const cap = s.charAt(0).toUpperCase() + s.slice(1);
    const defLabel = `${cap} - ${selected.name}`;
    return `
      <div class="tag-card">
        <div class="tag-card-head">
          <span class="chip ${s}">${s}</span>
          <input type="text" class="tag-label-input" data-s="${s}"
                 value="${escAttr(t.tag_label || defLabel)}" placeholder="${escAttr(defLabel)}" />
        </div>
        <textarea class="tag-rule-input" data-s="${s}" rows="3"
          placeholder="Optional rule for ${s} — e.g. what counts as ${s} for ${escAttr(selected.name)}. Leave blank to use default logic.">${escapeHtml(t.rule || "")}</textarea>
      </div>`;
  }).join("");
}

$("addBrandBtn").addEventListener("click", async () => {
  const name = await Modal.prompt({
    title: "Add a brand",
    message: "Give the brand a name — you can add its tags, rules and topic URL next.",
    placeholder: "e.g. Ninja",
    okText: "Create brand",
  });
  if (!name || !name.trim()) return;
  const r = await Auth.authedFetch("/api/brands", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  });
  const data = await r.json();
  if (!r.ok) return Toast.error(data.error || "Failed to add brand");
  selected = data.brand;
  await loadBrands(data.brand.id);
  Toast.success(`Brand "${data.brand.name}" added.`, "Brand created");
});

$("deleteBrandBtn").addEventListener("click", async () => {
  if (!selected) return;
  const ok = await Modal.confirm({
    title: `Delete "${selected.name}"?`,
    message: "This removes the brand and its tag rules. This can't be undone.",
    okText: "Delete brand",
    danger: true,
  });
  if (!ok) return;
  const name = selected.name;
  const r = await Auth.authedFetch(`/api/brands/${selected.id}`, { method: "DELETE" });
  if (!r.ok) { const d = await r.json(); return Toast.error(d.error || "Failed to delete brand"); }
  selected = null;
  await loadBrands();
  Toast.info(`Brand "${name}" deleted.`, "Removed");
});

$("saveMyTopicBtn").addEventListener("click", async () => {
  if (!selected) return;
  const topic_url = $("myTopicUrl").value.trim();
  if (!topic_url) return Toast.error("Paste your Meltwater topic URL first.");
  const r = await Auth.authedFetch(`/api/brands/${selected.id}/my-topic-url`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ topic_url }),
  });
  const data = await r.json();
  if (r.ok) Toast.success("Apply to Meltwater will now use your personal topic URL for this brand.", "Saved");
  else Toast.error(data.error || "Could not save your topic URL.");
});

$("saveConfigBtn").addEventListener("click", async () => {
  if (!selected) return;
  const name = $("cfgName").value.trim();
  const meltwater_topic_url = $("cfgTopicUrl").value.trim();
  const roll_up_terms = $("cfgRollup").value.split(",").map(s => s.trim()).filter(Boolean);

  // 1) update brand core fields
  const r1 = await Auth.authedFetch(`/api/brands/${selected.id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, meltwater_topic_url, roll_up_terms }),
  });
  if (!r1.ok) { const d = await r1.json(); return Toast.error(d.error || "Could not save brand details"); }

  // 2) save tags + rules
  const tags = SENTIMENTS.map(s => ({
    sentiment: s,
    tag_label: document.querySelector(`.tag-label-input[data-s="${s}"]`).value.trim()
               || `${s.charAt(0).toUpperCase() + s.slice(1)} - ${name}`,
    rule: document.querySelector(`.tag-rule-input[data-s="${s}"]`).value.trim(),
  }));
  const r2 = await Auth.authedFetch(`/api/brands/${selected.id}/tags`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tags }),
  });
  if (!r2.ok) { const d = await r2.json(); return Toast.error(d.error || "Could not save tags & rules"); }

  const hasRules = tags.some(t => t.rule);
  Toast.success(
    hasRules ? `Saved — rules will guide tagging for ${name}.` : `Configuration saved for ${name}.`,
    "Brand config saved"
  );
  await loadBrands(selected.id);
});

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escAttr(s) { return escapeHtml(s); }

$("logoutLink").addEventListener("click", async (e) => {
  e.preventDefault();
  await Auth.signOut();
  window.location.href = "/login";
});
