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
    $("tagsHeading").textContent = "Tags & client feedback";
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
    <div class="tag-card" style="margin-bottom:12px">
      <div class="field-label" style="margin:0 0 10px">Tag list</div>
      <p class="section-sub" style="margin:0 0 12px">
        The tags the classifier can assign. Add a new tag or remove one — it takes effect on the
        next run, no code change needed. Type the tag <b>exactly</b> as it appears in Meltwater
        (the tag must also exist in Meltwater for "Apply" to set it).
      </p>
      <div class="row" style="margin-bottom:0; align-items:flex-end">
        <label class="field" style="flex:0 0 190px">
          <span class="field-label">Family</span>
          <select id="tlFamily"></select>
        </label>
        <label class="field" style="flex:2">
          <span class="field-label">Tag</span>
          <input type="text" id="tlLabel" placeholder="Product - New Product" />
        </label>
        <label class="field" style="flex:1.4">
          <span class="field-label">Match words (optional, comma-separated)</span>
          <input type="text" id="tlKeywords" placeholder="e.g. new product, NP" />
        </label>
        <button class="btn primary" id="tlAddBtn" style="flex:0 0 auto">
          <span class="btn-shine"></span><span class="btn-label">Add tag</span>
        </button>
      </div>
      <div class="tl-toolbar">
        <input type="search" id="tlSearch" placeholder="Search tags…" autocomplete="off" />
        <span class="section-sub" id="tlSummary"></span>
      </div>
      <div id="tlFamilies"></div>
    </div>
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
  loadTagList(id);

  // Fill the dropdown up front — it must work even if the list fails to load.
  $("tlFamily").innerHTML = TAG_FAMILIES.map(f =>
    `<option value="${f.key}">${escapeHtml(f.name)}</option>`).join("");
  $("tlLabel").value = TAG_FAMILIES[0].prefix;
  $("tlFamily").addEventListener("change", () => {
    const fam = TAG_FAMILIES.find(f => f.key === $("tlFamily").value);
    if (!fam) return;
    // Swap the family prefix on whatever was typed, keeping the name part.
    const v = $("tlLabel").value;
    const old = TAG_FAMILIES.find(f => v.toLowerCase().startsWith(f.prefix.toLowerCase()));
    const rest = old ? v.slice(old.prefix.length) : v;
    $("tlLabel").value = fam.prefix + rest.replace(/^\s+/, "");
    $("tlLabel").placeholder = fam.prefix + "…";
    $("tlLabel").focus();
  });
  tlView.open.clear(); tlView.q = "";
  $("tlSearch").addEventListener("input", () => { tlView.q = $("tlSearch").value; renderTagList(id); });
  $("tlLabel").addEventListener("keydown", (e) => { if (e.key === "Enter") $("tlAddBtn").click(); });
  $("tlAddBtn").addEventListener("click", async () => {
    const family = $("tlFamily").value;
    const label = $("tlLabel").value.trim();
    if (!label) return Toast.error("Type the tag name first.");
    const r = await Auth.authedFetch(`/api/brands/${id}/tag-list`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ family, label, keywords: $("tlKeywords").value }),
    });
    const d = await r.json();
    if (!r.ok) return Toast.error(d.error || "Could not add tag.");
    Toast.success(`"${label}" added to the tag list.`, "Tag added");
    const fam = TAG_FAMILIES.find(f => f.key === family);
    $("tlLabel").value = fam ? fam.prefix : "";
    $("tlKeywords").value = "";
    tlView.open.add(family);
    renderTagList(id, d.families);
  });

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

// --- Tag list (add / remove tags without a code change) ----------------------
// Mirrors _TAG_FAMILIES in app.py (key, display name, exact Meltwater prefix).
const TAG_FAMILIES = [
  { key: "TYPE_OF_PUBLICATION", name: "Type of Publication", prefix: "Type of Publication - " },
  { key: "TYPE_OF_COVERAGE",    name: "Type of Coverage",    prefix: "Type of Coverage - " },
  { key: "REGION",              name: "Region",              prefix: "Region - " },
  { key: "CORPORATE",           name: "Corporate",           prefix: "Corporate - " },
  { key: "PILLAR",              name: "Pillar",              prefix: "Pillar - " },
  { key: "INDUSTRY",            name: "Industry",            prefix: "Industry | " },
  { key: "PRODUCT",             name: "Product",             prefix: "Product - " },
  { key: "SPOKESPERSON",        name: "Spokesperson",        prefix: "Spokesperson | " },
];

// Tags the classifier's own code assigns (not just the model). Removing one
// stops it being APPLIED to Meltwater, but results will still show it.
const CODE_ASSIGNED_TAGS = new Set([
  "Corporate - Financial / IR",
  "Corporate - Product & Technology",
  "Type of Coverage - Press release",
  "Type of Coverage - Unique",
  "Type of Coverage - 3rd party press release",
]);

async function loadTagList(id) {
  let r, d;
  try {
    r = await Auth.authedFetch(`/api/brands/${id}/tag-list`);
    d = await r.json();
  } catch (e) {
    d = {};
  }
  if (!r || !r.ok) {
    $("tlFamilies").innerHTML = `<p class="section-sub" style="margin:0">${escapeHtml(d.error || "Could not load the tag list.")}</p>`;
    return;
  }
  renderTagList(id, d.families);
}

// View state for the tag list: which families are expanded + the search text.
const tlView = { families: [], open: new Set(), q: "" };

function renderTagList(id, families) {
  if (families) tlView.families = families;
  const q = tlView.q.trim().toLowerCase();
  const match = (l) => !q || l.toLowerCase().includes(q);
  // Show the name without the family prefix (the family is the row heading).
  const short = (f, l) => l.toLowerCase().startsWith(f.prefix.toLowerCase()) ? l.slice(f.prefix.length) : l;

  let total = 0, added = 0, removed = 0, shown = 0;
  const rows = tlView.families.map(f => {
    total += f.tags.length;
    const nNew = f.tags.filter(t => t.custom).length;
    added += nNew; removed += f.removed.length;
    const tags = f.tags.filter(t => match(t.label));
    const gone = f.removed.filter(match);
    if (q && !tags.length && !gone.length) return "";
    shown += tags.length;
    const open = q ? true : tlView.open.has(f.key);
    return `
      <div class="tl-fam ${open ? "open" : ""}">
        <button class="tl-fam-head" data-fam="${escAttr(f.key)}" aria-expanded="${open}">
          <span class="tl-caret">▸</span>
          <span class="tl-fam-name">${escapeHtml(f.name)}</span>
          <span class="tl-count">${q ? `${tags.length} of ${f.tags.length}` : f.tags.length}</span>
          ${nNew ? `<span class="tl-badge new">${nNew} new</span>` : ""}
          ${f.removed.length ? `<span class="tl-badge gone">${f.removed.length} removed</span>` : ""}
        </button>
        ${open ? `
        <div class="tl-chips">
          ${tags.map(t => `
            <span class="chip tag tl-chip ${t.custom ? "tl-custom" : ""}" title="${escAttr(t.label)}${t.custom ? " — added from this page" : ""}">
              ${escapeHtml(short(f, t.label))}${t.custom ? ' <em>new</em>' : ''}
              <button class="tl-x" title="Remove" data-fam="${escAttr(f.key)}" data-label="${escAttr(t.label)}">×</button>
            </span>`).join("") || (gone.length ? "" : '<span class="section-sub">No tags.</span>')}
          ${gone.map(l => `
            <span class="chip tl-chip tl-removed" title="${escAttr(l)} — removed, click ↺ to restore">
              <s>${escapeHtml(short(f, l))}</s>
              <button class="tl-restore" title="Restore" data-fam="${escAttr(f.key)}" data-label="${escAttr(l)}">↺</button>
            </span>`).join("")}
        </div>` : ""}
      </div>`;
  }).join("");

  $("tlFamilies").innerHTML = rows ||
    `<p class="section-sub" style="margin:8px 0 0">No tags match "${escapeHtml(tlView.q.trim())}".</p>`;
  $("tlSummary").textContent = q
    ? `${shown} match${shown === 1 ? "" : "es"}`
    : `${total} tags` + (added ? ` · ${added} new` : "") + (removed ? ` · ${removed} removed` : "");

  $("tlFamilies").querySelectorAll(".tl-fam-head").forEach(btn => btn.addEventListener("click", () => {
    if (tlView.q.trim()) return;          // while searching, matches stay open
    const k = btn.dataset.fam;
    tlView.open.has(k) ? tlView.open.delete(k) : tlView.open.add(k);
    renderTagList(id);
  }));

  $("tlFamilies").querySelectorAll(".tl-x").forEach(btn => btn.addEventListener("click", async () => {
    const label = btn.dataset.label;
    const ok = await Modal.confirm({
      title: "Remove this tag?",
      message: CODE_ASSIGNED_TAGS.has(label)
        ? `"${label}" is assigned by built-in tagging rules, so it will still appear in results — ` +
          `but "Apply to Meltwater" will skip it. You can restore it later.`
        : `"${label}" will no longer be assigned by the classifier. You can restore it later.`,
      okText: "Remove", danger: true,
    });
    if (!ok) return;
    const r = await Auth.authedFetch(`/api/brands/${id}/tag-list`, {
      method: "DELETE", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ family: btn.dataset.fam, label }),
    });
    const d = await r.json();
    if (!r.ok) return Toast.error(d.error || "Could not remove tag.");
    Toast.success(`"${label}" removed.`);
    renderTagList(id, d.families);
  }));

  $("tlFamilies").querySelectorAll(".tl-restore").forEach(btn => btn.addEventListener("click", async () => {
    const r = await Auth.authedFetch(`/api/brands/${id}/tag-list`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ family: btn.dataset.fam, label: btn.dataset.label }),
    });
    const d = await r.json();
    if (!r.ok) return Toast.error(d.error || "Could not restore tag.");
    Toast.success(`"${btn.dataset.label}" restored.`);
    renderTagList(id, d.families);
  }));
}

async function loadFeedbackRules(id) {
  const r = await Auth.authedFetch(`/api/brands/${id}/feedback-rules`);
  const d = await r.json();
  // Active rules first; inactive ones are kept for reference but NOT sent to
  // the classifier (live_rules reads active rules only).
  const rules = (d.rules || []).slice().sort((a, b) => (b.active !== false) - (a.active !== false));
  const nInactive = rules.filter(r => r.active === false).length;
  $("fbRuleCount").textContent = !rules.length ? ""
    : nInactive ? `· ${rules.length - nInactive} active · ${nInactive} inactive` : `· ${rules.length}`;
  if (!rules.length) {
    $("fbRules").innerHTML = `<p class="section-sub" style="margin:0">No rules yet — upload a doc to extract some.</p>`;
    return;
  }
  $("fbRules").innerHTML = rules.map(rule => `
    <div class="tag-card ${rule.active === false ? "fb-inactive" : ""}" style="margin-bottom:8px">
      <div class="tag-card-head" style="justify-content:space-between">
        <span>
          <span class="chip flag">${escapeHtml(rule.category || "general")}</span>
          ${rule.active === false ? '<span class="chip fb-inactive-chip" title="Not used by the classifier">inactive</span>' : ""}
        </span>
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
