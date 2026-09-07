(function () {
  const ROOT = document.documentElement;
  const THEMES = ["dark", "light"];
  const STYLES = ["midnight", "forest", "sunset", "graphite", "ocean"];

  function readTheme() {
    const t = ROOT.getAttribute("data-theme") || localStorage.getItem("tk-theme") || "dark";
    return THEMES.includes(t) ? t : "dark";
  }

  function readStyle() {
    const s = ROOT.getAttribute("data-style") || localStorage.getItem("tk-style") || "midnight";
    return STYLES.includes(s) ? s : "midnight";
  }

  function applyTheme(theme) {
    if (!THEMES.includes(theme)) theme = "dark";
    ROOT.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("tk-theme", theme);
    } catch (_) {}
    syncUi();
  }

  function applyStyle(style) {
    if (!STYLES.includes(style)) style = "midnight";
    ROOT.setAttribute("data-style", style);
    try {
      localStorage.setItem("tk-style", style);
    } catch (_) {}
    syncUi();
  }

  function syncUi() {
    const theme = readTheme();
    const style = readStyle();
    document.querySelectorAll("[data-theme-value]").forEach((btn) => {
      const on = btn.getAttribute("data-theme-value") === theme;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
    document.querySelectorAll("[data-style-value]").forEach((btn) => {
      const on = btn.getAttribute("data-style-value") === style;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function openModal(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.hidden = false;
    document.body.classList.add("modal-open");
    syncUi();
  }

  function closeModal(fromEl) {
    const modal = fromEl && fromEl.closest ? fromEl.closest(".modal") : fromEl;
    if (!modal) return;
    modal.hidden = true;
    if (!document.querySelector(".modal:not([hidden])")) {
      document.body.classList.remove("modal-open");
    }
  }

  document.querySelectorAll("[data-open-modal]").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      const id = btn.getAttribute("data-open-modal");
      if (!id) return;
      // Only handle global settings opener here; page-local handlers may also exist
      if (id === "modal-app-settings") {
        event.preventDefault();
        openModal(id);
      }
    });
  });

  const settingsModal = document.getElementById("modal-app-settings");
  if (settingsModal) {
    settingsModal.querySelectorAll("[data-close-modal]").forEach((el) => {
      el.addEventListener("click", () => closeModal(el));
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (settingsModal.hidden) return;
      closeModal(settingsModal);
    });
  }

  document.querySelectorAll("[data-theme-value]").forEach((btn) => {
    btn.addEventListener("click", () => applyTheme(btn.getAttribute("data-theme-value")));
  });
  document.querySelectorAll("[data-style-value]").forEach((btn) => {
    btn.addEventListener("click", () => applyStyle(btn.getAttribute("data-style-value")));
  });

  syncUi();
})();
