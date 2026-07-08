// Premium toast notifications. API:
//   Toast.success(msg, title?)   Toast.error(msg, title?)   Toast.info(msg, title?)
//   const t = Toast.loading("Working…");  t.success("Done!");  t.error("Nope");  t.close();
window.Toast = (function () {
  let wrap;
  function host() {
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.className = "toast-wrap";
      document.body.appendChild(wrap);
    }
    return wrap;
  }

  const ICONS = {
    success: `<svg viewBox="0 0 24 24" fill="none" stroke="#25d0a0" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path class="draw" d="M20 6 9 17l-5-5"/></svg>`,
    error: `<svg viewBox="0 0 24 24" fill="none" stroke="#ff5d73" stroke-width="3" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>`,
    info: `<svg viewBox="0 0 24 24" fill="none" stroke="#fdb913" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v4h1"/></svg>`,
    loading: `<div class="toast-spin"></div>`,
  };
  const TITLES = { success: "Success", error: "Something went wrong", info: "Heads up", loading: "Working…" };

  function iconHtml(type) {
    return `<div class="toast-ic">${ICONS[type] || ICONS.info}</div>`;
  }

  function remove(el) {
    if (!el || el._gone) return;
    el._gone = true;
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 400);
  }

  function build(type, message, title, duration) {
    const el = document.createElement("div");
    el.className = "toast " + type + (duration ? " timed" : "");
    if (duration) el.style.setProperty("--dur", duration + "ms");
    el.innerHTML =
      iconHtml(type) +
      `<div class="toast-body">
         <div class="toast-title">${esc(title || TITLES[type])}</div>
         <div class="toast-msg">${esc(message || "")}</div>
       </div>
       <button class="toast-close" aria-label="Dismiss">&times;</button>
       ${duration ? '<div class="toast-bar"></div>' : ""}`;
    el.querySelector(".toast-close").addEventListener("click", () => remove(el));

    let timer;
    if (duration) {
      const start = () => { timer = setTimeout(() => remove(el), duration); };
      const stop = () => clearTimeout(timer);
      start();
      el.addEventListener("mouseenter", stop);
      el.addEventListener("mouseleave", start);
    }
    host().appendChild(el);

    // celebratory sparkle on success (reuses the FX burst if present)
    if (type === "success" && window.FX && window.FX.burst) {
      const r = el.getBoundingClientRect();
      window.FX.burst(r.left + 22, r.top + 22, 14, 0.7);
    }
    return el;
  }

  function show(type, message, title, duration = 4200) {
    return build(type, message, title, duration);
  }

  function morph(el, type, message, title, duration = 4200) {
    if (!el || !el.isConnected) return show(type, message, title, duration);
    el.className = "toast " + type + " timed";
    el.style.setProperty("--dur", duration + "ms");
    el.querySelector(".toast-ic").innerHTML = ICONS[type] || ICONS.info;
    el.querySelector(".toast-title").textContent = title || TITLES[type];
    el.querySelector(".toast-msg").textContent = message || "";
    let bar = el.querySelector(".toast-bar");
    if (!bar) { bar = document.createElement("div"); bar.className = "toast-bar"; el.appendChild(bar); }
    bar.style.animation = "none"; void bar.offsetWidth; bar.style.animation = "";
    setTimeout(() => remove(el), duration);
    if (type === "success" && window.FX && window.FX.burst) {
      const r = el.getBoundingClientRect();
      window.FX.burst(r.left + 22, r.top + 22, 14, 0.7);
    }
    return el;
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  return {
    success: (m, t) => show("success", m, t),
    error: (m, t) => show("error", m, t),
    info: (m, t) => show("info", m, t),
    loading: (m, t) => {
      const el = build("loading", m, t, 0); // no auto-dismiss
      return {
        el,
        success: (msg, title) => morph(el, "success", msg, title),
        error: (msg, title) => morph(el, "error", msg, title),
        info: (msg, title) => morph(el, "info", msg, title),
        close: () => remove(el),
      };
    },
  };
})();
