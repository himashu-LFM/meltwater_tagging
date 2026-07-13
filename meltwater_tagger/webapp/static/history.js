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
        <div class="stats" style="align-items:center">
          <span class="stat">${run.total_posts} posts</span>
          <span class="stat">🟢 ${run.positive_count}</span>
          <span class="stat">🔴 ${run.negative_count}</span>
          <span class="stat">⚪ ${run.neutral_count}</span>
          ${run.applied_count ? `<span class="stat">🏷 ${run.applied_count} taggable</span>` : ""}
          <span class="chip ${run.status === 'applied' ? 'positive' : 'neutral'}">${run.status === 'applied' ? '✓ applied' : run.status}</span>
          <button class="mini-btn" data-export="${run.id}">⬇ Export</button>
          <button class="mini-btn danger" data-delete="${run.id}" data-brand="${escAttr(run.brand_name)}">🗑 Delete</button>
        </div>
      </div>
    </div>`).join("");

  document.querySelectorAll(".run-row").forEach(row => {
    row.addEventListener("click", () => showDetail(row.dataset.id));
  });
  document.querySelectorAll("[data-export]").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();  // don't also open the detail panel
      const r = await Auth.authedFetch(`/api/history/${btn.dataset.export}`);
      const d = await r.json();
      if (d.run) exportRun(d.run);
    });
  });
  document.querySelectorAll("[data-delete]").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = btn.dataset.delete;
      const ok = await Modal.confirm({
        title: `Delete this ${btn.dataset.brand} run?`,
        message: "This permanently removes the run and its results. This can't be undone.",
        okText: "Delete run",
        danger: true,
      });
      if (!ok) return;
      const r = await Auth.authedFetch(`/api/history/${id}`, { method: "DELETE" });
      if (r.ok) {
        Toast.info("Run deleted.", "Removed");
        $("detailPanel").classList.add("hidden");
        loadRuns();
      } else {
        const d = await r.json();
        Toast.error(d.error || "Could not delete this run.");
      }
    });
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
      <td><a href="${encodeURI(res.permalink)}" target="_blank">${escapeHtml((res.permalink||"").slice(0,55))}…</a></td>
      <td>${res.applied ? '<span class="chip positive">✓ Applied</span>' : '—'}</td></tr>`;
  }).join("");
  const applyCount = (data.run.results || []).filter(r => r.action === "apply").length;
  const doneCount = (data.run.results || []).filter(r => r.applied).length;
  panel.innerHTML = `
    <div class="results-head" style="margin-top:0">
      <div>
        <h3 style="margin:0">${escapeHtml(data.run.brand_name)} — ${new Date(data.run.created_at).toLocaleString()}</h3>
        <div class="stats" style="margin-top:8px">
          <span class="chip ${data.run.status === 'applied' ? 'positive' : 'neutral'}">${data.run.status === 'applied' ? '✓ applied' : escapeHtml(data.run.status)}</span>
          <span class="stat">${applyCount} taggable</span>
          ${doneCount ? `<span class="chip positive">🏷 ${doneCount}/${applyCount} in Meltwater</span>` : ""}
          ${applyCount && doneCount && doneCount < applyCount ? `<span class="chip flag">${applyCount - doneCount} remaining</span>` : ""}
        </div>
      </div>
      <div class="results-actions">
        <button class="btn ghost" id="histExportBtn">
          <span class="btn-label">⬇ Export Excel</span>
        </button>
        <button class="btn primary" id="histApplyBtn">
          <span class="btn-shine"></span><span class="btn-label">🏷 Apply to Meltwater</span>
        </button>
      </div>
    </div>
    <div class="table-wrap" style="margin-top:14px">
      <table><thead><tr><th>#</th><th>Sentiment</th><th>Tag</th><th>Reason</th><th>Post</th><th>Applied</th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div>`;

  $("histExportBtn").addEventListener("click", () => exportRun(data.run));
  $("histApplyBtn").addEventListener("click", () => applyRun(data.run));
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function applyRun(run) {
  const applyCount = (run.results || []).filter(r => r.action === "apply").length;
  if (!applyCount) return Toast.info("Nothing to apply — no posts in this run were tagged.", "Nothing to do");

  const ok = await Modal.confirm({
    title: `Apply ${applyCount} tag(s) to Meltwater?`,
    message: `This logs into your saved Meltwater account and applies the tags from this ${run.brand_name} run (${new Date(run.created_at).toLocaleDateString()}).`,
    okText: "Apply now",
  });
  if (!ok) return;

  const btn = $("histApplyBtn");
  if (btn) { btn.disabled = true; btn.querySelector(".btn-label").textContent = "⏳ Applying…"; }
  const t = Toast.loading("Logging into Meltwater and applying tags — this can take a minute…", "Applying tags");
  try {
    const r = await Auth.authedFetch("/api/apply", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ results: run.results, run_brand: run.brand_name, run_id: run.id }),
    });
    const data = await r.json();
    if (r.ok) {
      const appliedNow = (data.applied || []).length;
      if (appliedNow) {
        t.success(`${data.message} · ${(data.skipped_already||[]).length} already tagged, ${(data.failed||[]).length} failed.`, "Applied to Meltwater");
        if (window.FX && window.FX.celebrate) window.FX.celebrate();
      } else {
        t.info("No new tags were applied — open the run to see per-post status.", "Nothing applied");
      }
      if ((data.unreached || []).length) {
        Toast.info(`${data.unreached.length} post(s) weren't found in the Meltwater feed — check the topic's date range covers them.`, "Some posts not found");
      }
      loadRuns();  // refresh status chip in the list
      showDetail(run.id);  // re-fetch so per-post "Applied" chips reflect what was actually confirmed
    } else {
      t.error(data.error || data.message || "Apply failed.");
    }
  } catch (err) {
    t.error(err.message);
  } finally {
    const b = $("histApplyBtn");
    if (b) { b.disabled = false; b.querySelector(".btn-label").textContent = "🏷 Apply to Meltwater"; }
  }
}

async function exportRun(run) {
  const t = Toast.loading("Preparing your Excel…");
  try {
    const r = await Auth.authedFetch("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ results: run.results || [], run_brand: run.brand_name }),
    });
    if (!r.ok) { t.error("Export failed. Please try again."); return; }
    const blob = await r.blob();
    const stamp = new Date(run.created_at).toISOString().slice(0, 10);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `tagging_${run.brand_name}_${stamp}.xlsx`;
    a.click();
    URL.revokeObjectURL(a.href);
    t.success(`Exported ${(run.results || []).length} rows.`, "Download ready");
  } catch (err) {
    t.error(err.message);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escAttr(s) { return escapeHtml(s); }

$("logoutLink").addEventListener("click", async (e) => {
  e.preventDefault();
  await Auth.signOut();
  window.location.href = "/login";
});
