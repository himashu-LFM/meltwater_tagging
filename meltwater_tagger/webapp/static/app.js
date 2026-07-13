const $ = (id) => document.getElementById(id);

const state = { urls: [], results: [], brand: "", runId: null };

(async () => {
  const session = await Auth.requireAuthOrRedirect();
  if (!session) return;
  await loadBrands();
})();

async function loadBrands() {
  const r = await Auth.authedFetch("/api/brands");
  const data = await r.json();
  const sel = $("brand");
  if (data.brands && data.brands.length) {
    sel.innerHTML = data.brands.map(b => `<option value="${escAttr(b.name)}">${escapeHtml(b.name)}</option>`).join("");
  } else {
    sel.innerHTML = `<option value="">No brands configured — add one on Profile</option>`;
  }
}

// ---- URL counting ----
function countUrls() {
  const pasted = $("urls").value.split(/\s+/).map(s => s.trim()).filter(Boolean);
  const total = pasted.length || state.urls.length;
  $("urlCount").textContent = `${total} URL${total === 1 ? "" : "s"}`;
}
$("urls").addEventListener("input", () => { state.urls = []; countUrls(); });

// ---- fetch-mode pill ----
$("fetchMode").addEventListener("change", (e) => {
  const labels = { cdp: "CDP fetch", reddit_cookie: "Cookie fetch", anon: "Anon fetch" };
  $("modePill").textContent = labels[e.target.value] || e.target.value;
});

// ---- file upload / dropzone ----
const dz = $("dropzone"), fileInput = $("fileInput");
dz.addEventListener("click", () => fileInput.click());
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", (e) => {
  e.preventDefault(); dz.classList.remove("drag");
  if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener("change", () => { if (fileInput.files[0]) handleFile(fileInput.files[0]); });

async function handleFile(file) {
  $("inputErr").textContent = "";
  $("dzSub").textContent = `reading ${file.name}…`;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await Auth.authedFetch("/api/extract", { method: "POST", body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Failed to read file");
    state.urls = data.urls;
    $("urls").value = "";
    if (data.brand) {
      const opt = [...$("brand").options].find(o => o.value.toLowerCase() === data.brand.toLowerCase());
      if (opt) $("brand").value = opt.value;
    }
    $("dzSub").textContent = `✓ ${data.count} URLs loaded from ${file.name}`;
    countUrls();
  } catch (err) {
    $("inputErr").textContent = err.message;
    $("dzSub").textContent = "drag & drop or click to browse";
  }
}

// ---- classify ----
$("runBtn").addEventListener("click", run);
async function run() {
  $("inputErr").textContent = "";
  const brand = $("brand").value.trim();
  const pasted = $("urls").value.split(/\s+/).map(s => s.trim()).filter(Boolean);
  const urls = pasted.length ? pasted : state.urls;
  if (!brand) return ($("inputErr").textContent = "Please choose a brand.");
  if (!urls.length) return ($("inputErr").textContent = "Upload an Excel or paste at least one URL.");

  state.brand = brand;
  showView("loadingView");
  cycleLoaderText();

  try {
    const r = await Auth.authedFetch("/api/classify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls, brand, fetch_mode: $("fetchMode").value }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Classification failed");
    state.results = data.results;
    state.runId = data.run_id || null;
    renderResults(data);
    showView("resultsView");
    const applied = data.results.filter(x => x.tag).length;
    Toast.success(`Classified ${data.results.length} posts · ${applied} tagged.`, "Classification done");
  } catch (err) {
    showView("inputView");
    $("inputErr").textContent = err.message;
    Toast.error(err.message, "Classification failed");
  }
}

let loaderTimer;
function cycleLoaderText() {
  const msgs = ["Fetching post text…", "Reading full threads…", "Judging sentiment…", "Applying brand rules…"];
  let i = 0;
  $("loaderText").textContent = msgs[0];
  clearInterval(loaderTimer);
  loaderTimer = setInterval(() => { i = (i + 1) % msgs.length; $("loaderText").textContent = msgs[i]; }, 1800);
}

// ---- render ----
function renderResults(data) {
  clearInterval(loaderTimer);
  $("resBrand").textContent = data.run_brand;
  $("applyStatus").textContent = "";
  $("applyStatus").className = "apply-status";
  const res = data.results;

  const counts = { positive: 0, negative: 0, neutral: 0, other: 0 };
  res.forEach(r => {
    const s = (r.sentiment || "").toLowerCase();
    if (counts[s] !== undefined) counts[s]++; else counts.other++;
  });
  const appliedCount = res.filter(r => r.applied).length;
  $("stats").innerHTML =
    `<span class="stat">${res.length} posts</span>` +
    `<span class="stat">🟢 ${counts.positive} positive</span>` +
    `<span class="stat">🔴 ${counts.negative} negative</span>` +
    `<span class="stat">⚪ ${counts.neutral} neutral</span>` +
    `<span class="stat">⚑ ${counts.other} flagged/other</span>` +
    (appliedCount ? `<span class="chip positive">🏷 ${appliedCount} in Meltwater</span>` : "");

  // No taggable posts -> nothing Apply could do; make that obvious up front.
  const taggable = res.some(r => r.action === "apply" && r.tag);
  $("applyBtn").disabled = !taggable;
  $("applyBtn").title = taggable ? "" : "No taggable posts in this run";

  const body = $("resBody");
  body.innerHTML = "";
  res.forEach((r, idx) => {
    const s = (r.sentiment || "").toLowerCase();
    const cls = ["positive", "negative", "neutral"].includes(s) ? s : "flag";
    const chipText = r.tag ? s : (r.flag_brand ? `flag → ${r.flag_brand}` : r.action);
    const tr = document.createElement("tr");
    tr.style.animationDelay = (idx * 30) + "ms";
    tr.innerHTML = `
      <td>${idx + 1}</td>
      <td><span class="chip ${cls}">${escapeHtml(chipText || "—")}</span></td>
      <td>${escapeHtml(r.tag || "—")}</td>
      <td class="reason">${escapeHtml(r.reason || "")}</td>
      <td><a href="${encodeURI(r.permalink)}" target="_blank" rel="noopener">${escapeHtml(shorten(r.permalink))}</a></td>
      <td>${r.applied ? '<span class="chip positive">✓ Applied</span>' : '—'}</td>`;
    body.appendChild(tr);
  });
}

function shorten(u) { return u.length > 60 ? u.slice(0, 57) + "…" : u; }
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escAttr(s) { return escapeHtml(s); }

// ---- export ----
$("exportBtn").addEventListener("click", async () => {
  const t = Toast.loading("Preparing your Excel…");
  try {
    const r = await Auth.authedFetch("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ results: state.results, run_brand: state.brand }),
    });
    if (!r.ok) return t.error("Export failed. Please try again.");
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `tagging_${state.brand}.xlsx`;
    a.click();
    URL.revokeObjectURL(a.href);
    t.success(`Exported ${state.results.length} rows to Excel.`, "Download ready");
  } catch (err) {
    t.error(err.message);
  }
});

// ---- apply to meltwater ----
function applySummaryChips(data) {
  // A compact, always-visible recap under the results header — so the outcome
  // stays on screen after the toast fades.
  const applied = (data.applied || []).length;
  const already = (data.skipped_already || []).length;
  const failed = (data.failed || []).length;
  const unreached = (data.unreached || []).length;
  const bits = [];
  if (applied)   bits.push(`<span class="chip positive">✓ ${applied} applied</span>`);
  if (already)   bits.push(`<span class="chip neutral">⏭ ${already} already tagged</span>`);
  if (failed)    bits.push(`<span class="chip negative">✗ ${failed} failed</span>`);
  if (unreached) bits.push(`<span class="chip flag">🔍 ${unreached} not found in feed</span>`);
  if (!bits.length) bits.push(`<span class="chip neutral">Nothing needed applying</span>`);
  $("applyStatus").innerHTML = bits.join(" ") +
    `<span class="apply-time">${new Date().toLocaleTimeString()}</span>`;
  $("applyStatus").className = "apply-status";
}

$("applyBtn").addEventListener("click", async () => {
  const btn = $("applyBtn");
  const label = btn.querySelector(".btn-label");
  const origLabel = label.textContent;
  btn.disabled = true;
  btn.classList.add("busy");
  label.textContent = "⏳ Applying…";
  const t = Toast.loading("Logging into Meltwater and applying tags — this can take a minute…", "Applying tags");
  try {
    const r = await Auth.authedFetch("/api/apply", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ results: state.results, run_brand: state.brand, run_id: state.runId }),
    });
    const data = await r.json();
    if (r.ok) {
      const applied = (data.applied || []).length;
      if (applied) {
        t.success(`${data.message} · ${(data.skipped_already||[]).length} already tagged, ${(data.failed||[]).length} failed.`, "Applied to Meltwater");
        if (window.FX && window.FX.celebrate) window.FX.celebrate();
      } else {
        t.info("No new tags were applied — see the summary chips for details.", "Nothing applied");
      }
      const confirmed = new Set([...(data.applied||[]), ...(data.skipped_already||[])].map(x => x.permalink));
      state.results.forEach(r2 => { if (confirmed.has(r2.permalink)) r2.applied = true; });
      renderResults({ run_brand: state.brand, results: state.results });
      applySummaryChips(data);
      if ((data.unreached || []).length) {
        Toast.info(`${data.unreached.length} post(s) weren't found in the Meltwater feed — check the topic's date range covers them.`, "Some posts not found");
      }
    } else {
      t.error(data.error || data.message || "Apply failed.");
    }
  } catch (err) {
    t.error(err.message);
  } finally {
    btn.disabled = false;
    btn.classList.remove("busy");
    label.textContent = origLabel;
  }
});

// ---- nav ----
$("backBtn").addEventListener("click", () => showView("inputView"));
function showView(id) {
  ["inputView", "loadingView", "resultsView"].forEach(v => $(v).classList.toggle("hidden", v !== id));
}

$("logoutLink").addEventListener("click", async (e) => {
  e.preventDefault();
  await Auth.signOut();
  window.location.href = "/login";
});

// sync fetch-mode pill with whatever option is selected on load
const labels0 = { cdp: "CDP fetch", reddit_cookie: "Cookie fetch", anon: "Anon fetch" };
$("modePill").textContent = labels0[$("fetchMode").value] || $("fetchMode").value;
countUrls();
