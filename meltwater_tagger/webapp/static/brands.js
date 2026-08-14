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
  $("cfgEnvironment").value = selected.environment || "";
  $("cfgMsg").textContent = "";

  // load my personal topic URL override
  $("myTopicUrl").value = "";
  Auth.authedFetch(`/api/brands/${id}/my-topic-url`).then(r => r.json()).then(d => {
    $("myTopicUrl").value = d.topic_url || "";
  });

  // Taxonomy brands (Bentley) don't use positive/negative/neutral — they tag
  // from a big protocol taxonomy and are guided by uploaded client feedback docs.
  // So for them we swap the sentiment "Tags & rules" card for an upload card.
  if (isTaxonomyBrand(selected.name)) {
    $("tagsHeading").textContent = "Client feedback docs";
    renderFeedbackDocs(id);
    return;
  }

  $("tagsHeading").textContent = "Tags & rules";

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
          ${t.rule ? '<span class="chip flag" title="A custom rule guides this sentiment">⚙ rule active</span>' : ''}
          <input type="text" class="tag-label-input" data-s="${s}"
                 value="${escAttr(t.tag_label || defLabel)}" placeholder="${escAttr(defLabel)}" />
        </div>
        <textarea class="tag-rule-input" data-s="${s}" rows="3"
          placeholder="Optional rule for ${s} — e.g. what counts as ${s} for ${escAttr(selected.name)}. Leave blank to use default logic.">${escapeHtml(t.rule || "")}</textarea>
      </div>`;
  }).join("");
}

// Which brands use the taxonomy pipeline (many tags + feedback docs) instead of
// sentiment. Kept as a simple list for now; add future taxonomy brands here.
function isTaxonomyBrand(name) {
  return (name || "").trim().toLowerCase() === "bentley";
}

// Render the "Client feedback docs" card: upload + list of uploaded docs.
async function renderFeedbackDocs(id) {
  $("tagCards").innerHTML = `
    <div class="tag-card">
      <p class="section-sub" style="margin:0 0 12px">
        Upload the client's "Tagging Adjustments" feedback docs (.docx, .txt, .md).
        These become the living rules the classifier follows — no positive/negative/neutral here.
      </p>
      <div class="row" style="margin-bottom:0; align-items:center">
        <input type="file" id="fbFile" accept=".docx,.txt,.md" />
        <button class="btn primary" id="fbUploadBtn" style="flex:0 0 auto">
          <span class="btn-shine"></span><span class="btn-label">Upload doc</span>
        </button>
      </div>
      <div id="fbList" style="margin-top:16px"></div>
    </div>
    <div class="tag-card" style="margin-top:12px">
      <div class="field-label" style="margin:0 0 10px">Extracted rules <span class="section-sub" id="fbRuleCount"></span></div>
      <p class="section-sub" style="margin:0 0 12px">Each uploaded doc is parsed into reusable rules the classifier follows on future articles.</p>
      <div id="fbRules"></div>
    </div>`;

  loadFeedbackDocs(id);
  loadFeedbackRules(id);

  $("fbUploadBtn").addEventListener("click", async () => {
    const input = $("fbFile");
    if (!input.files || !input.files[0]) return Toast.error("Choose a file first.");
    const fd = new FormData();
    fd.append("file", input.files[0]);
    $("fbUploadBtn").disabled = true;
    const btnLabel = $("fbUploadBtn").querySelector(".btn-label");
    const prev = btnLabel ? btnLabel.textContent : "";
    if (btnLabel) btnLabel.textContent = "Parsing…";
    try {
      const r = await Auth.authedFetch(`/api/brands/${id}/feedback-docs`, { method: "POST", body: fd });
      const d = await r.json();
      if (!r.ok) return Toast.error(d.error || "Upload failed");
      if (d.extract_error) {
        Toast.error(`Doc saved, but rule extraction failed: ${d.extract_error}`, "Partial");
      } else {
        Toast.success(`Uploaded "${d.doc.filename}" — ${d.rules_added} rule(s) extracted.`, "Doc parsed");
      }
      input.value = "";
      loadFeedbackDocs(id);
      loadFeedbackRules(id);
    } finally {
      $("fbUploadBtn").disabled = false;
      if (btnLabel) btnLabel.textContent = prev;
    }
  });
}

async function loadFeedbackRules(id) {
  const r = await Auth.authedFetch(`/api/brands/${id}/feedback-rules`);
  const d = await r.json();
  const rules = d.rules || [];
  $("fbRuleCount").textContent = rules.length ? `· ${rules.length}` : "";
  if (!rules.length) {
    $("fbRules").innerHTML = `<p class="section-sub" style="margin:0">No rules yet — upload a doc to extract some.</p>`;
    return;
  }
  $("fbRules").innerHTML = rules.map(rule => `
    <div class="tag-card" style="margin-bottom:8px">
      <div class="tag-card-head" style="justify-content:space-between">
        <span class="chip flag">${escapeHtml(rule.category || "general")}</span>
        <button class="mini-btn danger" data-rule="${escAttr(rule.id)}">Delete</button>
      </div>
      <div style="margin-top:8px">${escapeHtml(rule.rule_text || "")}</div>
      ${rule.example_url ? `<div class="section-sub" style="margin-top:6px">↳ ${escapeHtml(rule.example_url)}</div>` : ""}
    </div>`).join("");
  $("fbRules").querySelectorAll("button[data-rule]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const r2 = await Auth.authedFetch(`/api/brands/${id}/feedback-rules/${btn.dataset.rule}`, { method: "DELETE" });
      if (r2.ok) { Toast.success("Rule deleted."); loadFeedbackRules(id); }
      else Toast.error("Could not delete rule.");
    });
  });
}

async function loadFeedbackDocs(id) {
  const r = await Auth.authedFetch(`/api/brands/${id}/feedback-docs`);
  const d = await r.json();
  const docs = d.docs || [];
  if (!docs.length) {
    $("fbList").innerHTML = `<p class="section-sub" style="margin:0">No feedback docs uploaded yet.</p>`;
    return;
  }
  $("fbList").innerHTML = docs.map(doc => `
    <div class="tag-card-head" style="justify-content:space-between; margin-bottom:8px">
      <span>📄 ${escapeHtml(doc.filename || "untitled")}
        <span class="section-sub">· ${new Date(doc.created_at).toLocaleDateString()}</span></span>
      <button class="mini-btn danger" data-doc="${escAttr(doc.id)}">Remove</button>
    </div>`).join("");
  $("fbList").querySelectorAll("button[data-doc]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const ok = await Modal.confirm({
        title: "Remove this doc?",
        message: "This deletes the uploaded feedback doc. This can't be undone.",
        okText: "Remove", danger: true,
      });
      if (!ok) return;
      const r = await Auth.authedFetch(`/api/brands/${id}/feedback-docs/${btn.dataset.doc}`, { method: "DELETE" });
      if (r.ok) { Toast.success("Doc removed."); loadFeedbackDocs(id); }
      else Toast.error("Could not remove doc.");
    });
  });
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
  const environment = $("cfgEnvironment").value.trim();

  // 1) update brand core fields
  const r1 = await Auth.authedFetch(`/api/brands/${selected.id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, meltwater_topic_url, roll_up_terms, environment }),
  });
  if (!r1.ok) { const d = await r1.json(); return Toast.error(d.error || "Could not save brand details"); }

  // 2) save tags + rules — sentiment brands only. Taxonomy brands (Bentley)
  //    have no sentiment inputs on screen; their rules come from feedback docs.
  if (!isTaxonomyBrand(name)) {
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
  }

  Toast.success(`Configuration saved for ${name}.`, "Brand config saved");
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
