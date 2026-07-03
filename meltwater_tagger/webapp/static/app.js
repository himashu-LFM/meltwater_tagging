const $ = (id) => document.getElementById(id);

const state = { urls: [], results: [], brand: "" };

// ---- URL counting ----
function countUrls() {
  const pasted = $("urls").value.split(/\s+/).map(s => s.trim()).filter(Boolean);
  const total = pasted.length || state.urls.length;
  $("urlCount").textContent = `${total} URL${total === 1 ? "" : "s"}`;
}
$("urls").addEventListener("input", () => { state.urls = []; countUrls(); });

// ---- fetch-mode pill ----
$("fetchMode").addEventListener("change", (e) => {
  $("modePill").textContent = e.target.value === "cdp" ? "CDP fetch" : "Anon fetch";
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
    const r = await fetch("/api/extract", { method: "POST", body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Failed to read file");
    state.urls = data.urls;
    $("urls").value = "";               // uploaded URLs take over
    if (data.brand && !$("brand").value) $("brand").value = data.brand;
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
  if (!brand) return ($("inputErr").textContent = "Please enter the run brand.");
  if (!urls.length) return ($("inputErr").textContent = "Upload an Excel or paste at least one URL.");

  state.brand = brand;
  showView("loadingView");
  cycleLoaderText();

  try {
    const r = await fetch("/api/classify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls, brand, fetch_mode: $("fetchMode").value }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Classification failed");
    state.results = data.results;
    renderResults(data);
    showView("resultsView");
  } catch (err) {
    showView("inputView");
    $("inputErr").textContent = err.message;
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
  const res = data.results;

  const counts = { positive: 0, negative: 0, neutral: 0, other: 0 };
  res.forEach(r => {
    const s = (r.sentiment || "").toLowerCase();
    if (counts[s] !== undefined) counts[s]++; else counts.other++;
  });
  $("stats").innerHTML =
    `<span class="stat">${res.length} posts</span>` +
    `<span class="stat">🟢 ${counts.positive} positive</span>` +
    `<span class="stat">🔴 ${counts.negative} negative</span>` +
    `<span class="stat">⚪ ${counts.neutral} neutral</span>` +
    `<span class="stat">⚑ ${counts.other} flagged/other</span>`;

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
      <td><a href="${encodeURI(r.permalink)}" target="_blank" rel="noopener">${escapeHtml(shorten(r.permalink))}</a></td>`;
    body.appendChild(tr);
  });
}

function shorten(u) { return u.length > 60 ? u.slice(0, 57) + "…" : u; }
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---- export ----
$("exportBtn").addEventListener("click", async () => {
  const r = await fetch("/api/export", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ results: state.results, run_brand: state.brand }),
  });
  if (!r.ok) return alert("Export failed");
  const blob = await r.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `tagging_${state.brand}.xlsx`;
  a.click();
  URL.revokeObjectURL(a.href);
});

// ---- nav ----
$("backBtn").addEventListener("click", () => showView("inputView"));
function showView(id) {
  ["inputView", "loadingView", "resultsView"].forEach(v => $(v).classList.toggle("hidden", v !== id));
}

// sync fetch-mode pill with whatever option is selected on load
$("modePill").textContent = $("fetchMode").value === "cdp" ? "CDP fetch" : "Anon fetch";
countUrls();
