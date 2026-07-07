const $ = (id) => document.getElementById(id);

(async () => {
  const session = await Auth.requireAuthOrRedirect();
  if (!session) return;
  await loadRuns();
})();

async function loadRuns() {
  const r = await Auth.authedFetch("/api/history");
  const data = await r.json();
  const el = $("runList");
  if (!data.runs || !data.runs.length) {
    el.innerHTML = `<div class="card glass" style="text-align:center;color:var(--muted)">
      No runs yet — go tag some posts.</div>`;
    return;
  }
  el.innerHTML = data.runs.map((run, i) => `
    <div class="card glass run-row" data-id="${run.id}" style="margin-bottom:14px;cursor:pointer;
         animation-delay:${i * 40}ms">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
        <div>
          <div style="font-weight:700;font-size:16px">${escapeHtml(run.brand_name)}</div>
          <div style="color:var(--muted);font-size:13px">${new Date(run.created_at).toLocaleString()}</div>
        </div>
        <div class="stats">
          <span class="stat">${run.total_posts} posts</span>
          <span class="stat">🟢 ${run.positive_count}</span>
          <span class="stat">🔴 ${run.negative_count}</span>
          <span class="stat">⚪ ${run.neutral_count}</span>
          <span class="chip ${run.status === 'applied' ? 'positive' : 'neutral'}">${run.status}</span>
        </div>
      </div>
    </div>`).join("");

  document.querySelectorAll(".run-row").forEach(row => {
    row.addEventListener("click", () => showDetail(row.dataset.id));
  });
}

async function showDetail(id) {
  const r = await Auth.authedFetch(`/api/history/${id}`);
  const data = await r.json();
  const panel = $("detailPanel");
  panel.classList.remove("hidden");
  if (!data.run) { panel.innerHTML = "Not found."; return; }
  const rows = (data.run.results || []).map((res, idx) => {
    const s = (res.sentiment || "").toLowerCase();
    const cls = ["positive", "negative", "neutral"].includes(s) ? s : "flag";
    return `<tr><td>${idx + 1}</td><td><span class="chip ${cls}">${escapeHtml(s || res.action)}</span></td>
      <td>${escapeHtml(res.tag || "—")}</td><td class="reason">${escapeHtml(res.reason || "")}</td>
      <td><a href="${encodeURI(res.permalink)}" target="_blank">${escapeHtml((res.permalink||"").slice(0,55))}…</a></td></tr>`;
  }).join("");
  panel.innerHTML = `
    <h3 style="margin-top:0">${escapeHtml(data.run.brand_name)} — ${new Date(data.run.created_at).toLocaleString()}</h3>
    <div class="table-wrap" style="margin-top:14px">
      <table><thead><tr><th>#</th><th>Sentiment</th><th>Tag</th><th>Reason</th><th>Post</th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div>`;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

$("logoutLink").addEventListener("click", async (e) => {
  e.preventDefault();
  await Auth.signOut();
  window.location.href = "/login";
});
