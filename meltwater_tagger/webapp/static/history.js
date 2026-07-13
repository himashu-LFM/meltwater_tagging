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
  const taggable = (res) => res.action === "apply" && res.tag;
  const rows = (data.run.results || []).map((res, idx) => {
    const s = (res.sentiment || "").toLowerCase();
    const cls = ["positive", "negative", "neutral"].includes(s) ? s : "flag";
    // Prefer the stored type; for older runs (saved before content_type existed)
    // derive it from the URL — a comment URL is unmistakable, so this is exact.
    const ctype = (res.content_type || deriveContentType(res.permalink)).toLowerCase();
    const typeChip = ctype === "comment"
      ? '<span class="chip type-comment">💬 Comment</span>'
      : '<span class="chip type-post">📄 Post</span>';
    const canTag = taggable(res) && !res.applied;
    const actionCell = res.applied
      ? '<span class="chip positive">✓ Applied</span>'
      : (canTag ? `<button class="mini-btn" data-tag-idx="${idx}">🏷 Tag this post</button>` : "—");
    return `<tr><td>${idx + 1}</td><td>${typeChip}</td>
      <td><span class="chip ${cls}">${escapeHtml(s || res.action)}</span></td>
      <td>${escapeHtml(res.tag || "—")}</td><td class="reason">${escapeHtml(res.reason || "")}</td>
      <td><a href="${encodeURI(res.permalink)}" target="_blank">${escapeHtml((res.permalink||"").slice(0,55))}…</a></td>
      <td>${actionCell}</td></tr>`;
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
      <table><thead><tr><th>#</th><th>Type</th><th>Sentiment</th><th>Tag</th><th>Reason</th><th>Post</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div>`;

  $("histExportBtn").addEventListener("click", () => exportRun(data.run));
  $("histApplyBtn").addEventListener("click", () => applyRun(data.run));
  panel.querySelectorAll("[data-tag-idx]").forEach(btn => {
    btn.addEventListener("click", () => applySinglePost(data.run, +btn.dataset.tagIdx, btn));
  });
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function applySinglePost(run, idx, btn) {
  const res = (run.results || [])[idx];
  if (!res) return;

  // Guard against firing this while a bulk (or another single) apply is
  // already running for the same run -- two concurrent Playwright sessions
  // logging into the same Meltwater account at once is asking for trouble.
  if (document.querySelector('[data-tag-idx][disabled], #histApplyBtn:disabled')) {
    return Toast.info("An apply is already in progress for this run — wait for it to finish.", "Please wait");
  }

  const ok = await Modal.confirm({
    title: "Tag this one post?",
    message: `This logs into your saved Meltwater account and applies "${res.tag}" to just this post.`,
    okText: "Tag it",
  });
  if (!ok) return;

  document.querySelectorAll("[data-tag-idx]").forEach(b => b.disabled = true);
  const histBtn = $("histApplyBtn");
  if (histBtn) histBtn.disabled = true;
  const origText = btn.textContent;
  btn.textContent = "⏳ Tagging…";

  const t = Toast.loading("Logging into Meltwater and applying this tag…", "Applying tag");
  try {
    const r = await Auth.authedFetch("/api/apply", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ results: [res], run_brand: run.brand_name, run_id: run.id }),
    });
    const data = await r.json();
    if (r.ok) {
      const appliedNow = (data.applied || []).length;
      const alreadyDone = (data.skipped_already || []).length;
      if (appliedNow || alreadyDone) {
        t.success(appliedNow ? `Tagged with "${res.tag}".` : "Already tagged in Meltwater.", "Applied to Meltwater");
        if (appliedNow && window.FX && window.FX.celebrate) window.FX.celebrate();
      } else if ((data.unreached || []).length) {
        t.error("This post wasn't found in the Meltwater feed — check the topic's date range covers it.", "Not found");
      } else {
        t.error(data.message || "Could not tag this post.", "Apply failed");
      }
      loadRuns();
      showDetail(run.id);  // re-fetch so this row's real status reflects what Meltwater confirmed
    } else {
      t.error(data.error || data.message || "Apply failed.");
      btn.textContent = origText;
    }
  } catch (err) {
    t.error(err.message);
    btn.textContent = origText;
  } finally {
    document.querySelectorAll("[data-tag-idx]").forEach(b => b.disabled = false);
    if (histBtn) histBtn.disabled = false;
  }
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

// Mirrors classify.py reddit_ids: a Reddit URL is a comment if it has a
// /comment/<id> segment OR a trailing base36 id after the title slug.
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

$("logoutLink").addEventListener("click", async (e) => {
  e.preventDefault();
  await Auth.signOut();
  window.location.href = "/login";
});
