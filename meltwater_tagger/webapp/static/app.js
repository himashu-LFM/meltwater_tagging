const $ = (id) => document.getElementById(id);

const state = { urls: [], results: [], brand: "", runId: null, labels: null };

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
  applyFetchModeRestriction();
}

// ---- Bentley (news-site brand) only supports the news article reader —
// Reddit cookie / CDP / anon are tuned for Reddit and won't read news sites
// correctly, so lock the fetch mode instead of letting a bad combo run.
const NEWS_ONLY_BRANDS = ["bentley"];
function applyFetchModeRestriction() {
  const brand = ($("brand").value || "").trim().toLowerCase();
  const fm = $("fetchMode");
  const isNewsOnly = NEWS_ONLY_BRANDS.includes(brand);
  [...fm.options].forEach(o => {
    o.disabled = isNewsOnly && o.value !== "news_reader";
    o.hidden = isNewsOnly && o.value !== "news_reader";
  });
  if (isNewsOnly) fm.value = "news_reader";
  fm.dispatchEvent(new Event("change"));
}
$("brand").addEventListener("change", applyFetchModeRestriction);

// ---- URL counting ----
function countUrls() {
  const pasted = $("urls").value.split(/\s+/).map(s => s.trim()).filter(Boolean);
  const total = pasted.length || state.urls.length;
  $("urlCount").textContent = `${total} URL${total === 1 ? "" : "s"}`;
}
$("urls").addEventListener("input", () => { state.urls = []; countUrls(); });

// ---- fetch-mode pill ----
const FETCH_MODE_LABELS = { reddit_scraper: "Reddit Scrapper", reddit_api: "Reddit API fetch", cdp: "CDP fetch", reddit_cookie: "Cookie fetch", anon: "Anon fetch", news_reader: "News reader fetch" };
$("fetchMode").addEventListener("change", (e) => {
  $("modePill").textContent = FETCH_MODE_LABELS[e.target.value] || e.target.value;
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
      if (opt) { $("brand").value = opt.value; applyFetchModeRestriction(); }
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
  // Sent by /api/classify for sentiment brands. Absent for taxonomy brands
  // (Bentley), which keeps those rows read-only.
  if (data.labels) state.labels = data.labels;

  refreshSummary(res);

  const body = $("resBody");
  body.innerHTML = "";
  res.forEach((r, idx) => {
    const ctype = (r.content_type || deriveContentType(r.permalink)).toLowerCase();
    const typeChip = ctype === "comment"
      ? '<span class="chip type-comment">💬 Comment</span>'
      : '<span class="chip type-post">📄 Post</span>';
    const tr = document.createElement("tr");
    tr.dataset.idx = idx;
    tr.style.animationDelay = (idx * 30) + "ms";
    tr.innerHTML = `
      <td>${idx + 1}</td>
      <td>${typeChip}</td>
      <td class="cell-sentiment"></td>
      <td class="cell-tag">${escapeHtml(r.tag || "—")}</td>
      <td class="reason">${escapeHtml(r.reason || "")}</td>
      <td><a href="${encodeURI(r.permalink)}" target="_blank" rel="noopener">${escapeHtml(shorten(r.permalink))}</a></td>
      <td>${r.applied ? '<span class="chip positive">✓ Applied</span>' : '—'}</td>`;
    body.appendChild(tr);
    paintSentimentCell(tr, r, idx);
  });
}

// Stats + Apply-button state, recomputed after every manual override.
function refreshSummary(res) {
  const counts = { positive: 0, negative: 0, neutral: 0, other: 0 };
  res.forEach(r => {
    const s = (r.sentiment || "").toLowerCase();
    if (counts[s] !== undefined) counts[s]++; else counts.other++;
  });
  const appliedCount = res.filter(r => r.applied).length;
  const edited = res.filter(r => r.overridden).length;
  $("stats").innerHTML =
    `<span class="stat">${res.length} posts</span>` +
    `<span class="stat">🟢 ${counts.positive} positive</span>` +
    `<span class="stat">🔴 ${counts.negative} negative</span>` +
    `<span class="stat">⚪ ${counts.neutral} neutral</span>` +
    `<span class="stat">⚑ ${counts.other} flagged/other</span>` +
    (edited ? `<span class="chip type-comment">✎ ${edited} edited</span>` : "") +
    (appliedCount ? `<span class="chip positive">🏷 ${appliedCount} in Meltwater</span>` : "");

  const taggable = res.some(r => r.action === "apply" && r.tag);
  $("applyBtn").disabled = !taggable;
  $("applyBtn").title = taggable ? "" : "No taggable posts in this run";

  // Retry is offered only for rows the model never decided — and never for
  // rows a human set by hand (mirrors _is_retryable on the server).
  const failed = res.filter(r => r.action === "review" && !r.overridden).length;
  const retryBtn = $("retryBtn");
  if (retryBtn) {
    retryBtn.classList.toggle("hidden", failed === 0);
    retryBtn.querySelector(".btn-label").textContent = `↻ Retry ${failed} failed`;
  }
}

// The Sentiment cell: a dropdown when the brand has sentiment labels, else the
// original read-only chip.
function paintSentimentCell(tr, r, idx) {
  const cell = tr.querySelector(".cell-sentiment");
  const s = (r.sentiment || "").toLowerCase();
  const cls = ["positive", "negative", "neutral"].includes(s) ? s : "flag";

  if (!state.labels) {
    const chipText = r.tag ? s : (r.flag_brand ? `flag → ${r.flag_brand}` : r.action);
    cell.innerHTML = `<span class="chip ${cls}">${escapeHtml(chipText || "—")}</span>`;
    return;
  }

  // What the model originally decided, so the row can always be reverted.
  const autoLabel = r.auto_sentiment || s || (r.flag_brand ? `flag → ${r.flag_brand}` : r.action) || "—";
  const opts = ["positive", "negative", "neutral"]
    .map(v => `<option value="${v}"${v === s ? " selected" : ""}>${v}</option>`).join("");
  const unset = ["positive", "negative", "neutral"].includes(s)
    ? "" : `<option value="" selected>${escapeHtml(String(autoLabel))}</option>`;

  cell.innerHTML =
    `<select class="sent-select ${cls}" data-idx="${idx}" title="Change the sentiment for this row">` +
      unset + opts +
    `</select>` +
    (r.overridden ? ` <button class="sent-revert" data-idx="${idx}" title="Revert to the model's original decision">↺</button>` : "");
}

// One delegated listener, so it survives re-renders.
document.addEventListener("change", e => {
  const sel = e.target.closest && e.target.closest(".sent-select");
  if (!sel) return;
  applyOverride(Number(sel.dataset.idx), sel.value);
});
document.addEventListener("click", e => {
  const btn = e.target.closest && e.target.closest(".sent-revert");
  if (!btn) return;
  revertOverride(Number(btn.dataset.idx));
});

function applyOverride(idx, sentiment) {
  const r = state.results[idx];
  if (!r || !sentiment || !state.labels) return;

  // Remember the model's verdict once, so ↺ can always restore it.
  if (!r.overridden) {
    r.auto_sentiment = r.sentiment || r.action || "";
    r.auto_tag = r.tag || null;
    r.auto_action = r.action;
    r.auto_reason = r.reason || "";
  }
  r.sentiment = sentiment;
  r.tag = state.labels[sentiment];
  r.action = "apply";
  r.overridden = true;
  r.reason = `manually set to ${sentiment} (model said: ${r.auto_sentiment || "—"})`;
  redrawRow(idx);
}

function revertOverride(idx) {
  const r = state.results[idx];
  if (!r || !r.overridden) return;
  r.sentiment = r.auto_sentiment && ["positive", "negative", "neutral"].includes(r.auto_sentiment)
    ? r.auto_sentiment : "";
  r.tag = r.auto_tag || null;
  r.action = r.auto_action;
  r.reason = r.auto_reason || "";
  r.overridden = false;
  redrawRow(idx);
}

function redrawRow(idx) {
  const r = state.results[idx];
  const tr = $("resBody").querySelector(`tr[data-idx="${idx}"]`);
  if (tr) {
    tr.querySelector(".cell-tag").textContent = r.tag || "—";
    tr.querySelector(".reason").textContent = r.reason || "";
    tr.classList.toggle("row-edited", !!r.overridden);
    paintSentimentCell(tr, r, idx);
  }
  refreshSummary(state.results);
}

function shorten(u) { return u.length > 60 ? u.slice(0, 57) + "…" : u; }
// Mirrors classify.py reddit_ids: comment URL if it has /comment/<id> or a
// trailing base36 id after the title slug. Used as a fallback when a result
// has no stored content_type (older runs).
function deriveContentType(url) {
  if (!url || url.indexOf("reddit.com") === -1) return "post";
  const clean = String(url).split("?")[0].split("#")[0];
  const m = clean.match(/\/comments\/([a-z0-9]+)/i);
  if (!m) return "post";
  if (/\/comment\/[a-z0-9]+/i.test(clean)) return "comment";
  const after = clean.slice(m.index + m[0].length);
  const segs = after.split("/").filter(Boolean);
  if (segs.length >= 2 && /^[a-z0-9]{4,}$/i.test(segs[segs.length - 1])) return "comment";
  return "post";
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escAttr(s) { return escapeHtml(s); }

// ---- retry failed rows only ----
$("retryBtn").addEventListener("click", async () => {
  const btn = $("retryBtn");
  const failed = state.results.filter(r => r.action === "review" && !r.overridden).length;
  if (!failed) return;

  btn.disabled = true;
  const label = btn.querySelector(".btn-label");
  const original = label.textContent;
  label.textContent = `↻ Retrying ${failed}…`;
  const t = Toast.loading(`Re-classifying ${failed} failed row${failed > 1 ? "s" : ""}…`);
  try {
    const r = await Auth.authedFetch("/api/reclassify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        results: state.results, run_brand: state.brand,
        run_id: state.runId, fetch_mode: $("fetchMode").value,
      }),
    });
    const data = await r.json();
    if (!r.ok) return t.error(data.error || "Retry failed.");

    state.results = data.results;
    renderResults({ run_brand: state.brand, results: state.results, labels: data.labels });

    if (data.recovered > 0) {
      t.success(`Recovered ${data.recovered} of ${data.retried} row${data.retried > 1 ? "s" : ""}.`,
                "Retry complete");
    } else {
      t.error(`Retried ${data.retried}, but none could be classified. ` +
              `Check the post is still live, or set the sentiment manually.`);
    }
  } catch (e) {
    t.error(`Retry failed: ${e.message || e}`);
  } finally {
    btn.disabled = false;
    label.textContent = original;
  }
});

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

// ---- MFA (Meltwater SSO one-time code) ----
// While an apply run is in flight, poll the server. If a Meltwater (@meltwater.com)
// login pauses for an SMS code, show the OTP popup right here — the user never
// leaves the tagging screen. Nothing shows for ListenFirstMedia logins.
let mfaPoll = null, mfaLastRound = 0, mfaOpen = false;
function startMfaPolling() {
  mfaLastRound = 0; mfaOpen = false;
  clearInterval(mfaPoll);
  mfaPoll = setInterval(async () => {
    try {
      const r = await Auth.authedFetch("/api/mfa/status");
      if (!r.ok) return;
      const s = await r.json();
      if (s.state === "awaiting" && s.round > mfaLastRound) {
        mfaLastRound = s.round;
        showMfa(s);
      } else if (mfaOpen && ["processing", "timeout", "cancelled", "none"].includes(s.state)) {
        hideMfa();
      }
    } catch (e) { /* transient poll error — ignore */ }
  }, 2000);
}
function stopMfaPolling() { clearInterval(mfaPoll); mfaPoll = null; hideMfa(); }
function showMfa(s) {
  mfaOpen = true;
  $("mfaErr").textContent = s.error ? s.error : "";
  $("mfaSub").textContent = (s.attempt && s.max)
    ? `Meltwater texted a code to your phone. Attempt ${s.attempt} of ${s.max}.`
    : "Meltwater texted a code to your phone. Enter it to continue signing in.";
  $("mfaInput").value = "";
  $("mfaOverlay").classList.remove("hidden");
  $("mfaInput").focus();
}
function hideMfa() { mfaOpen = false; $("mfaOverlay").classList.add("hidden"); }
async function submitMfa() {
  const otp = $("mfaInput").value.trim();
  if (!otp) { $("mfaErr").textContent = "Enter the code first."; return; }
  const btn = $("mfaSubmit"); btn.disabled = true;
  try {
    const r = await Auth.authedFetch("/api/mfa/otp", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ otp }),
    });
    const d = await r.json();
    if (!r.ok) { $("mfaErr").textContent = d.error || "Could not submit the code."; }
    else { $("mfaSub").textContent = "Verifying…"; $("mfaErr").textContent = ""; hideMfa(); }
  } catch (e) { $("mfaErr").textContent = e.message; }
  finally { btn.disabled = false; }
}
$("mfaSubmit").addEventListener("click", submitMfa);
$("mfaInput").addEventListener("keydown", (e) => { if (e.key === "Enter") submitMfa(); });
$("mfaCancel").addEventListener("click", async () => {
  try { await Auth.authedFetch("/api/mfa/cancel", { method: "POST" }); } catch (e) {}
  hideMfa();
});

$("applyBtn").addEventListener("click", async () => {
  const btn = $("applyBtn");
  const label = btn.querySelector(".btn-label");
  const origLabel = label.textContent;
  btn.disabled = true;
  btn.classList.add("busy");
  label.textContent = "⏳ Applying…";
  const t = Toast.loading("Logging into Meltwater and applying tags — this can take a minute…", "Applying tags");
  startMfaPolling();
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
    stopMfaPolling();
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
$("modePill").textContent = FETCH_MODE_LABELS[$("fetchMode").value] || $("fetchMode").value;
countUrls();
