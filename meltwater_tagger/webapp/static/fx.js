// Micro-interaction layer: 3D card tilt, magnetic buttons, click ripple,
// gold particle bursts, cursor glow, and count-up stats. Vanilla, dependency
// -free, and disabled on touch / reduced-motion for accessibility + perf.
(function () {
  const reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const coarse = window.matchMedia && matchMedia("(pointer: coarse)").matches;
  const rich = !reduce && !coarse;

  // ---------- cursor glow ----------
  if (rich) {
    const glow = document.createElement("div");
    glow.className = "cursor-glow";
    document.body.appendChild(glow);
    let gx = innerWidth / 2, gy = innerHeight / 2, tx = gx, ty = gy;
    addEventListener("mousemove", (e) => { tx = e.clientX; ty = e.clientY; });
    (function loop() {
      gx += (tx - gx) * 0.15; gy += (ty - gy) * 0.15;
      glow.style.transform = `translate(${gx - 150}px, ${gy - 150}px)`;
      requestAnimationFrame(loop);
    })();
  }

  // ---------- burst canvas ----------
  const fxCanvas = document.createElement("canvas");
  fxCanvas.className = "fx-canvas";
  document.body.appendChild(fxCanvas);
  const fctx = fxCanvas.getContext("2d");
  let parts = [];
  function sizeFx() { fxCanvas.width = innerWidth; fxCanvas.height = innerHeight; }
  sizeFx(); addEventListener("resize", sizeFx);

  function burst(x, y, n = 24, power = 1) {
    for (let i = 0; i < n; i++) {
      const a = Math.random() * Math.PI * 2;
      const sp = (2 + Math.random() * 5) * power;
      parts.push({
        x, y, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp - 1,
        life: 1, size: 2 + Math.random() * 3,
        c: Math.random() < 0.75 ? "253,185,19" : "255,255,255",
      });
    }
    if (!burst.running) { burst.running = true; requestAnimationFrame(tick); }
  }
  function tick() {
    fctx.clearRect(0, 0, fxCanvas.width, fxCanvas.height);
    parts = parts.filter(p => p.life > 0);
    for (const p of parts) {
      p.x += p.vx; p.y += p.vy; p.vy += 0.12; p.vx *= 0.98; p.life -= 0.02;
      fctx.globalAlpha = Math.max(p.life, 0);
      fctx.fillStyle = `rgba(${p.c},${p.life})`;
      fctx.beginPath(); fctx.arc(p.x, p.y, p.size * p.life, 0, Math.PI * 2); fctx.fill();
    }
    fctx.globalAlpha = 1;
    if (parts.length) requestAnimationFrame(tick); else burst.running = false;
  }
  window.FX = window.FX || {};
  window.FX.burst = burst;
  window.FX.celebrate = () => {
    const cx = innerWidth / 2, cy = innerHeight / 3;
    burst(cx, cy, 80, 1.8);
    setTimeout(() => burst(cx - 120, cy + 40, 40, 1.4), 120);
    setTimeout(() => burst(cx + 120, cy + 40, 40, 1.4), 220);
  };

  // ---------- ripple + burst on primary buttons (delegated) ----------
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".btn");
    if (!btn) return;
    // ripple
    const r = document.createElement("span");
    r.className = "ripple";
    const rect = btn.getBoundingClientRect();
    const d = Math.max(rect.width, rect.height);
    r.style.width = r.style.height = d + "px";
    r.style.left = (e.clientX - rect.left - d / 2) + "px";
    r.style.top = (e.clientY - rect.top - d / 2) + "px";
    btn.appendChild(r);
    setTimeout(() => r.remove(), 600);
    // gold burst for primary actions
    if (btn.classList.contains("primary")) burst(e.clientX, e.clientY, 18, 0.9);
  });

  if (!rich) return;  // skip motion-heavy tilt/magnetic on touch/reduced-motion

  // ---------- 3D tilt on cards (delegated via per-element listeners) ----------
  function attachTilt(el, max = 6) {
    el.addEventListener("mousemove", (ev) => {
      const r = el.getBoundingClientRect();
      const px = (ev.clientX - r.left) / r.width - 0.5;
      const py = (ev.clientY - r.top) / r.height - 0.5;
      el.style.transform = `perspective(900px) rotateY(${px * max}deg) rotateX(${-py * max}deg)`;
    });
    el.addEventListener("mouseleave", () => { el.style.transform = ""; });
  }

  // ---------- magnetic buttons ----------
  function attachMagnet(btn) {
    btn.addEventListener("mousemove", (ev) => {
      const r = btn.getBoundingClientRect();
      const mx = ev.clientX - r.left - r.width / 2;
      const my = ev.clientY - r.top - r.height / 2;
      btn.style.transform = `translate(${mx * 0.18}px, ${my * 0.28}px)`;
    });
    btn.addEventListener("mouseleave", () => { btn.style.transform = ""; });
  }

  function wire() {
    document.querySelectorAll(".auth-card, .card.glass").forEach(el => {
      if (!el.dataset.tilt) { el.dataset.tilt = "1"; attachTilt(el, el.classList.contains("auth-card") ? 5 : 3); }
    });
    document.querySelectorAll(".btn").forEach(b => {
      if (!b.dataset.mag) { b.dataset.mag = "1"; attachMagnet(b); }
    });
  }
  wire();
  // re-wire when views swap (results buttons appear later)
  new MutationObserver(wire).observe(document.body, { childList: true, subtree: true });

  // ---------- count-up stats ----------
  function animateCount(el) {
    const m = el.textContent.match(/(\D*)(\d+)(\D*)/);
    if (!m) return;
    const [_, pre, numStr, post] = m;
    const target = parseInt(numStr, 10);
    if (target === 0) return;
    let cur = 0;
    const step = Math.max(1, Math.round(target / 20));
    const id = setInterval(() => {
      cur += step;
      if (cur >= target) { cur = target; clearInterval(id); }
      el.textContent = pre + cur + post;
    }, 25);
  }
  const statsHost = document.getElementById("stats");
  if (statsHost) {
    new MutationObserver(() => {
      statsHost.querySelectorAll(".stat").forEach(animateCount);
    }).observe(statsHost, { childList: true });
  }
})();
