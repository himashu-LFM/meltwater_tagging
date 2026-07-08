// On-brand modal dialogs replacing native alert/confirm/prompt.
//   await Modal.prompt({title, message?, placeholder?, value?, okText?})  -> string | null
//   await Modal.confirm({title, message?, okText?, danger?})             -> boolean
window.Modal = (function () {
  function build(opts, withInput) {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "modal-overlay";
      overlay.innerHTML = `
        <div class="modal-card glass" role="dialog" aria-modal="true">
          <h3 class="modal-title">${esc(opts.title || "")}</h3>
          ${opts.message ? `<p class="modal-msg">${esc(opts.message)}</p>` : ""}
          ${withInput ? `<input class="modal-input" type="text" placeholder="${esc(opts.placeholder || "")}" />` : ""}
          <div class="modal-actions">
            <button class="btn ghost modal-cancel">${esc(opts.cancelText || "Cancel")}</button>
            <button class="btn primary modal-ok ${opts.danger ? "danger" : ""}">
              <span class="btn-shine"></span><span class="btn-label">${esc(opts.okText || "OK")}</span>
            </button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      requestAnimationFrame(() => overlay.classList.add("show"));

      const input = overlay.querySelector(".modal-input");
      if (input) {
        if (opts.value) input.value = opts.value;
        setTimeout(() => input.focus(), 60);
      }

      function close(result) {
        overlay.classList.remove("show");
        setTimeout(() => overlay.remove(), 220);
        document.removeEventListener("keydown", onKey);
        resolve(result);
      }
      function onKey(e) {
        if (e.key === "Escape") close(withInput ? null : false);
        if (e.key === "Enter") ok();
      }
      function ok() { close(withInput ? (input.value.trim() || null) : true); }

      overlay.querySelector(".modal-ok").addEventListener("click", ok);
      overlay.querySelector(".modal-cancel").addEventListener("click", () => close(withInput ? null : false));
      overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(withInput ? null : false); });
      document.addEventListener("keydown", onKey);
    });
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  return {
    prompt: (opts) => build(opts, true),
    confirm: (opts) => build(opts, false),
  };
})();
